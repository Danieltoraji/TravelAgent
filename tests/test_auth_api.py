"""账号体系端到端测试（多用户改造 2026-09，方案见 docs/sync_notes_multiuser_for_ac_*.md）。

走 django.test.Client 全中间件链（Bearer token 认证 → 每用户运行时注入）：
- 白名单：/api/health/ 免认证；register/login 免认证；
- 门禁：业务端点无 token / 垃圾 token → 401；
- 账号：注册（重复 409 / 短密码 400）、登录（错密码 401、单设备轮换旧 token）、
  me、logout（token 失效）；
- 认证后基础可用：status 200、timeline 写读。

DB 为 conftest（tests/conftest.py pytest_configure）准备的独立临时 sqlite。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import uuid

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        from django.test import Client

        self.client = Client()
        self._created: list[str] = []

    def tearDown(self) -> None:
        from django.contrib.auth.models import User

        from runtime.manager import manager

        for username in self._created:
            try:
                user = User.objects.filter(username=username).first()
                if user is not None:
                    manager.drop(user.id)
                    user.delete()
            except Exception:  # noqa: BLE001  清理失败不影响其他用例
                pass

    # -- 辅助 ---------------------------------------------------------------

    def _register(self, username: str | None = None, password: str = "secret123"):
        username = username or f"u_{uuid.uuid4().hex[:8]}"
        self._created.append(username)
        resp = self.client.post(
            "/api/auth/register/",
            data=json.dumps({"username": username, "password": password}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        return username, json.loads(resp.content)["token"]

    def _get(self, path: str, token: str | None = "unused"):
        kwargs = {}
        if token is not None:
            kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.get(path, **kwargs)

    def _post(self, path: str, body: dict, token: str | None = "unused"):
        kwargs: dict = {"content_type": "application/json"}
        if token is not None:
            kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(path, data=json.dumps(body), **kwargs)

    _TIMELINE = {
        "city": "北京", "start_date": "2026-10-01", "end_date": "2026-10-01",
        "days": [{"day": 1, "date": "2026-10-01",
                  "items": [{"name": "故宫", "arrival": "09:00"}]}],
    }


class TestAuthGate(_Base):
    """401 门禁与白名单。"""

    def test_health_is_public(self) -> None:
        self.assertEqual(self._get("/api/health/", token=None).status_code, 200)

    def test_register_login_are_public(self) -> None:
        resp = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": "nobody", "password": "whatever"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)  # 路由可达（免 Bearer），只是密码错

    def test_business_endpoint_requires_token(self) -> None:
        resp = self._get("/api/status/", token=None)
        self.assertEqual(resp.status_code, 401)
        self.assertIn("unauthorized", json.loads(resp.content)["error"])

    def test_garbage_token_rejected(self) -> None:
        self.assertEqual(self._get("/api/status/", token="garbage").status_code, 401)

    def test_malformed_header_rejected(self) -> None:
        resp = self.client.get("/api/status/", HTTP_AUTHORIZATION="Basic abc")
        self.assertEqual(resp.status_code, 401)


class TestAccountLifecycle(_Base):
    def test_register_success_and_duplicate(self) -> None:
        username, token = self._register()
        self.assertTrue(token)
        # 重复注册 → 409
        resp = self.client.post(
            "/api/auth/register/",
            data=json.dumps({"username": username, "password": "secret123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 409)

    def test_register_validations(self) -> None:
        for body in ({}, {"username": "x"}, {"username": "", "password": "x"*8}):
            resp = self.client.post(
                "/api/auth/register/", data=json.dumps(body),
                content_type="application/json",
            )
            self.assertEqual(resp.status_code, 400, body)
        resp = self.client.post(
            "/api/auth/register/",
            data=json.dumps({"username": "shortpw", "password": "123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_login_and_token_rotation(self) -> None:
        # 错密码 → 401
        username, token1 = self._register()
        resp = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": username, "password": "wrong-password"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 401)
        # 正确登录 → 新 token；旧 token 被轮换失效（单设备语义）
        resp = self.client.post(
            "/api/auth/login/",
            data=json.dumps({"username": username, "password": "secret123"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200)
        token2 = json.loads(resp.content)["token"]
        self.assertNotEqual(token1, token2)
        self.assertEqual(self._get("/api/auth/me/", token=token1).status_code, 401)
        self.assertEqual(self._get("/api/auth/me/", token=token2).status_code, 200)

    def test_me_and_logout(self) -> None:
        username, token = self._register()
        resp = self._get("/api/auth/me/", token=token)
        body = json.loads(resp.content)
        self.assertEqual(body["username"], username)
        self.assertFalse(body["has_plan"])
        # logout 后 token 失效
        self.assertEqual(self._post("/api/auth/logout/", {}, token=token).status_code, 200)
        self.assertEqual(self._get("/api/auth/me/", token=token).status_code, 401)

    def test_authed_status_and_timeline_roundtrip(self) -> None:
        _, token = self._register()
        self.assertEqual(self._get("/api/status/", token=token).status_code, 200)
        resp = self._post("/api/timeline/", dict(self._TIMELINE), token=token)
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._get("/api/timeline/", token=token)
        self.assertEqual(json.loads(resp.content)["city"], "北京")
        # me 的 has_plan 翻转
        self.assertTrue(json.loads(self._get("/api/auth/me/", token=token).content)["has_plan"])


if __name__ == "__main__":
    unittest.main()
