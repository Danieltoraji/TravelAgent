"""服务器核对 8.27 两项修复的回归测试（A-2 队列 / 事件去重，B 仓库侧）：

1) ``_replan_to_actions``：ReplanRequest（diff_summary）→ Action Queue 条目
   （更新路线 auto + 换宿预订 confirm），无新时间轴/None → 空；
2) 满房 confirm 失败 → BOOKING 事件只缓冲一次（不再重复，修复 ①）；
3) decision_hook 返回 ReplanRequest → handle_event 后动作入队（修复 ②）。
"""

import os
import sys
import unittest
from datetime import date, datetime

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 与 test_a_interface.py 相同：django_server（runtime 包）+ a_side + B 根
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.schemas import (  # noqa: E402
    DayPlan,
    EventType,
    MonitorEvent,
    PermissionLevel,
    Place,
    ReplanRequest,
    TripTimeline,
)
from execution.execution_agent import ExecutionAgent  # noqa: E402
from runtime.agent_runtime import AgentRuntime, _replan_to_actions  # noqa: E402


def make_timeline() -> TripTimeline:
    return TripTimeline(
        city="北京",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        days=[DayPlan(day=1, date=date(2026, 8, 1), items=[
            Place(name="故宫", category="scenic", arrival="09:00"),
            Place(name="午餐", category="food", arrival="12:00"),
        ])],
    )


def make_booking_event() -> MonitorEvent:
    return MonitorEvent(
        event_id="bevt-TEST",
        event_type=EventType.BOOKING,
        place="皇城景观酒店",
        observed_at=datetime.now(),
        rule_name="booking-confirm",
        data={"hotel_id": "BJ_H001", "hotel_name": "皇城景观酒店", "hotel_full": True},
    )


class TestReplanToActions(unittest.TestCase):
    """修复 ② 的纯函数层：diff_summary → Action Queue 条目。"""

    def test_none_or_no_timeline_returns_empty(self) -> None:
        self.assertEqual(_replan_to_actions(None), [])
        self.assertEqual(
            _replan_to_actions(ReplanRequest(new_timeline=None, reason="不可行")), []
        )

    def test_new_timeline_produces_route_update_auto(self) -> None:
        replan = ReplanRequest(
            new_timeline=make_timeline(),
            reason="故宫排队 120 分钟",
            diff_summary=[],
        )
        items = _replan_to_actions(replan)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "更新路线")
        self.assertEqual(items[0].permission, PermissionLevel.AUTO)
        self.assertEqual(items[0].type, "ROUTE_UPDATE")

    def test_hotel_changed_produces_confirm_booking(self) -> None:
        replan = ReplanRequest(
            new_timeline=make_timeline(),
            reason="酒店 BJ_H001 满房",
            diff_summary=["[hotel_changed] 海淀园林商务酒店（酒店 BJ_H001 满房，从候选池排除（硬不可行））"],
        )
        items = _replan_to_actions(replan)
        titles = [(i.title, i.permission, i.type) for i in items]
        self.assertIn(("更新路线", PermissionLevel.AUTO, "ROUTE_UPDATE"), titles)
        self.assertIn(("预订海淀园林商务酒店", PermissionLevel.CONFIRM, "HOTEL_BOOK"), titles)


class TestBookingEventDedup(unittest.TestCase):
    """修复 ①：满房 confirm 失败后 BOOKING 事件只缓冲一次。"""

    def test_booking_failure_buffers_event_once(self) -> None:
        rt = AgentRuntime()
        rt.timeline = make_timeline()
        rt.agent = ExecutionAgent(
            timeline=rt.timeline,
            registry=rt.registry,
            on_event=rt._on_event,          # 与生产一致：handle_event 内部回调缓冲
        )
        rec = rt.booking_manager.prepare(
            place="皇城景观酒店（满房）", target_date="2026-08-01",
            party_size=2, booking_type="hotel",
        )
        with self.assertRaises(RuntimeError):
            rt.booking_manager.confirm(rec.booking_id)
        n_booking = sum(1 for e in rt.events if e.event_type == EventType.BOOKING)
        self.assertEqual(n_booking, 1, f"/api/events 不应出现重复 BOOKING，实际 {n_booking} 条")

    def test_enqueue_actions_dedup_by_id(self) -> None:
        rt = AgentRuntime()
        a = _replan_to_actions(ReplanRequest(new_timeline=make_timeline(), reason="满房"))
        b = _replan_to_actions(ReplanRequest(new_timeline=make_timeline(), reason="满房"))
        rt.booking_manager.enqueue_actions(a)
        rt.booking_manager.enqueue_actions(b)   # action_id 不同 → 各自保留
        self.assertEqual(len(rt.booking_manager.actions()), len(a) + len(b))
        # 重复同一条（相同 action_id）不叠加
        rt.booking_manager.enqueue_actions(a)
        self.assertEqual(len(rt.booking_manager.actions()), len(a) + len(b))

    def test_tool_call_log_hides_non_readonly_data(self) -> None:
        rt = AgentRuntime()
        rt.registry.call("weather", city="北京")
        rt.registry.call("booking", action="prepare", place="故宫")

        weather_entry = next(item for item in rt.tool_call_log if item["tool"] == "weather")
        booking_entry = next(item for item in rt.tool_call_log if item["tool"] == "booking")

        self.assertTrue(weather_entry["has_data"])
        self.assertIsNotNone(weather_entry["data"])
        self.assertFalse(booking_entry["has_data"])
        self.assertIsNone(booking_entry["data"])

    def test_hotel_detail_call_updates_cache(self) -> None:
        rt = AgentRuntime()
        result = rt.registry.call("hotel", action="detail", hotelId="H001")
        self.assertEqual(result.status.value, "ok")
        self.assertIn("H001", rt.hotel_details)


class TestReplanActionsQueue(unittest.IsolatedAsyncioTestCase):
    """修复 ② 集成层：决策 hook 返回 ReplanRequest → 动作真实进入队列。

    真实链路：ExecutionAgent.handle_event → decision_hook（= AgentRuntime
    ``_get_decision_hook`` 的包装闭包，内部先调 A 侧 hook 再 ``_record_decision``）
    → enqueue。这里 monkeypatch 模块级 ``build_decision_hook`` 注入假 hook。
    """

    async def test_replan_enqueues_actions_via_handle_event(self) -> None:
        import runtime.agent_runtime as ar

        rt = AgentRuntime()
        rt.timeline = make_timeline()

        def fake_raw_hook(tool_provider=None):
            def hook(_req):  # 模拟 A 侧 Decision Engine：满房硬规则 → 换酒店
                return ReplanRequest(
                    new_timeline=make_timeline(),
                    reason="皇城景观酒店 满房，必须重新规划",
                    diff_summary=["[hotel_changed] 海淀园林商务酒店（酒店 BJ_H001 满房）"],
                )
            return hook

        original = ar.build_decision_hook
        ar.build_decision_hook = fake_raw_hook
        try:
            rt.agent = ExecutionAgent(
                timeline=rt.timeline,
                registry=rt.registry,
                on_event=rt._on_event,
                booking_manager=rt.booking_manager,
            )
            rt.agent.decision_hook = rt._get_decision_hook()   # 真实包装闭包（含 _record_decision）
            await rt.agent.handle_event(make_booking_event())
        finally:
            ar.build_decision_hook = original

        titles = {(a.title, a.permission) for a in rt.booking_manager.actions()}
        self.assertIn(("更新路线", PermissionLevel.AUTO), titles)
        self.assertIn(("预订海淀园林商务酒店", PermissionLevel.CONFIRM), titles)


if __name__ == "__main__":
    unittest.main()