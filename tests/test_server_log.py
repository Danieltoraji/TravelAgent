"""服务端日志观测测试（server_log 2026-09-15）。

- 请求级日志：X-Request-ID 响应头、401 拒绝日志、POST 请求行日志、
  request_id 贯穿（RequestIdFilter 把 ContextVar 注入 LogRecord）；
- 报错补记录：畸形 JSON 体、plan 异常转 500 不再静默；
- 工具调用：抛异常的调用现在有 logger.exception（此前完全无记录）。

plan 全链路用桩（替换 AgentRuntime.init_from_requirement）避免真实规划器：
桩必须返回真实 TripTimeline——响应构造与 snapshot()/persist 都要走 to_dict。
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import unittest
import uuid
from datetime import date
from unittest import mock

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_PLAN_BODY = {
    "content": {"destination": "北京", "days": 1, "constraints": {"budget": 3000}},
}


def _tiny_timeline():
    from core.schemas import DayPlan, Place, TripTimeline

    return TripTimeline(
        id="tl-test", city="北京",
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 1),
        days=[DayPlan(day=1, date=date(2026, 10, 1),
                      items=[Place(id="BJ_001", name="故宫", arrival="09:00")])],
    )


def _fake_init_from_requirement(self, payload):
    """plan 桩：与真实现同构的最小会话建立（记 requirement + 真实小时间轴），
    跳过 20-70s 的真实规划器。必须用真实 TripTimeline——响应构造与
    snapshot()/persist 都要走 to_dict。"""
    self.requirement = payload
    self._last_planner_error = None
    timeline = _tiny_timeline()
    self.init_timeline(timeline)
    return timeline


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        from django.test import Client

        self.client = Client()
        self._created: list[str] = []
        # plan 用桩：替换真实规划器（见 _fake_init_from_requirement）
        self._patchers = [
            mock.patch(
                "runtime.agent_runtime.AgentRuntime.init_from_requirement",
                autospec=True,
                side_effect=_fake_init_from_requirement,
            ),
            mock.patch(
                "runtime.agent_runtime.AgentRuntime.enrich_transport_details",
                autospec=True,
                side_effect=lambda self, timeline, **kw: None,
            ),
        ]
        self._init_mock = self._patchers[0].start()   # 单测可改 side_effect
        self._patchers[1].start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

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

    def _post(self, path: str, body: dict, token: str | None = "unused"):
        kwargs: dict = {"content_type": "application/json"}
        if token is not None:
            kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(path, data=json.dumps(body), **kwargs)

    def _post_raw(self, path: str, raw: str, token: str | None = "unused"):
        kwargs: dict = {"content_type": "application/json"}
        if token is not None:
            kwargs["HTTP_AUTHORIZATION"] = f"Bearer {token}"
        return self.client.post(path, data=raw, **kwargs)


class TestRequestLogs(_Base):
    """请求级日志与 X-Request-ID。"""

    def test_response_carries_request_id(self) -> None:
        _, token = self._register()
        resp = self._post("/api/plan/", dict(_PLAN_BODY), token=token)
        self.assertEqual(resp.status_code, 200, resp.content)
        rid = resp["X-Request-ID"]
        self.assertRegex(rid, r"^[0-9a-f]{8}$")

    def test_unauthorized_logged(self) -> None:
        with self.assertLogs("api.middleware", level="INFO") as cm:
            resp = self.client.get("/api/status/")
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(
            any("401 unauthorized" in msg for msg in cm.output), cm.output
        )

    def test_post_request_line_logged(self) -> None:
        _, token = self._register()
        with self.assertLogs("api.middleware", level="INFO") as cm:
            resp = self._post("/api/plan/", dict(_PLAN_BODY), token=token)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(
            any("POST /api/plan/" in m and "-> 200" in m for m in cm.output),
            cm.output,
        )

    def test_request_id_correlates_log_records(self) -> None:
        """RequestIdFilter 把 ContextVar 里的 request_id 注入日志记录，
        与 X-Request-ID 响应头一致（请求 ↔ 异常串联的机制基础）。"""
        from api.logging_utils import RequestIdFilter

        _, token = self._register()
        captured: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append(record)

        handler = _Capture(level=logging.INFO)
        handler.addFilter(RequestIdFilter())
        mw_logger = logging.getLogger("api.middleware")
        mw_logger.addHandler(handler)
        try:
            resp = self.client.get("/api/status/")
        finally:
            mw_logger.removeHandler(handler)
        self.assertEqual(resp.status_code, 401)
        self.assertTrue(captured)
        for record in captured:
            self.assertEqual(record.request_id, resp["X-Request-ID"])


class TestErrorLogs(_Base):
    """报错路径不再静默。"""

    def test_malformed_json_body_logged(self) -> None:
        _, token = self._register()
        with self.assertLogs("api.views", level="WARNING") as cm:
            resp = self._post_raw("/api/plan/", "{not-json", token=token)
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(any("invalid JSON body" in m for m in cm.output), cm.output)

    def test_plan_exception_logged(self) -> None:
        """plan 内部异常转 500 时必须有 ERROR 日志（此前零记录）。"""
        _, token = self._register()
        self._init_mock.side_effect = RuntimeError("planner exploded")
        try:
            with self.assertLogs("api.views", level="ERROR") as cm:
                resp = self._post("/api/plan/", dict(_PLAN_BODY), token=token)
        finally:
            self._init_mock.side_effect = _fake_init_from_requirement
        self.assertEqual(resp.status_code, 500)
        self.assertTrue(any("plan failed" in m for m in cm.output), cm.output)


class TestToolCallLogging(unittest.TestCase):
    """工具调用失败也留痕（server_log 2026-09-15 补的盲区）。"""

    def test_raising_tool_call_logged_then_reraised(self) -> None:
        from tools import ToolRegistry

        with mock.patch.object(
            ToolRegistry, "call", side_effect=RuntimeError("boom")
        ):
            from runtime.agent_runtime import AgentRuntime

            rt = AgentRuntime()
            with self.assertLogs("runtime.agent", level="ERROR") as cm:
                with self.assertRaises(RuntimeError):
                    rt.registry.call("weather", city="北京")
        self.assertTrue(
            any("tool weather" in m and "boom" in m for m in cm.output), cm.output
        )


if __name__ == "__main__":
    unittest.main()
