"""历史规划归档测试（server_log 2026-09-15）。

- 二次 plan 触发归档：旧会话快照（requirement/timeline/booking_state…）
  写入 TripPlanArchive，当前 Trip 行指向新需求；
- 查询端点：/api/plans/history/ 列表、/<id>/ 全量；需 Bearer token；
  非本人 404；
- 保留上限：每用户最多 PLAN_ARCHIVE_KEEP 份，超出删最旧。

plan 全链路用桩（同 test_server_log.py），避免真实规划器。
"""

from __future__ import annotations

import json
import os
import sys
import unittest
import uuid
from datetime import date
from unittest import mock

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _plan_body(destination: str) -> dict:
    return {"content": {"destination": destination, "days": 1,
                        "constraints": {"budget": 3000}}}


def _tiny_timeline():
    from core.schemas import DayPlan, Place, TripTimeline

    return TripTimeline(
        id="tl-archive", city="北京",
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 1),
        days=[DayPlan(day=1, date=date(2026, 10, 1),
                      items=[Place(id="BJ_001", name="故宫", arrival="09:00")])],
    )


def _fake_init_from_requirement(self, payload):
    """plan 桩（与 test_server_log.py 同构）：记 requirement + 真实小时间轴，
    跳过真实规划器；snapshot()/persist 走 to_dict 必须是真实 TripTimeline。"""
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

        from api.models import TripPlanArchive
        from runtime.manager import manager

        for username in self._created:
            try:
                user = User.objects.filter(username=username).first()
                if user is not None:
                    manager.drop(user.id)
                    TripPlanArchive.objects.filter(user=user).delete()
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

    def _post(self, path: str, body: dict, token: str):
        return self.client.post(
            path, data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def _get(self, path: str, token: str):
        return self.client.get(path, HTTP_AUTHORIZATION=f"Bearer {token}")


class TestPlanArchive(_Base):
    def test_first_plan_no_archive(self) -> None:
        _, token = self._register()
        resp = self._post("/api/plan/", _plan_body("北京"), token)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = json.loads(self._get("/api/plans/history/", token).content)
        self.assertEqual(body["count"], 0)

    def test_second_plan_archives_previous(self) -> None:
        from django.contrib.auth.models import User

        from api.models import Trip, TripPlanArchive

        username, token = self._register()
        self._post("/api/plan/", _plan_body("北京"), token)
        resp = self._post("/api/plan/", _plan_body("上海"), token)
        self.assertEqual(resp.status_code, 200, resp.content)

        user = User.objects.get(username=username)
        archives = TripPlanArchive.objects.filter(user=user).order_by("archived_at")
        self.assertEqual(archives.count(), 1)
        archived = archives.first()
        # 归档的是被覆写前的旧需求（北京），不是当前需求（上海）
        self.assertEqual(
            archived.requirement["content"]["destination"], "北京"
        )
        self.assertEqual(archived.timeline["city"], "北京")
        self.assertEqual(archived.reason, "new_plan")
        # 当前 Trip 行指向新需求（归档不影响覆写语义）
        self.assertEqual(
            Trip.objects.get(user=user).requirement["content"]["destination"],
            "上海",
        )

    def test_history_list_and_detail(self) -> None:
        _, token = self._register()
        self._post("/api/plan/", _plan_body("北京"), token)
        self._post("/api/plan/", _plan_body("上海"), token)

        body = json.loads(self._get("/api/plans/history/", token).content)
        self.assertEqual(body["count"], 1)
        entry = body["archives"][0]
        self.assertEqual(entry["requirement"]["content"]["destination"], "北京")
        self.assertEqual(entry["timeline_days"], 1)

        detail = json.loads(
            self._get(f"/api/plans/history/{entry['id']}/", token).content
        )
        self.assertEqual(detail["requirement"]["content"]["destination"], "北京")
        self.assertEqual(detail["timeline"]["city"], "北京")
        for key in ("events", "replans", "timeline_history", "booking_state"):
            self.assertIn(key, detail)

    def test_history_detail_limit_clamped(self) -> None:
        _, token = self._register()
        self.assertEqual(
            json.loads(self._get("/api/plans/history/?limit=abc", token).content),
            {"error": "limit must be an integer"},
        )

    def test_history_requires_auth_and_scoped(self) -> None:
        _, token_a = self._register()
        self._post("/api/plan/", _plan_body("北京"), token_a)
        self._post("/api/plan/", _plan_body("上海"), token_a)   # 二次 plan 才有归档
        list_body = json.loads(self._get("/api/plans/history/", token_a).content)
        archive_id = list_body["archives"][0]["id"]

        # 无 token → 401
        self.assertEqual(
            self.client.get("/api/plans/history/").status_code, 401
        )
        # 用户 B：列表为空、他人详情 404（不泄漏存在性）
        _, token_b = self._register()
        self.assertEqual(
            json.loads(self._get("/api/plans/history/", token_b).content)["count"], 0
        )
        resp = self._get(f"/api/plans/history/{archive_id}/", token_b)
        self.assertEqual(resp.status_code, 404)

    def test_retention_keeps_newest(self) -> None:
        _, token = self._register()
        with mock.patch("api.views.PLAN_ARCHIVE_KEEP", 2):
            for dest in ("北京", "上海", "广州"):
                resp = self._post("/api/plan/", _plan_body(dest), token)
                self.assertEqual(resp.status_code, 200, resp.content)
        body = json.loads(self._get("/api/plans/history/", token).content)
        self.assertEqual(body["count"], 2)
        # 归档的是"上一次"快照：plan 上海归档北京、plan 广州归档上海——
        # 三次规划后现存 [北京快照, 上海快照]，保留 2 份即这两份
        destinations = [
            a["requirement"]["content"]["destination"] for a in body["archives"]
        ]
        self.assertEqual(destinations, ["上海", "北京"])


if __name__ == "__main__":
    unittest.main()
