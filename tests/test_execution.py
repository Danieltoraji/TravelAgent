"""Execution Agent 测试：规则构建、影响判定、到达前触发。"""

import unittest
from datetime import date, datetime

from core.schemas import (
    DayPlan,
    DecisionRequest,
    EventType,
    MonitorEvent,
    Place,
    TripTimeline,
)
from execution.execution_agent import ExecutionAgent


def make_timeline() -> TripTimeline:
    return TripTimeline(
        city="北京",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        days=[
            DayPlan(day=1, date=date(2026, 8, 1), items=[
                Place(name="故宫", category="scenic", arrival="09:00"),
                Place(name="景山公园", category="scenic", arrival="14:00"),
                Place(name="全聚德(前门店)", category="food", arrival="18:00"),
            ]),
        ],
    )


def make_event(event_type: EventType, data) -> MonitorEvent:
    return MonitorEvent(
        event_id="test-1",
        event_type=event_type,
        place="北京",
        observed_at=datetime.now(),
        rule_name="test",
        data=data,
    )


class TestBuildRules(unittest.TestCase):
    def test_periodic_and_lookahead_rules(self) -> None:
        agent = ExecutionAgent(make_timeline())
        self.assertEqual(len(agent.periodic_rules), 2)          # 天气 + 交通
        self.assertEqual(len(agent.lookahead_rules), 3)         # 2 景点 + 1 餐饮
        names = [r.name for r in agent.lookahead_rules]
        self.assertIn("scenic-故宫", names)
        self.assertIn("food-全聚德(前门店)", names)


class TestSignificance(unittest.IsolatedAsyncioTestCase):
    async def test_rain_triggers_decision(self) -> None:
        decisions: list = []
        agent = ExecutionAgent(make_timeline(), decision_hook=lambda req: decisions.append(req))
        req = await agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 85}))
        self.assertIsInstance(req, DecisionRequest)
        self.assertEqual(len(decisions), 1)

    async def test_mild_rain_no_decision(self) -> None:
        agent = ExecutionAgent(make_timeline())
        req = await agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 20}))
        self.assertIsNone(req)

    async def test_queue_over_threshold_triggers(self) -> None:
        decisions: list = []
        agent = ExecutionAgent(make_timeline(), decision_hook=lambda req: decisions.append(req))
        req = await agent.handle_event(make_event(EventType.SCENIC, {"queue_min": 120}))
        self.assertIsInstance(req, DecisionRequest)

    async def test_traffic_delay_triggers(self) -> None:
        agent = ExecutionAgent(make_timeline())
        req = await agent.handle_event(make_event(EventType.TRAFFIC, {"delay_min": 45}))
        self.assertIsInstance(req, DecisionRequest)


class TestLookahead(unittest.IsolatedAsyncioTestCase):
    async def test_fires_when_within_window(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        world = MockWorld()
        agent = ExecutionAgent(make_timeline(), registry=build_registry(world))
        # 故宫 09:00 到达，提前 20 分钟 = 08:40；当前 08:45 应触发
        events = await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        places = [e.place for e in events]
        self.assertIn("故宫", places)

    async def test_does_not_fire_before_window(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        world = MockWorld()
        agent = ExecutionAgent(make_timeline(), registry=build_registry(world))
        events = await agent.check_lookahead(datetime(2026, 8, 1, 8, 0))   # 早于 08:40
        self.assertEqual(events, [])


class TestReplanLoopback(unittest.IsolatedAsyncioTestCase):
    async def test_replan_replaces_timeline_and_rebuilds_rules(self) -> None:
        from core.schemas import ReplanRequest
        from tools import build_registry
        from tools.mock_data import MockWorld

        world = MockWorld()
        agent = ExecutionAgent(make_timeline(), registry=build_registry(world))
        original_rules_count = len(agent.lookahead_rules)

        # A 返回一份新时间轴（多加一个景点）
        new_timeline = make_timeline()
        new_timeline.days[0].items.append(
            Place(name="天安门", category="scenic", arrival="11:00")
        )
        replan = ReplanRequest(
            new_timeline=new_timeline,
            reason="故宫排队120分钟，调整上午行程",
            diff_summary=["新增 天安门 11:00"],
        )

        def hook(_req: DecisionRequest) -> ReplanRequest:
            return replan

        agent.decision_hook = hook
        # 触发一次显著事件（暴雨）让 handle_event 调用 hook
        await agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 85}))

        # 断言时间轴已替换、规则已重建（lookahead 规则数应 +1）
        self.assertIs(agent.timeline, new_timeline)
        self.assertEqual(len(agent.lookahead_rules), original_rules_count + 1)
        # 新规则名应包含"天安门"
        names = [r.name for r in agent.lookahead_rules]
        self.assertIn("scenic-天安门", names)

    async def test_replan_without_new_timeline_is_noop(self) -> None:
        from core.schemas import ReplanRequest
        from tools import build_registry
        from tools.mock_data import MockWorld

        agent = ExecutionAgent(make_timeline(), registry=build_registry(MockWorld()))
        original_timeline = agent.timeline
        replan = ReplanRequest(new_timeline=None, reason="影响可忽略")

        def hook(_req: DecisionRequest) -> ReplanRequest:
            return replan

        agent.decision_hook = hook
        await agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 85}))
        self.assertIs(agent.timeline, original_timeline)  # 未替换


class TestAutoBooking(unittest.IsolatedAsyncioTestCase):
    """自动预约集成测试：ExecutionAgent ↔ BookingManager。"""

    def _make_timeline_with_ticket(self) -> TripTimeline:
        """故宫 ticket_required=True，景山 ticket_required=False。"""
        return TripTimeline(
            city="北京",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 1),
            days=[
                DayPlan(day=1, date=date(2026, 8, 1), items=[
                    Place(name="故宫", category="scenic", arrival="09:00",
                          ticket_required=True, price=60.0),
                    Place(name="景山公园", category="scenic", arrival="14:00",
                          ticket_required=False),
                    Place(name="全聚德(前门店)", category="food", arrival="18:00"),
                ]),
            ],
        )

    def _make_agent_with_bm(self, timeline: TripTimeline):
        from booking.booking_manager import BookingManager
        from tools import build_registry
        from tools.mock_data import MockWorld
        world = MockWorld()
        registry = build_registry(world)
        bm = BookingManager(registry)
        agent = ExecutionAgent(
            timeline=timeline,
            registry=registry,
            booking_manager=bm,
        )
        return agent, bm

    async def test_auto_book_scenic_with_ticket(self) -> None:
        """ticket_required=True 的景点 → 自动预约，ActionItem 产出。"""
        agent, bm = self._make_agent_with_bm(self._make_timeline_with_ticket())
        # 故宫 09:00 到达，提前 20min = 08:40；08:45 触发
        await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        records = bm.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0].place, "故宫")
        self.assertEqual(records[0].booking_type, "scenic")
        self.assertIn("故宫", agent._booked_places)
        # ActionItem 应已产出
        actions = bm.actions()
        self.assertTrue(any("故宫" in a.title for a in actions))
        # E6：预约状态回填到时间轴 Place
        gugong = agent.timeline.days[0].items[0]
        self.assertEqual(gugong.booking_id, records[0].booking_id)
        self.assertEqual(gugong.booking_status, "pending_confirm")

    async def test_auto_book_scenic_without_ticket(self) -> None:
        """ticket_required=False 的景点 → 不预约。"""
        agent, bm = self._make_agent_with_bm(self._make_timeline_with_ticket())
        # 景山 14:00 到达，提前 20min = 13:40；13:45 触发
        await agent.check_lookahead(datetime(2026, 8, 1, 13, 45))
        # 景山不应被预约（ticket_required=False）
        records = bm.records()
        places = [r.place for r in records]
        self.assertNotIn("景山公园", places)
        self.assertNotIn("景山公园", agent._booked_places)

    async def test_auto_book_food(self) -> None:
        """餐厅 → 自动预约（food 类型）。"""
        agent, bm = self._make_agent_with_bm(self._make_timeline_with_ticket())
        # 先触发故宫（08:45），再触发全聚德（17:35）
        await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        await agent.check_lookahead(datetime(2026, 8, 1, 17, 35))
        records = bm.records()
        # 故宫 + 全聚德 都应被预约
        self.assertEqual(len(records), 2)
        food_recs = [r for r in records if r.booking_type == "food"]
        self.assertEqual(len(food_recs), 1)
        self.assertEqual(food_recs[0].place, "全聚德(前门店)")
        self.assertIn("全聚德(前门店)", agent._booked_places)

    async def test_no_duplicate_booking(self) -> None:
        """同一地点触发两次 → 只预约一次。"""
        agent, bm = self._make_agent_with_bm(self._make_timeline_with_ticket())
        # 第一次触发
        await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        self.assertEqual(len(bm.records()), 1)
        # 第二次触发（时间更晚，但 rule.fired=True 不会再次触发）
        await agent.check_lookahead(datetime(2026, 8, 1, 9, 0))
        self.assertEqual(len(bm.records()), 1)

    async def test_no_booking_manager_noop(self) -> None:
        """booking_manager=None → 不报错，不预约。"""
        from tools import build_registry
        from tools.mock_data import MockWorld
        agent = ExecutionAgent(
            timeline=self._make_timeline_with_ticket(),
            registry=build_registry(MockWorld()),
            # booking_manager 不传
        )
        # 应不报错
        await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        self.assertEqual(len(agent._booked_places), 0)

    async def test_replan_clears_booked_places(self) -> None:
        """重规划后 _booked_places 保留仍在新 timeline 中的地点（防重复预约），
        新地点可预约，被移除的地点从 _booked_places 中清除。"""
        from core.schemas import ReplanRequest
        agent, bm = self._make_agent_with_bm(self._make_timeline_with_ticket())
        # 先触发故宫预约
        await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        self.assertIn("故宫", agent._booked_places)
        self.assertEqual(len(bm.records()), 1)

        # 重规划：新 timeline 加一个新景点（故宫仍在）
        new_timeline = self._make_timeline_with_ticket()
        new_timeline.days[0].items.append(
            Place(name="天坛", category="scenic", arrival="09:00",
                  ticket_required=True, price=15.0)
        )
        replan = ReplanRequest(
            new_timeline=new_timeline,
            reason="测试重规划",
            diff_summary=["新增 天坛"],
        )
        agent.apply_replan(replan)

        # 故宫仍在 _booked_places 中（防重复预约）
        self.assertIn("故宫", agent._booked_places)
        # _place_info 应包含新地点
        self.assertIn("天坛", agent._place_info)

        # 触发新地点的 lookahead（天坛 09:00，提前 20min = 08:40）
        await agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        # 天坛应被预约
        places = [r.place for r in bm.records()]
        self.assertIn("天坛", places)
        # 故宫不应被重复预约（仍在 _booked_places 中）
        gu_gong_count = sum(1 for p in places if p == "故宫")
        self.assertEqual(gu_gong_count, 1)


if __name__ == "__main__":
    unittest.main()
