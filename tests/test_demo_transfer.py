"""阶段 2 目标测试：班次一致性与换乘可行性（2:25~3:05）。

验收口径（02-四小时修复与Demo执行计划.md §2:25～3:05 完成标准）：
- 最终结果中的时长、价格、车站、车次来自**同一组具体班次**（I-05，阶段 1 已保证）；
- **换乘校验**：飞→火 = 航班到达 + 出机场 30min + 转场 + 进站 30min；
  火→飞 = 火车到达 + 出站 20min + 转场 + 提前 1.5h 到机场；
  火车发车改早 → 候选被淘汰（feasible=False + reject_reason）；
- 完整总耗时 = 各段运行 + 等待 + 转场 + 缓冲（I-11），12h 按完整总耗时过滤；
- 来源按 legs 聚合 live / mixed / demo_fixture（I-12）；Demo 样例绝不标成 live；
- 结果按完整班次组合排序，不拼接字段。

全部用例走 B 侧 mock 工具 / local fixture，**禁真实网络**。
"""

from __future__ import annotations

import os
import sys
import unittest
from typing import Any, Dict, List, Optional

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data_transmission.air_routes import load_air_routes  # noqa: E402
from data_transmission.demo_candidate import (  # noqa: E402
    AIR_ARRIVE_BUFFER_MIN,
    AIR_CHECKIN_BUFFER_MIN,
    DEFAULT_TRANSFER_MIN,
    RAIL_ARRIVE_BUFFER_MIN,
    RAIL_CHECKIN_BUFFER_MIN,
    TransferCheck,
    build_demo_candidates,
    check_leg_connection,
    lookup_transfer_minutes,
    make_demo_flight_provider,
    make_demo_train_provider,
)
from tools.flight.tools import FlightSearchTool  # noqa: E402
from tools.train.tools import TrainTicketTool, _demo_train_rows  # noqa: E402
from tools.train.trip import TrainTripSkill  # noqa: E402

DATE = "2026-09-01"


class _FakeToolProvider:
    """B 侧 mock 工具门面：train_ticket / train_trip / flight_search。"""

    def __init__(self) -> None:
        self.tools: Dict[str, Any] = {
            "train_ticket": TrainTicketTool(),
            "train_trip": TrainTripSkill(),
            "flight_search": FlightSearchTool(),
        }

    def call(self, name: str, **kwargs: Any) -> Any:
        return self.tools[name].execute(**kwargs)


class TestTransferConstants(unittest.TestCase):
    """换乘缓冲常量与确定性转场表（用户拍板口径）。"""

    def test_user_buffers(self) -> None:
        self.assertEqual(AIR_ARRIVE_BUFFER_MIN, 30)   # 飞→火：出机场 30min
        self.assertEqual(RAIL_CHECKIN_BUFFER_MIN, 30)  # 飞→火：进站 30min
        self.assertEqual(RAIL_ARRIVE_BUFFER_MIN, 20)   # 火→飞：出站 20min
        self.assertEqual(AIR_CHECKIN_BUFFER_MIN, 90)   # 火→飞：提前 1.5h 到机场

    def test_lookup_demo_transfer(self) -> None:
        self.assertEqual(lookup_transfer_minutes("常州奔牛机场", "常州北站"), 45)
        self.assertEqual(lookup_transfer_minutes("常州奔牛机场", "常州站"), 50)

    def test_lookup_norm_place_suffix(self) -> None:
        # 「常州北」与表键「常州北站」归一化后命中（去 站/机场 尾缀）
        self.assertEqual(lookup_transfer_minutes("常州奔牛机场", "常州北"), 45)

    def test_lookup_unknown_falls_back(self) -> None:
        self.assertEqual(
            lookup_transfer_minutes("火星机场", "月球站"), DEFAULT_TRANSFER_MIN
        )


class TestTransferCheck(unittest.TestCase):
    """check_leg_connection 纯函数：早班淘汰 / 晚班可行 / 原因可解释。"""

    def _leg(self, mode: str, depart: str, arrive: str,
             service_no: str = "KN5621",
             frm: str = "常州奔牛机场" if False else "锦州湾机场",
             to: str = "常州奔牛机场") -> Any:
        from data_transmission.demo_candidate import CandidateLeg
        return CandidateLeg(
            mode=mode, origin="锦州", destination="常州",
            from_station_or_airport=frm, to_station_or_airport=to,
            depart_datetime=f"{DATE} {depart}", arrive_datetime=f"{DATE} {arrive}",
            duration_min=115, price=199.0, source="demo_fixture",
            service_no=service_no,
        )

    def _train_leg(self, depart: str, arrive: str, service_no: str = "G7121") -> Any:
        from data_transmission.demo_candidate import CandidateLeg
        return CandidateLeg(
            mode="train", origin="常州", destination="上海",
            from_station_or_airport="常州北站", to_station_or_airport="上海虹桥站",
            depart_datetime=f"{DATE} {depart}", arrive_datetime=f"{DATE} {arrive}",
            duration_min=82, price=116.0, source="demo_fixture",
            service_no=service_no,
        )

    def test_flight_train_early_rejected(self) -> None:
        flight = self._leg("air", "08:00", "09:55", "KN5621", to="常州奔牛机场")
        train = self._train_leg("11:30", "12:54", "G7365")  # 间隔 95min
        check: TransferCheck = check_leg_connection(flight, train)
        self.assertFalse(check.ok)
        # 所需 = 出机场 30 + 转场 45（奔牛→常州北 Demo 值）+ 进站 30 = 105
        self.assertEqual(check.required_gap_min, 105)
        self.assertEqual(check.wait_min, 95)
        self.assertIn("无法接续", check.reason)

    def test_flight_train_acceptable(self) -> None:
        flight = self._leg("air", "08:00", "09:55", "KN5621", to="常州奔牛机场")
        train = self._train_leg("12:30", "13:52", "G7121")  # 间隔 155min ≥ 105
        check: TransferCheck = check_leg_connection(flight, train)
        self.assertTrue(check.ok)
        self.assertEqual(check.required_gap_min, 105)

    def test_train_flight_direction(self) -> None:
        # 火→飞：所需 = 出站 20 + 转场 45（默认） + 提前 90 = 155
        from data_transmission.demo_candidate import CandidateLeg
        train = CandidateLeg(
            mode="train", origin="北京", destination="天津",
            from_station_or_airport="北京南站", to_station_or_airport="天津站",
            depart_datetime=f"{DATE} 10:00", arrive_datetime=f"{DATE} 11:10",
            duration_min=70, price=54.5, source="demo_fixture", service_no="G5",
        )
        flight_early = CandidateLeg(
            mode="air", origin="天津", destination="上海",
            from_station_or_airport="天津滨海机场", to_station_or_airport="上海虹桥国际机场",
            depart_datetime=f"{DATE} 13:00", arrive_datetime=f"{DATE} 14:50",
            duration_min=110, price=520.0, source="demo_fixture", service_no="HO1215",
        )
        check_early: TransferCheck = check_leg_connection(train, flight_early)
        self.assertFalse(check_early.ok)
        self.assertEqual(check_early.required_gap_min, 155)  # 20+45+90
        self.assertEqual(check_early.wait_min, 110)


class TestDemoTransferChain(unittest.TestCase):
    """端到端：默认只返回可接续候选；include_rejected 可审计淘汰原因。"""

    def setUp(self) -> None:
        self.provider = _FakeToolProvider()
        self.air = load_air_routes()

    def test_only_feasible_returned(self) -> None:
        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=self.air,
        )
        self.assertTrue(candidates)
        for c in candidates:
            self.assertTrue(c.feasible, c.reject_reason)
        self.assertEqual(
            {leg.service_no for c in candidates for leg in c.legs if leg.mode == "train"},
            {"G7121"},  # G7132(08:30)/G7365(11:30) 均早于可接续窗口 → 被淘汰
        )

    def test_rejected_candidates_carried_with_reason(self) -> None:
        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=self.air,
            include_rejected=True,
        )
        rejected = [c for c in candidates if not c.feasible]
        self.assertEqual(len(rejected), 2)  # G7132 + G7365
        codes = {c.legs[1].service_no for c in rejected}
        self.assertEqual(codes, {"G7132", "G7365"})
        for c in rejected:
            self.assertTrue(c.reject_reason)
            self.assertIn("无法接续", c.reject_reason)

    def test_full_total_includes_wait_transfer_buffer(self) -> None:
        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=self.air,
        )
        chain = next(c for c in candidates if c.template == "flight_train")
        # 完整总耗时 = 值机 90 + 飞行 115 + 等待 155 + 车次 82 = 442
        # （段间缓冲含在等待内；转场/缓冲本身比运行 + 等待更长）
        self.assertEqual(chain.running_minutes, 115 + 82)
        self.assertGreater(
            chain.total_minutes,
            chain.running_minutes + chain.transfer_wait_min,
        )
        self.assertEqual(chain.total_minutes, 90 + 115 + 155 + 82)

    def test_leg_prices_durations_same_train(self) -> None:
        # I-05：时长与价格来自同一车次行——KN5621 与 G7121 各自完整
        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=self.air,
        )
        chain = next(c for c in candidates if c.template == "flight_train")
        air_leg, train_leg = chain.legs
        self.assertEqual(air_leg.duration_min, 115)
        self.assertEqual(air_leg.price, 199.0)
        self.assertEqual(train_leg.service_no, "G7121")
        self.assertEqual(train_leg.duration_min, 82)
        self.assertEqual(train_leg.price, 116.0)

    def test_agg_source_demo_fixture(self) -> None:
        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=self.air,
        )
        for c in candidates:
            self.assertEqual(c.agg_source, "demo_fixture")
            for leg in c.legs:
                self.assertEqual(leg.source, "demo_fixture")  # 绝不冒充 live

    def test_agg_source_mixed_for_live_flight(self) -> None:
        # 航空段 live 真源 + 铁路段 demo_fixture → mixed（I-12）
        def flight_provider(o: str, d: str, date: str) -> Optional[List[Dict[str, Any]]]:
            if (o, d) == ("锦州", "常州"):
                return [{"flight_no": "KN5601", "airline": "演示航",
                         "from_airport": "JNZ", "to_airport": "CZX",
                         "from_airport_name": "锦州湾机场",
                         "to_airport_name": "常州奔牛机场",
                         "depart_time": "08:00", "arrive_time": "09:55",
                         "duration_min": 115, "price": 199.0, "date": date,
                         "status": "scheduled", "source": "live"}]
            return None

        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=flight_provider,
            air_routes=self.air,
        )
        chain = next(c for c in candidates if c.template == "flight_train")
        self.assertEqual(chain.agg_source, "mixed")
        self.assertEqual(chain.legs[0].source, "live")
        self.assertEqual(chain.legs[1].source, "demo_fixture")

    def test_mock_mapped_to_demo_fixture(self) -> None:
        # make_demo_*_provider：B 侧 mock 工具(ToolResult.source=mock) → demo_fixture
        tp = _FakeToolProvider()
        train_provider = make_demo_train_provider(tp)
        rows = train_provider("常州", "上海", DATE)
        self.assertTrue(rows)
        self.assertEqual(rows[0]["source"], "demo_fixture")
        flight_provider = make_demo_flight_provider(tp)
        frows = flight_provider("锦州", "常州", DATE)
        self.assertEqual(frows[0]["source"], "demo_fixture")


if __name__ == "__main__":
    unittest.main()