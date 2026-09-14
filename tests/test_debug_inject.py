"""/api/debug/inject/ 注入端点测试：payload 校验 + 事件构造 + 真链路调用。

不触发 LLM：用 FakeAgent 替换 runtime.agent，断言注入事件构造正确、
handle_event 被真实调用、响应 decision 映射正确、token 鉴权生效、
persist_world 写假池生效。

多用户改造（2026-09）：视图从 ``request.runtime`` 取运行时——用例构造独立
``AgentRuntime`` 挂到请求上，world/replan_history 均为实例私有。
"""

import json
import os
import sys
import unittest
from datetime import datetime
from types import SimpleNamespace

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"),
           os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=True,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        ROOT_URLCONF=None,
    )
import django  # noqa: E402

django.setup()

from django.http import HttpRequest  # noqa: E402

from api import views  # noqa: E402
from config.settings import settings as app_settings  # noqa: E402
from core.schemas import EventType, MonitorEvent  # noqa: E402
from runtime.agent_runtime import AgentRuntime  # noqa: E402


def _post(body: dict, token: str | None = None, rt: AgentRuntime | None = None) -> HttpRequest:
    req = HttpRequest()
    req.method = "POST"
    req.path = "/api/debug/inject/"
    req._body = json.dumps(body).encode("utf-8")
    if rt is not None:
        req.runtime = rt
    if token:
        req.META["HTTP_X_DEBUG_TOKEN"] = token
    return req


class FakeAgent:
    """替身 ExecutionAgent：记录收到的事件，不触发 LLM。

    significant=True 时 handle_event 返回非空（模拟"达阈值"）；
    record_replan=True 时按 _record_decision 的形状写入所属运行时的
    replan_history。
    """

    def __init__(self, rt: AgentRuntime, significant: bool = False,
                 record_replan: bool = False) -> None:
        self.rt = rt
        self.calls: list[MonitorEvent] = []
        self.significant = significant
        self.record_replan = record_replan

    async def handle_event(self, event: MonitorEvent):  # noqa: ANN201
        self.calls.append(event)
        if self.record_replan:
            self.rt.replan_history.append({
                "id": "replan-1",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "events": [event.to_dict()],
                "current_timeline": {},
                "context": {},
                "decision": {
                    "need_replan": True,
                    "impact": 0.8,
                    "reason": "暴雨影响行程",
                    "diff_summary": ["[rescheduled] 故宫 09:00 → 14:00"],
                    "new_timeline": {"city": "北京", "days": []},
                },
            })
        return object() if self.significant else None


class TestDebugInject(unittest.TestCase):
    def setUp(self) -> None:
        self.rt = AgentRuntime()
        self.rt.timeline = SimpleNamespace(city="北京")
        self.rt.replan_history = []
        self._token = app_settings.debug_inject_token
        app_settings.debug_inject_token = ""

    def tearDown(self) -> None:
        app_settings.debug_inject_token = self._token
        if getattr(self.rt, "world", None) is not None:
            self.rt.world.clear_weather_overrides()
            self.rt.world.clear_traffic_overrides()

    def _run(self, body: dict, token: str | None = None):
        return views.debug_inject(_post(body, token, rt=self.rt))

    # -- 校验路径 ----------------------------------------------------------

    def test_empty_body_rejected(self) -> None:
        resp = self._run({})
        self.assertEqual(resp.status_code, 400)

    def test_no_timeline_rejected(self) -> None:
        self.rt.agent = None
        resp = self._run({"scenario": "storm"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("No timeline set", resp.content.decode())

    def test_unknown_scenario_rejected(self) -> None:
        self.rt.agent = FakeAgent(self.rt)
        resp = self._run({"scenario": "tsunami"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("unknown scenario", resp.content.decode())

    def test_invalid_event_type_rejected(self) -> None:
        self.rt.agent = FakeAgent(self.rt)
        resp = self._run({"event_type": "ufo", "data": {}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("invalid event_type", resp.content.decode())

    def test_scenic_requires_place(self) -> None:
        self.rt.agent = FakeAgent(self.rt)
        resp = self._run({"event_type": "scenic", "data": {"queue_min": 120}})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("place is required", resp.content.decode())

    def test_data_must_be_object(self) -> None:
        self.rt.agent = FakeAgent(self.rt)
        resp = self._run({"event_type": "scenic", "place": "故宫", "data": [1, 2]})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("data must be an object", resp.content.decode())

    # -- 事件构造与真链路 --------------------------------------------------

    def test_storm_preset_builds_weather_event(self) -> None:
        agent = FakeAgent(self.rt, significant=True)
        self.rt.agent = agent
        resp = self._run({"scenario": "storm"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(agent.calls), 1)
        ev = agent.calls[0]
        self.assertEqual(ev.event_type, EventType.WEATHER)
        self.assertEqual(ev.place, "北京")  # weather 缺省挂城市
        self.assertEqual(ev.data["rain_probability"], 85)
        self.assertTrue(ev.event_id.startswith("inject-"))
        body = json.loads(resp.content.decode())
        self.assertTrue(body["significant"])      # FakeAgent significant=True
        self.assertEqual(body["decision"], "hook_error")  # 达阈值但未记录

    def test_queue_preset_requires_place(self) -> None:
        self.rt.agent = FakeAgent(self.rt)
        resp = self._run({"scenario": "queue"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("place is required", resp.content.decode())

    def test_raw_inject_event_fields(self) -> None:
        agent = FakeAgent(self.rt, significant=False)
        self.rt.agent = agent
        resp = self._run({
            "event_type": "traffic",
            "place": "北京-故宫",
            "data": {"delay_min": 45, "congestion": "拥堵"},
            "rule_name": "demo-script",
            "spot_id": "S1",
        })
        self.assertEqual(resp.status_code, 200)
        ev = agent.calls[0]
        self.assertEqual(ev.event_type, EventType.TRAFFIC)
        self.assertEqual(ev.rule_name, "demo-script")
        self.assertEqual(ev.spot_id, "S1")
        body = json.loads(resp.content.decode())
        self.assertFalse(body["significant"])
        self.assertEqual(body["decision"], "not_significant")

    def test_booking_preset_gets_hotel_id(self) -> None:
        agent = FakeAgent(self.rt)
        self.rt.agent = agent
        resp = self._run({"scenario": "hotel_full", "place": "皇城景观酒店"})
        self.assertEqual(resp.status_code, 200)
        ev = agent.calls[0]
        self.assertEqual(ev.event_type, EventType.BOOKING)
        # 2026-08-31：place 名称解析为酒店池真实 id（换宿排除必须用池 id）
        self.assertEqual(ev.data["hotel_id"], "BJ_H001")

    def test_booking_hotel_id_fallback_to_name(self) -> None:
        """live 酒店（不在假池）名称解析失败 → 回退原名称（可显式传 data.hotel_id）。"""
        agent = FakeAgent(self.rt)
        self.rt.agent = agent
        resp = self._run({"scenario": "hotel_full", "place": "布丁酒店(北京西站店)"})
        self.assertEqual(resp.status_code, 200)
        ev = agent.calls[0]
        self.assertEqual(ev.data["hotel_id"], "布丁酒店(北京西站店)")
        # 显式传 hotel_id 优先
        resp2 = self._run({
            "event_type": "booking", "place": "布丁酒店(北京西站店)",
            "data": {"hotel_full": True, "hotel_id": "577984"},
        })
        ev2 = agent.calls[1]
        self.assertEqual(ev2.data["hotel_id"], "577984")

    def test_replanned_response_mapping(self) -> None:
        agent = FakeAgent(self.rt, significant=True, record_replan=True)
        self.rt.agent = agent
        resp = self._run({"scenario": "storm"})
        body = json.loads(resp.content.decode())
        self.assertEqual(body["decision"], "replanned")
        self.assertTrue(body["timeline_changed"])
        self.assertEqual(body["replan"]["id"], "replan-1")
        self.assertIn("diff_summary", body["replan"]["decision"])

    # -- 鉴权 --------------------------------------------------------------

    def test_token_required_when_configured(self) -> None:
        app_settings.debug_inject_token = "sekrit"
        self.rt.agent = FakeAgent(self.rt)
        resp = self._run({"scenario": "storm"})
        self.assertEqual(resp.status_code, 401)
        resp_ok = self._run({"scenario": "storm"}, token="sekrit")
        self.assertEqual(resp_ok.status_code, 200)

    # -- persist_world -----------------------------------------------------

    def test_persist_world_writes_mock_world(self) -> None:
        self.rt.agent = FakeAgent(self.rt)
        self.assertIsNotNone(getattr(self.rt, "world", None))
        resp = self._run({
            "scenario": "storm",
            "persist_world": True,
        })
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            self.rt.world.weather_overrides.get("rain_probability"), 85
        )
        resp2 = self._run({
            "scenario": "queue",
            "place": "故宫",
            "persist_world": True,
        })
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(self.rt.world.get_queue("故宫"), 120)

    # -- 多用户 ------------------------------------------------------------

    def test_persist_world_isolated_per_runtime(self) -> None:
        """多用户：persist_world 只写当前请求所属运行时的 world（另一运行时不可见）。"""
        self.rt.agent = FakeAgent(self.rt)
        other = AgentRuntime()
        resp = self._run({"scenario": "storm", "persist_world": True})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.rt.world.weather_overrides.get("rain_probability"), 85)
        self.assertEqual(other.world.weather_overrides, {})


if __name__ == "__main__":
    unittest.main()
