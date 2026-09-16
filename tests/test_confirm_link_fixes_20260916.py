"""确认链路三修复回归（2026-09-16，用户实测「确认一直执行中 + 调整计划重复弹」）。

- 修复 1 确认异步化：满房触发的重规划转后台线程（_on_booking_failed 只提交
  不等待），confirm 请求秒级返回；/api/status/ 透出 replan_in_progress；
  booking_confirm 失败响应带 replan_async 信号。
- 修复 2 事件冷却去重：同指纹显著事件在冷却窗内只放行进决策一次
  （poll 全量重发不再反复 replan / 反复打分）。
- 修复 3 满房循环终结：BookingManager 记住确认满房的酒店——同酒店不再
  prepare/不再生成预订动作卡片；重规划回填动作时过滤已知满房酒店。
"""

import json
import os
import sys
import unittest
from datetime import date, datetime
from types import SimpleNamespace

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

from core.schemas import (  # noqa: E402
    BookingStatus,
    DayPlan,
    EventType,
    MonitorEvent,
    Place,
    ReplanRequest,
    TripTimeline,
)
from booking.booking_manager import BookingManager  # noqa: E402
from execution.execution_agent import ExecutionAgent  # noqa: E402
from tools.base_tool import ToolRegistry  # noqa: E402
from tools.booking_tool import BookingTool  # noqa: E402
from tools.scenic_tool import ScenicTool  # noqa: E402
from tools.mock_data import MockWorld  # noqa: E402


def _make_timeline(city: str = "北京") -> TripTimeline:
    return TripTimeline(
        city=city,
        start_date=date(2026, 9, 20),
        end_date=date(2026, 9, 21),
        days=[
            DayPlan(day=1, date=date(2026, 9, 20), items=[
                Place(name="故宫", category="scenic", arrival="09:00"),
            ]),
        ],
    )


def _make_event(event_type: EventType, place: str, data: dict) -> MonitorEvent:
    return MonitorEvent(
        event_id="test-evt",
        event_type=event_type,
        place=place,
        observed_at=datetime.now(),
        rule_name="test",
        data=data,
    )


def _booking_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(BookingTool())
    reg.register(ScenicTool(MockWorld()))
    return reg


# ── 修复 2：事件冷却去重 ──────────────────────────────────────────────


class TestEventCooldown(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = {"now": datetime(2026, 9, 16, 12, 0, 0)}

    def _agent(self, decisions: list) -> ExecutionAgent:
        return ExecutionAgent(
            _make_timeline(),
            decision_hook=lambda req: (decisions.append(req), None)[1],
            now_fn=lambda: self.clock["now"],
        )

    async def test_same_event_within_cooldown_only_one_decision(self) -> None:
        decisions: list = []
        agent = self._agent(decisions)
        evt = _make_event(EventType.WEATHER, "北京", {"rain_probability": 85})
        await agent.handle_event(evt)
        await agent.handle_event(evt)   # poll 全量重发的同一事件
        self.assertEqual(len(decisions), 1)

    async def test_cooldown_expiry_allows_decision_again(self) -> None:
        decisions: list = []
        agent = self._agent(decisions)
        evt = _make_event(EventType.WEATHER, "北京", {"rain_probability": 85})
        await agent.handle_event(evt)
        self.clock["now"] = datetime(2026, 9, 16, 12, 10, 1)   # 601s 后出窗
        await agent.handle_event(evt)
        self.assertEqual(len(decisions), 2)

    async def test_different_place_not_cooled(self) -> None:
        decisions: list = []
        agent = self._agent(decisions)
        await agent.handle_event(_make_event(EventType.WEATHER, "北京", {"rain_probability": 85}))
        await agent.handle_event(_make_event(EventType.WEATHER, "上海", {"rain_probability": 85}))
        self.assertEqual(len(decisions), 2)

    async def test_decision_none_still_counts_as_handled(self) -> None:
        """decision 返回 None（不重规划）也算已处理——重复打分同样被冷却挡住。"""
        calls: list = []

        def hook(req):
            calls.append(req)
            return None

        agent = ExecutionAgent(
            _make_timeline(), decision_hook=hook,
            now_fn=lambda: self.clock["now"],
        )
        evt = _make_event(EventType.WEATHER, "北京", {"rain_probability": 85})
        await agent.handle_event(evt)
        await agent.handle_event(evt)
        self.assertEqual(len(calls), 1)

    async def test_insufficient_event_not_cooled_later(self) -> None:
        """未达阈值的事件（rain<60）不记指纹——之后变强降雨仍正常触发。"""
        decisions: list = []
        agent = self._agent(decisions)
        await agent.handle_event(_make_event(EventType.WEATHER, "北京", {"rain_probability": 30}))
        await agent.handle_event(_make_event(EventType.WEATHER, "北京", {"rain_probability": 85}))
        self.assertEqual(len(decisions), 1)


# ── 修复 3：满房循环终结 ──────────────────────────────────────────────


class TestFailedHotelLoopTerminator(unittest.TestCase):
    def setUp(self) -> None:
        self.bm = BookingManager(_booking_registry())

    def _fail_hotel(self, place: str) -> None:
        rec = self.bm.prepare(place, target_date="2026-09-20",
                              party_size=2, booking_type="hotel")
        with self.assertRaises(RuntimeError):
            self.bm.confirm(rec.booking_id)

    def test_failed_hotel_recorded_and_blocked(self) -> None:
        self._fail_hotel("皇城景观酒店（满房）")
        self.assertTrue(self.bm.is_hotel_failed("皇城景观酒店"))
        self.assertFalse(self.bm.is_hotel_failed("另一家酒店"))

    def test_same_hotel_prepare_rejected(self) -> None:
        self._fail_hotel("皇城景观酒店（满房）")
        with self.assertRaises(RuntimeError):
            self.bm.prepare("皇城景观酒店", target_date="2026-09-20",
                            party_size=2, booking_type="hotel")
        # 其它酒店不受影响
        rec = self.bm.prepare("另一家酒店", target_date="2026-09-20",
                              party_size=2, booking_type="hotel")
        self.assertEqual(rec.status, BookingStatus.PENDING_CONFIRM)

    def test_hotel_key_normalization(self) -> None:
        """归一化键剥离「满房」标记（口径同 runtime._resolve_hotel_id）。"""
        self.assertEqual(BookingManager._hotel_key("皇城景观酒店（满房）"), "皇城景观酒店")
        self.assertEqual(BookingManager._hotel_key("皇城景观酒店满房"), "皇城景观酒店")
        self.assertEqual(BookingManager._hotel_key(" 皇城景观酒店 "), "皇城景观酒店")

    def test_snapshot_roundtrip_keeps_failed_hotels(self) -> None:
        self._fail_hotel("皇城景观酒店（满房）")
        restored = BookingManager(_booking_registry())
        restored.restore_snapshot(self.bm.snapshot())
        self.assertTrue(restored.is_hotel_failed("皇城景观酒店"))


# ── 修复 3 + 0827 回填联动：重规划动作过滤已知满房酒店 ─────────────────


class TestReplanActionFilter(unittest.TestCase):
    def test_full_hotel_action_filtered_on_enqueue(self) -> None:
        from runtime.agent_runtime import AgentRuntime

        rt = AgentRuntime()
        rt.booking_manager._failed_hotels.add("皇城酒店")
        req = SimpleNamespace(
            events=[_make_event(EventType.BOOKING, "皇城酒店",
                                {"hotel_id": "h1", "hotel_full": True})],
            current_timeline=_make_timeline(),
            context={},
        )
        replan = ReplanRequest(
            new_timeline=_make_timeline(),
            reason="满房换宿",
            diff_summary=[
                "[hotel_changed] 皇城酒店（已满房）",
                "[hotel_changed] 新酒店（换宿）",
            ],
        )
        rt._record_decision(req, replan)
        targets = [a.target for a in rt.booking_manager.actions()]
        self.assertIn("hotel:新酒店", targets)
        self.assertNotIn("hotel:皇城酒店", targets)   # 循环源：不再重复弹卡


# ── 修复 1：确认异步化 ────────────────────────────────────────────────


class TestBackgroundReplan(unittest.TestCase):
    def setUp(self) -> None:
        from runtime.agent_runtime import AgentRuntime

        self.rt = AgentRuntime()
        self.decisions: list = []

        def hook(req):
            self.decisions.append(req)
            return ReplanRequest(
                new_timeline=_make_timeline(), reason="满房换宿",
                diff_summary=["[hotel_changed] 新酒店（换宿）"],
            )

        self.agent = ExecutionAgent(
            _make_timeline(), decision_hook=hook,
            now_fn=lambda: datetime(2026, 9, 16, 12, 0, 0),
        )
        self.rt.timeline = self.agent.timeline
        self.rt.agent = self.agent

    def test_on_booking_failed_schedules_and_applies(self) -> None:
        """_on_booking_failed 只提交不等待；后台重规划生效且状态收口。"""
        captured_in_progress: list = []

        class _SyncExecutor:
            """submit 即同步执行（测试用：跳过真线程）。"""

            def submit(self, fn, *args) -> None:
                captured_in_progress.append(self_rt.replan_in_progress)
                fn(*args)

        self_rt = self.rt
        self.rt._replan_executor = _SyncExecutor()
        record = SimpleNamespace(booking_id="BK1", place="满房酒店")
        self.rt._on_booking_failed(record)
        # 提交瞬间 replan_in_progress 已置位（C 端 /api/status/ 可观测）
        self.assertEqual(captured_in_progress[0]["event_type"], "booking")
        # 后台执行完成：决策进来了、状态收口、轨迹落 last_plan_trace
        self.assertEqual(len(self.decisions), 1)
        self.assertIsNone(self.rt.replan_in_progress)
        self.assertIsNotNone(self.rt.last_plan_trace)
        self.assertEqual(self.rt.last_plan_trace["plan_meta"]["kind"], "booking_replan")

    def test_background_replan_without_agent_is_safe(self) -> None:
        rt = self.rt
        rt.agent = None
        rt._run_background_replan(
            _make_event(EventType.BOOKING, "满房酒店", {"hotel_full": True})
        )
        self.assertIsNone(rt.replan_in_progress)


# ── 修复 1：booking_confirm 视图失败响应带 replan_async 信号 ──────────


class TestBookingConfirmAsyncFields(unittest.TestCase):
    def setUp(self) -> None:
        import django
        from django.conf import settings

        if not settings.configured:
            settings.configure(
                DEBUG=True, ALLOWED_HOSTS=["*"], DATABASES={},
                INSTALLED_APPS=[], ROOT_URLCONF=None,
            )
            django.setup()
        from api import views
        from runtime.agent_runtime import AgentRuntime

        self.views = views
        self.rt = AgentRuntime()

    def test_confirm_failure_response_has_replan_async(self) -> None:
        from django.http import HttpRequest

        rec = self.rt.booking_manager.prepare(
            place="皇城景观酒店（满房）", target_date="2026-09-20",
            party_size=2, booking_type="hotel",
        )
        req = HttpRequest()
        req.method = "POST"
        req.runtime = self.rt
        resp = self.views.booking_confirm(req, rec.booking_id)
        body = json.loads(resp.content.decode("utf-8"))
        self.assertEqual(resp.status_code, 400)
        self.assertTrue(body.get("replan_async"))
        # 满房已记账（修复 3）：同酒店不再可 prepare
        self.assertTrue(self.rt.booking_manager.is_hotel_failed("皇城景观酒店"))


if __name__ == "__main__":
    unittest.main()
