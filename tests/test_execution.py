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


class TestSignificance(unittest.TestCase):
    def test_rain_triggers_decision(self) -> None:
        decisions: list = []
        agent = ExecutionAgent(make_timeline(), decision_hook=lambda req: decisions.append(req))
        req = agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 85}))
        self.assertIsInstance(req, DecisionRequest)
        self.assertEqual(len(decisions), 1)

    def test_mild_rain_no_decision(self) -> None:
        agent = ExecutionAgent(make_timeline())
        req = agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 20}))
        self.assertIsNone(req)

    def test_queue_over_threshold_triggers(self) -> None:
        decisions: list = []
        agent = ExecutionAgent(make_timeline(), decision_hook=lambda req: decisions.append(req))
        req = agent.handle_event(make_event(EventType.SCENIC, {"queue_min": 120}))
        self.assertIsInstance(req, DecisionRequest)

    def test_traffic_delay_triggers(self) -> None:
        agent = ExecutionAgent(make_timeline())
        req = agent.handle_event(make_event(EventType.TRAFFIC, {"delay_min": 45}))
        self.assertIsInstance(req, DecisionRequest)


class TestLookahead(unittest.TestCase):
    def test_fires_when_within_window(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        world = MockWorld()
        agent = ExecutionAgent(make_timeline(), registry=build_registry(world))
        # 故宫 09:00 到达，提前 20 分钟 = 08:40；当前 08:45 应触发
        events = agent.check_lookahead(datetime(2026, 8, 1, 8, 45))
        places = [e.place for e in events]
        self.assertIn("故宫", places)

    def test_does_not_fire_before_window(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        world = MockWorld()
        agent = ExecutionAgent(make_timeline(), registry=build_registry(world))
        events = agent.check_lookahead(datetime(2026, 8, 1, 8, 0))   # 早于 08:40
        self.assertEqual(events, [])


class TestReplanLoopback(unittest.TestCase):
    def test_replan_replaces_timeline_and_rebuilds_rules(self) -> None:
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
        agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 85}))

        # 断言时间轴已替换、规则已重建（lookahead 规则数应 +1）
        self.assertIs(agent.timeline, new_timeline)
        self.assertEqual(len(agent.lookahead_rules), original_rules_count + 1)
        # 新规则名应包含"天安门"
        names = [r.name for r in agent.lookahead_rules]
        self.assertIn("scenic-天安门", names)

    def test_replan_without_new_timeline_is_noop(self) -> None:
        from core.schemas import ReplanRequest
        from tools import build_registry
        from tools.mock_data import MockWorld

        agent = ExecutionAgent(make_timeline(), registry=build_registry(MockWorld()))
        original_timeline = agent.timeline
        replan = ReplanRequest(new_timeline=None, reason="影响可忽略")

        def hook(_req: DecisionRequest) -> ReplanRequest:
            return replan

        agent.decision_hook = hook
        agent.handle_event(make_event(EventType.WEATHER, {"rain_probability": 85}))
        self.assertIs(agent.timeline, original_timeline)  # 未替换


if __name__ == "__main__":
    unittest.main()
