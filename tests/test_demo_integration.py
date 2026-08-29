"""B 入口集成测试（3:35 验收：从 B 真实入口提交规划请求，不直接调 A 内部函数）。

链路：``build_planner_hook(requirement, tool_provider)``（与服务器 agent_runtime
相同构造路径）→ ``generate_timeline()`` 内 ``_attach_trip_segments`` →
固定 Demo 场景 锦州→上海 走 ``build_demo_trip_segments``（候选链路）→
``plan["trip_segments"]`` 含 锦州→常州→上海 legs。

断言（03-Demo验收清单 三）：
- 请求进入 A 候选生成流程；返回结构含路线 legs（每段：方式/起终点/站点机场/
  发到时刻/费用/来源）；总时间含等待/转场/缓冲；来源 demo_fixture/mixed；
  同一请求重复运行结果稳定；非 Demo 场景回退原链（不产生 demo 段）。
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"),
           os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runtime.a_interface import build_planner_hook  # noqa: E402
from tools.flight.tools import FlightSearchTool  # noqa: E402
from tools.train.tools import TrainTicketTool  # noqa: E402
from tools.train.trip import TrainTripSkill  # noqa: E402

DATE = "2026-09-01"


class _DemoToolProvider:
    """B 侧 mock 工具门面（train_ticket / train_trip / flight_search）。"""

    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {
            "train_ticket": TrainTicketTool(),
            "train_trip": TrainTripSkill(),
            "flight_search": FlightSearchTool(),
        }

    def call(self, name: str, **kwargs: Any) -> Any:
        return self.tools[name].execute(**kwargs)


def _demo_requirement() -> Dict[str, Any]:
    return {
        "content": {
            "origin": "锦州",
            "destination": "上海",
            "start_date": "2026-09-01",
            "days": 2,
            "visitor_number": 1,
            "preferences": {"travel_priority": "earliest",
                             "preferred_tags": [], "avoid_tags": []},
            "travel_schedule": {
                "departure_date": DATE,
                "departure_time": "08:00",
            },
            "constraints": {
                "budget": 3000,
                "must_visit": [],
                "required_tags": [],
                "dismissed_tags": [],
                "daily_travel_time": 480,
                "include_meal_time_in_daily_limit": False,
            },
        }
    }


class TestDemoIntegrationFromBEntry(unittest.TestCase):
    """从 B 真实入口（build_planner_hook）验证候选链路接入。"""

    def test_demo_chain_attached_from_real_entry(self) -> None:
        tool = _DemoToolProvider()
        hook = build_planner_hook(_demo_requirement(), tool_provider=tool)
        timeline = hook.generate_timeline()
        self.assertIsNone(hook.last_error, hook.last_error)
        segments = (hook._current_plan or {}).get("trip_segments") or []
        self.assertTrue(segments, "Demo 场景必须产出 trip_segments")
        seg = segments[0]
        details = seg["details"]
        self.assertEqual(details["kind"], "outbound")
        self.assertEqual(details["from"], "锦州")
        self.assertEqual(details["to"], "上海")
        self.assertEqual(details["mode"], "联运")
        self.assertEqual(details["source"], "demo_fixture")  # 绝不冒充 live
        chain = " → ".join(details["stops"])  # stops 是 list[str]，直接 join
        self.assertIn("常州", chain)

        # legs：每段含方式/起终点/站点机场/发到时刻/费用/来源（03 验收三）
        legs = details["legs"]
        intercity = [l for l in legs if l["kind"] == "intercity"]
        self.assertEqual(len(intercity), 2)
        self.assertEqual(intercity[0]["mode"], "air")
        self.assertEqual(intercity[0]["service_no"], "KN5621")
        self.assertEqual(intercity[0]["from"], "锦州湾机场")
        self.assertEqual(intercity[0]["to"], "常州奔牛机场")
        self.assertEqual(intercity[0]["depart_datetime"], f"{DATE} 08:00")
        self.assertEqual(intercity[0]["arrive_datetime"], f"{DATE} 09:55")
        self.assertEqual(intercity[1]["mode"], "train")
        self.assertEqual(intercity[1]["service_no"], "G7121")
        self.assertEqual(intercity[1]["depart_datetime"], f"{DATE} 12:30")
        self.assertEqual(intercity[1]["source"], "demo_fixture")
        # 段间 local 转场占位（奔牛机场 → 常州北站）
        local_mid = [l for l in legs if l["kind"] == "local" and "转场" in (l.get("note") or "")]
        self.assertTrue(local_mid)

        # 总时间含等待/转场/缓冲：完整总耗时 442min（值机90+飞115+等待155+车82）
        self.assertEqual(seg["duration_minutes"], 442)
        self.assertEqual(details["cost_per_person"], 315.0)
        self.assertGreaterEqual(details["transfer_wait_min"], 105)  # 满足换乘裕量

    def test_repeat_runs_stable(self) -> None:
        tool = _DemoToolProvider()
        h1 = build_planner_hook(_demo_requirement(), tool_provider=tool)
        h1.generate_timeline()
        h2 = build_planner_hook(_demo_requirement(), tool_provider=tool)
        h2.generate_timeline()
        s1 = (h1._current_plan or {}).get("trip_segments") or []
        s2 = (h2._current_plan or {}).get("trip_segments") or []
        self.assertEqual(
            [(s["details"]["legs"]) for s in s1],
            [(s["details"]["legs"]) for s in s2],
            "同一请求重复运行结果必须稳定",
        )

    def test_non_demo_scenario_falls_back(self) -> None:
        """非固定 Demo 场景：不产出 demo 段（回退原链，不注入候选链路）。"""
        tool = _DemoToolProvider()
        req = _demo_requirement()
        req["content"]["origin"] = "北京"
        req["content"]["destination"] = "乌鲁木齐"
        hook = build_planner_hook(req, tool_provider=tool)
        timeline = hook.generate_timeline()
        segments = (hook._current_plan or {}).get("trip_segments") or []
        # 北京→乌鲁木齐（航空链路）/或空——但绝不该出现 锦州→常州（demo 专属 legs）
        for seg in segments:
            for l in (seg.get("details") or {}).get("legs") or []:
                self.assertNotEqual(l.get("service_no"), "KN5621")


if __name__ == "__main__":
    unittest.main()