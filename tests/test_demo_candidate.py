"""Demo 候选链路目标测试（四小时作战阶段 1：1:30 Demo 候选链路）。

验收口径（02-四小时修复与Demo执行计划.md §1:30～2:25 完成标准）：
- 候选集中出现 锦州→常州→上海（飞机→火车模板）；
- 常州→上海铁路查询在一次规划内只执行一次（正/负结果都缓存）；
- 每条 leg 来自**同一个完整班次**（时刻/时长/价格/班次号同行，I-05/I-10）；
- 每城市对航班验证 ≤ Top-K（默认 4），一次规划内一城市对只查一次；
- 没有全国航班城市矩阵扫描；
- 铁路 mock 城市对校验（I-04 铁路侧对齐）：任意城市对绝不返回
  固定北京南→上海虹桥车次冒充真实数据。

全部用例走 B 侧 mock 工具 / local fixture，**禁真实网络**（额度 550/月纪律）。
"""

from __future__ import annotations

import os
import re
import sys
import unittest
from typing import Any, Dict, List, Optional, Sequence

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data_transmission.air_routes import AirRoutes, load_air_routes  # noqa: E402
from data_transmission.demo_candidate import (  # noqa: E402
    IntercityQueryCache,
    build_demo_candidates,
    make_demo_flight_provider,
    make_demo_train_provider,
)
from tools.flight.tools import FlightSearchTool  # noqa: E402
from tools.train.tools import TrainTicketTool, _demo_train_rows  # noqa: E402
from tools.train.trip import TrainTripSkill  # noqa: E402

DATE = "2026-09-01"  # Demo 固定日期（2026-09-01，随样例数据存在）


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


_DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

_LEG_MIN_FIELDS = (
    "mode", "origin", "destination", "from_station_or_airport",
    "to_station_or_airport", "depart_datetime", "arrive_datetime",
    "duration_min", "price", "source", "service_no",
)


def _assert_leg_fields(test: unittest.TestCase, leg: Any) -> None:
    for field in _LEG_MIN_FIELDS:
        test.assertTrue(hasattr(leg, field) or field in leg,
                        f"leg 缺少最低字段 {field}")
    test.assertRegex(str(leg.depart_datetime), _DATETIME_RE)
    test.assertRegex(str(leg.arrive_datetime), _DATETIME_RE)


class TestDemoTrainMockCityChecks(unittest.TestCase):
    """铁路 mock 城市对校验（I-04 铁路侧对齐）。"""

    def test_beijing_shanghai_kept(self) -> None:
        self.assertEqual([r["code"] for r in _demo_train_rows("北京", "上海")],
                         ["G39", "D311"])

    def test_changzhou_shanghai_demo_rows(self) -> None:
        rows = _demo_train_rows("常州", "上海")
        self.assertEqual([r["code"] for r in rows], ["G7132", "G7365"])
        self.assertEqual(rows[0]["from_station"], "常州北")
        self.assertEqual(rows[0]["to_station"], "上海虹桥")
        self.assertGreater(rows[0]["price"], 0)  # Demo 样例必须带票价

    def test_station_name_variant_accepted(self) -> None:
        self.assertEqual(len(_demo_train_rows("常州北站", "上海虹桥站")), 2)

    def test_unknown_pair_returns_empty(self) -> None:
        self.assertEqual(_demo_train_rows("郑州", "上海"), [])
        self.assertEqual(_demo_train_rows("锦州", "北京"), [])
        self.assertEqual(_demo_train_rows("常州", "北京"), [])

    def test_ticket_tool_ok_with_city_names(self) -> None:
        r = TrainTicketTool().execute(from_station="常州", to_station="上海",
                                      date=DATE)
        self.assertEqual(r.status.value, "ok")
        self.assertEqual(r.data[0]["code"], "G7132")

    def test_trip_tool_earliest_picks_shortest(self) -> None:
        r = TrainTripSkill().execute(from_city="常州", to_city="上海", date=DATE)
        self.assertEqual(r.status.value, "ok")
        # G7365（01:24）短于 G7132（01:26）→ earliest 推荐 G7365
        self.assertEqual(r.data["code"], "G7365")

    def test_trip_tool_unknown_pair_errors(self) -> None:
        r = TrainTripSkill().execute(from_city="郑州", to_city="上海", date=DATE)
        self.assertEqual(r.status.value, "error")
        self.assertIn("暂无演示车次", r.error)


class TestDemoCandidateChain(unittest.TestCase):
    """核心验收：锦州 → 常州 → 上海 候选链路（飞机→火车模板）。"""

    def setUp(self) -> None:
        self.provider = _FakeToolProvider()
        self.air = load_air_routes()
        self.cache = IntercityQueryCache()

    def _build(self, origin: str = "锦州", destination: str = "上海",
               **kwargs: Any) -> List[Any]:
        return build_demo_candidates(
            origin, destination, DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=kwargs.pop("air_routes", self.air),
            cache=kwargs.pop("cache", self.cache),
            **kwargs,
        )

    def test_chain_jinzhou_changzhou_shanghai_present(self) -> None:
        candidates = self._build()
        chains = [c for c in candidates if c.template == "flight_train"]
        self.assertTrue(chains, "候选集必须出现 飞机→火车 模板")
        for candidate in chains:
            self.assertEqual([leg.origin for leg in candidate.legs],
                             ["锦州", "常州"])
            self.assertEqual([leg.destination for leg in candidate.legs],
                             ["常州", "上海"])
            self.assertEqual(candidate.legs[0].mode, "air")
            self.assertEqual(candidate.legs[1].mode, "train")
            self.assertEqual(candidate.legs[0].service_no, "KN5621")
            self.assertIn(candidate.legs[1].service_no, ("G7132", "G7365"))

    def test_legs_min_fields(self) -> None:
        candidates = self._build()
        self.assertTrue(candidates)
        for leg in candidates[0].legs:
            _assert_leg_fields(self, leg)
        # Demo 固定样例来源必须诚实，不得冒充 live
        self.assertEqual(candidates[0].agg_source, "demo_fixture")
        for leg in candidates[0].legs:
            self.assertEqual(leg.source, "demo_fixture")

    def test_train_query_once_and_flight_after_train(self) -> None:
        self._build()
        # 常州→上海 一次规划内只查一次（正结果缓存）
        self.assertEqual(self.cache.train_calls("常州", "上海", DATE), 1)
        # 锦州→常州 航班只查一次（Top-K 城市对验证）
        self.assertEqual(self.cache.flight_calls("锦州", "常州", DATE), 1)

    def test_rail_unavailable_skips_flight_verification(self) -> None:
        self._build()
        # 郑州→上海 铁路无车次（mock 未收录）→ 锦州→郑州 航班必须不被验证
        self.assertEqual(self.cache.train_calls("郑州", "上海", DATE), 1)
        self.assertEqual(self.cache.flight_calls("锦州", "郑州", DATE), 0)
        # 直达航空模板：拓扑有 锦州→上海 边 → 只提名验证一次（结果为 0 条，诚实淘汰）
        self.assertEqual(self.cache.flight_calls("锦州", "上海", DATE), 1)

    def test_no_flight_matrix_scan(self) -> None:
        self._build()
        # 全部航班调用 = 直航提名(锦州→上海) + 铁路可行的航空段
        # （锦州→北京、锦州→常州）= 3 对；绝无全国城市矩阵扫描
        flight_pairs = [
            key for key in self.cache.calls if key[0] == "flight"
        ]
        self.assertEqual(len(flight_pairs), 3)
        for key in flight_pairs:
            self.assertEqual(key[1], "锦州")  # 只从出发城展开的有限城市对

    def test_topology_missing_edge_no_candidate(self) -> None:
        # 删除 锦州→常州 提示边（03 验收「航线拓扑缺边」）：
        # 不生成该链候选、不调用该航班城市对
        trimmed = AirRoutes([
            h for h in self.air.hints
            if not (h.origin_city == "锦州" and h.destination_city == "常州")
        ])
        candidates = self._build(air_routes=trimmed)
        chains = [c for c in candidates if c.template == "flight_train"]
        self.assertFalse(chains)
        self.assertEqual(self.cache.flight_calls("锦州", "常州", DATE), 0)

    def test_negative_train_result_cached(self) -> None:
        # 负结果（郑州→上海 无车次）也缓存：同一 cache 第二次 build 不再查
        self._build()
        self._build()
        self.assertEqual(self.cache.train_calls("郑州", "上海", DATE), 1)


class TestTopK(unittest.TestCase):
    def test_flight_candidates_capped(self) -> None:
        rows = [
            {"flight_no": f"KN99{i}", "airline": "演示航司",
             "from_airport": "JNZ", "to_airport": "CZX",
             "from_airport_name": "锦州湾机场", "to_airport_name": "常州奔牛机场",
             "depart_time": "08:00", "arrive_time": "09:55",
             "duration_min": 115, "price": 199.0, "date": DATE,
             "status": "scheduled", "source": "demo_fixture"}
            for i in range(8)
        ]

        def flight_provider(origin: str, destination: str, date: str):
            return rows if (origin, destination) == ("锦州", "常州") else None

        air = load_air_routes()
        candidates = build_demo_candidates(
            "锦州", "上海", DATE,
            train_provider=lambda o, d, dt: _demo_train_rows(o, d) or None,
            flight_provider=flight_provider,
            air_routes=air,
        )
        # Top-K ≤4 = 每城市对**参与验证的航班行数** ≤4
        # （8 条候选行只验证前 4 条；再与 2 条车次组合出 4×2=8 个候选）
        air_service_nos = {
            leg.service_no for c in candidates for leg in c.legs
            if leg.mode == "air"
        }
        self.assertLessEqual(len(air_service_nos), 4)


class TestDirectTemplates(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = _FakeToolProvider()
        self.air = load_air_routes()

    def _build(self, origin: str, destination: str) -> List[Any]:
        return build_demo_candidates(
            origin, destination, DATE,
            train_provider=make_demo_train_provider(self.provider),
            flight_provider=make_demo_flight_provider(self.provider),
            air_routes=self.air,
            mid_cities_max=4,
        )

    def test_direct_train(self) -> None:
        candidates = self._build("北京", "上海")
        trains = [c for c in candidates if c.template == "direct_train"]
        self.assertTrue(trains)
        codes = {leg.service_no for c in trains for leg in c.legs}
        self.assertEqual(codes, {"G39", "D311"})

    def test_direct_flight(self) -> None:
        candidates = self._build("北京", "上海")
        flights = [c for c in candidates if c.template == "direct_flight"]
        self.assertTrue(flights)
        self.assertLessEqual(len(flights), 3)  # 京沪样例 3 条 ≤ Top-K

    def test_unknown_direct_pair_yields_nothing(self) -> None:
        # 火星→月球：拓扑无边 + 铁路 mock 空 → 空候选（不假装）
        candidates = self._build("火星", "月球")
        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()