"""Decision Engine 测试：影响评分、Replan 触发、时间轴修改。"""

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
from decision.decision_engine import DecisionEngine, IMPACT_SCORES


def make_event(event_type: EventType, data, place: str = "北京") -> MonitorEvent:
    return MonitorEvent(
        event_id="t1",
        event_type=event_type,
        place=place,
        observed_at=datetime.now(),
        rule_name="test",
        data=data,
    )


def make_timeline() -> TripTimeline:
    return TripTimeline(
        city="北京",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        days=[
            DayPlan(day=1, date=date(2026, 8, 1), items=[
                Place(name="故宫", category="scenic", arrival="09:00", queue_min=20),
                Place(name="景山公园", category="scenic", arrival="14:00", queue_min=5),
            ]),
        ],
    )


class TestScoring(unittest.TestCase):
    def test_weather_score_is_40(self):
        engine = DecisionEngine()
        req = DecisionRequest(
            events=[make_event(EventType.WEATHER, {"rain_probability": 85})],
            current_timeline=make_timeline(),
        )
        self.assertEqual(engine._score(req.events), IMPACT_SCORES[EventType.WEATHER])

    def test_scenic_score_is_80(self):
        engine = DecisionEngine()
        req = DecisionRequest(
            events=[make_event(EventType.SCENIC, {"queue_min": 120}, place="故宫")],
            current_timeline=make_timeline(),
        )
        self.assertEqual(engine._score(req.events), IMPACT_SCORES[EventType.SCENIC])

    def test_multiple_events_sum(self):
        engine = DecisionEngine()
        req = DecisionRequest(
            events=[
                make_event(EventType.WEATHER, {"rain_probability": 85}),
                make_event(EventType.TRAFFIC, {"delay_min": 45}),
            ],
            current_timeline=make_timeline(),
        )
        expected = IMPACT_SCORES[EventType.WEATHER] + IMPACT_SCORES[EventType.TRAFFIC]
        self.assertEqual(engine._score(req.events), expected)


class TestDecision(unittest.TestCase):
    def test_low_score_no_replan(self):
        # 交通延误：分=20 < 阈值50 → 不重规划
        engine = DecisionEngine(impact_threshold=50)
        req = DecisionRequest(
            events=[make_event(EventType.TRAFFIC, {"delay_min": 45})],
            current_timeline=make_timeline(),
        )
        result = engine(req)
        self.assertIsNone(result)
        self.assertEqual(engine.history, [None])

    def test_high_score_triggers_replan(self):
        # 景点排队：分=80 > 阈值50 → 重规划
        engine = DecisionEngine(impact_threshold=50)
        req = DecisionRequest(
            events=[make_event(EventType.SCENIC, {"queue_min": 120}, place="故宫")],
            current_timeline=make_timeline(),
        )
        result = engine(req)
        self.assertIsNotNone(result)
        self.assertIsInstance(result, ReplanRequest := __import__(
            "core.schemas", fromlist=["ReplanRequest"]).ReplanRequest)
        self.assertIsNotNone(result.new_timeline)
        self.assertTrue(len(result.diff_summary) > 0)

    def test_replan_moves_scenic_to_afternoon(self):
        engine = DecisionEngine(impact_threshold=50)
        req = DecisionRequest(
            events=[make_event(EventType.SCENIC, {"queue_min": 120}, place="故宫")],
            current_timeline=make_timeline(),
        )
        result = engine(req)
        self.assertIsNotNone(result)
        # 故宫应被挪到 14:00
        for day in result.new_timeline.days:
            for item in day.items:
                if item.name == "故宫":
                    self.assertEqual(item.arrival, "14:00")

    def test_replan_weather_marks_outdoor(self):
        engine = DecisionEngine(impact_threshold=30)  # 天气分40 > 30 → 触发
        req = DecisionRequest(
            events=[make_event(EventType.WEATHER, {"rain_probability": 85, "condition": "暴雨"})],
            current_timeline=make_timeline(),
        )
        result = engine(req)
        self.assertIsNotNone(result)
        self.assertIn("暴雨", result.reason)
        # 户外景点 open_time 应含备注
        found_note = False
        for day in result.new_timeline.days:
            for item in day.items:
                if item.category == "scenic" and "暴雨" in item.open_time:
                    found_note = True
        self.assertTrue(found_note)

    def test_history_records_decisions(self):
        engine = DecisionEngine(impact_threshold=50)
        # 低分事件
        engine(DecisionRequest(
            events=[make_event(EventType.TRAFFIC, {"delay_min": 10})],
            current_timeline=make_timeline(),
        ))
        # 高分事件
        engine(DecisionRequest(
            events=[make_event(EventType.SCENIC, {"queue_min": 120}, place="故宫")],
            current_timeline=make_timeline(),
        ))
        self.assertEqual(len(engine.history), 2)
        self.assertIsNone(engine.history[0])
        self.assertIsNotNone(engine.history[1])


if __name__ == "__main__":
    unittest.main()
