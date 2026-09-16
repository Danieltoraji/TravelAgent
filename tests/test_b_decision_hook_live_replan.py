"""BDecisionHook live 重规划回归测试（smoke `replans=0` 根因修复，2026-09-15）。

三层根因（验收实锤）：live 城市重规划 ①候选池只读本地假池（降级只决策）、
②hotel_id 解析回退名称、③交通矩阵用本地 spots_graph——真源 id 缺边
ValueError 整条 replan 崩溃，被执行层吞掉 → `/api/replans/` 恒空。

本测试镜像 smoke 清单 3 的决策路径（北京 live 池 + booking 满房事件）：
修复后 hook 应产出 new_timeline 且 diff 含 hotel_changed（换宿成功）；
矩阵构建失败等异常降级「只决策」并被记录（count 不再为 0）。
"""

import os
from datetime import date, datetime
from types import SimpleNamespace

import django
from django.test import SimpleTestCase

django.setup()

from runtime.a_interface import build_decision_hook
from core.schemas import DecisionRequest, TripTimeline, DayPlan, Place, MonitorEvent, EventType


def _tool():
    class _Tool:
        def call(self, name, **kwargs):
            if name == "scenic":
                return {"data": [
                    {"id": "BJ01", "name": "故宫博物院", "alias": ["故宫"],
                     "location": {"lat": 39.916, "lng": 116.397}, "duration": 180,
                     "opening_time": "08:30", "closing_time": "17:00", "price": 60,
                     "tags": ["历史文化"]},
                    {"id": "BJ02", "name": "景山公园", "location": {"lat": 39.923, "lng": 116.397},
                     "duration": 90, "opening_time": "06:30", "closing_time": "21:00",
                     "price": 2, "tags": ["自然"]},
                ]}
            if name == "hotel":
                return {"data": [
                    {"id": "BJ_H001", "name": "皇城景观酒店", "location": {"lat": 39.92, "lng": 116.40},
                     "price_per_night": 300, "star": 4, "rating": 4.5},
                    {"id": "BJ_H002", "name": "另一家酒店", "location": {"lat": 39.90, "lng": 116.41},
                     "price_per_night": 280, "star": 3, "rating": 4.2},
                ]}
            if name == "map":
                if kwargs.get("action") == "batch_route":
                    return {"data": [{"origin": o, "destination": d, "distance_km": 3.0,
                                      "transport_minutes": 25}
                                     for o in kwargs["origins"] for d in kwargs["destinations"]]}
                return {"transport_minutes": 30, "distance_km": 3.0}
            return {"status": "error", "detail": f"no tool {name}"}
    return _Tool()


def _requirement():
    return {"content": {
        "destination": "北京", "start_date": "2026-09-21", "days": 2,
        "visitor_number": 1,
        "constraints": {"budget": 3000, "must_visit": ["故宫"], "daily_travel_time": 480,
                        "required_tags": [], "dismissed_tags": []},
        "preferences": {"preferred_tags": [], "avoid_tags": []},
    }}


def _timeline():
    return TripTimeline(
        id="p", city="北京", start_date=date(2026, 9, 21), end_date=date(2026, 9, 22),
        days=[DayPlan(day=1, date=date(2026, 9, 21),
                      items=[Place(category="scenic", name="故宫博物院", id="BJ01"),
                             Place(category="hotel", name="皇城景观酒店", id="BJ_H001")]),
              DayPlan(day=2, date=date(2026, 9, 22),
                      items=[Place(category="scenic", name="景山公园", id="BJ02")])],
    )


def _booking_event(hotel_id="BJ_H001"):
    return MonitorEvent(
        event_id="bevt-1", event_type=EventType.BOOKING, place="皇城景观酒店",
        observed_at=datetime.now(), rule_name="test", spot_id="",
        data={"hotel_id": hotel_id, "hotel_name": "皇城景观酒店", "hotel_full": True},
    )


class LiveReplanTest(SimpleTestCase):
    def setUp(self):
        os.environ["USE_LIVE_DATA"] = "1"

    def tearDown(self):
        os.environ.pop("USE_LIVE_DATA", None)

    def test_booking_full_event_produces_new_timeline_with_hotel_changed(self):
        """满房事件 → 重规划真跑（live 矩阵）→ new_timeline + hotel_changed。"""
        hook = build_decision_hook(tool_provider=_tool(), requirement=_requirement())
        replan = hook(DecisionRequest(
            events=[_booking_event()], current_timeline=_timeline(),
        ))
        assert replan is not None
        assert replan.new_timeline is not None, replan.reason
        diff = json.dumps(replan.diff_summary, ensure_ascii=False) if replan.diff_summary else ""
        assert "hotel_changed" in diff, replan.diff_summary
        # 换到另一家（原酒店被排除）
        hotels = {
            i.name for day in replan.new_timeline.days for i in day.items
            if i.category == "hotel"
        }
        assert "皇城景观酒店" not in hotels
        assert "另一家酒店" in hotels

    def test_replan_failure_degrades_to_decision_only_but_recorded(self):
        """矩阵构建/重规划异常 → 降级「只决策」并如实记录（replans 不再为空）。"""
        import call_llm.b_decision_hook as bdh

        hook = build_decision_hook(tool_provider=_tool(), requirement=_requirement())

        def _boom(*args, **kwargs):
            raise RuntimeError("replan 故障（测试注入）")

        hook._replan = _boom
        replan = hook(DecisionRequest(
            events=[_booking_event()], current_timeline=_timeline(),
        ))
        assert replan is not None
        assert replan.new_timeline is None
        assert "重规划执行失败" in replan.reason


import json  # noqa: E402  （置于文件尾供上面断言使用）
