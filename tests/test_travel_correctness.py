"""阶段 3 目标测试：快速正确性（3:05~3:35）。

验收口径（02-四小时修复与Demo执行计划.md §3:05～3:35）：
- **I-08**：返程失败**不复用去程**——返程（destination→origin）独立解析，查不到
  → 诚实缺省返程段，绝不用去程方向（origin→destination）伪造反向 legs；
- **I-11**：航班缓冲计入总时间——直达航空的完整耗时 = 运行 + air 值机缓冲
  （`AIR_BUFFER_MIN=60`，BFS 口径），直达判断与段时长都按完整耗时；
- **I-06**：缓存键含 mode 与 date——`IntercityQueryCache` 键 = (mode, origin,
  destination, date)，同键命中、不同 mode/date 各自独立查询。

全部用例本地 fixture / B 侧 mock，**禁真实网络**。
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

from data_transmission.city_travel import (  # noqa: E402
    AIR_BUFFER_MIN,
    CityTravelEdge,
    load_city_travel_options,
)
from data_transmission.demo_candidate import IntercityQueryCache  # noqa: E402
from data_transmission.travel import _resolve_intercity_route, build_trip_segments  # noqa: E402


class TestI08ReturnNeverReusesOutbound(unittest.TestCase):
    """I-08：返程不复用去程方向。"""

    def _requirement(self) -> Dict[str, Any]:
        return {
            "content": {
                "origin": "锦州",
                "destination": "上海",
                "travel_schedule": {
                    "departure_date": "2026-09-01",
                    "departure_time": "08:00",
                    "return_date": "2026-09-05",
                    "return_time": "20:00",
                },
            }
        }

    def test_return_never_reuses_outbound(self) -> None:
        live = CityTravelEdge(
            origin="锦州", destination="上海",
            transport_minutes=115, mode="air", cost_per_person=199.0,
        )

        def provider(o: str, d: str, mode: Optional[str] = None) -> Optional[CityTravelEdge]:
            if (o, d) == ("锦州", "上海"):
                return live  # 只有去程方向有真源
            return None     # 返程（上海→锦州）无方案

        segments = build_trip_segments({}, self._requirement(), travel_provider=provider)
        kinds = [s["details"]["kind"] for s in segments]
        self.assertIn("outbound", kinds)
        # 返程无方案 → 诚实缺省，绝不复用去程方向伪造返程段
        self.assertNotIn("return", kinds)

    def test_resolve_reverse_missing_is_none(self) -> None:
        live = CityTravelEdge(
            origin="锦州", destination="上海",
            transport_minutes=115, mode="air", cost_per_person=199.0,
        )

        def provider(o: str, d: str, mode: Optional[str] = None) -> Optional[CityTravelEdge]:
            return live if (o, d) == ("锦州", "上海") else None

        route = _resolve_intercity_route(
            "上海", "锦州", provider, load_city_travel_options()
        )
        self.assertIsNone(route)  # 反向独立解析失败 → None（不做方向互换）


class TestI11AirBufferIncluded(unittest.TestCase):
    """I-11：直达航空完整耗时含值机缓冲。"""

    def test_direct_air_resolution_includes_buffer(self) -> None:
        live = CityTravelEdge(
            origin="北京", destination="上海",
            transport_minutes=126, mode="air", cost_per_person=600.0,
        )

        def provider(o: str, d: str, mode: Optional[str] = None) -> Optional[CityTravelEdge]:
            return live if (o, d) == ("北京", "上海") else None

        route = _resolve_intercity_route(
            "北京", "上海", provider, load_city_travel_options()
        )
        self.assertIsNotNone(route)
        self.assertFalse(route.is_chain)
        # 完整耗时 = 运行 126 + air 值机缓冲 60（不再返回裸运行时长）
        self.assertEqual(route.total_minutes, 126 + AIR_BUFFER_MIN)

    def test_segment_uses_full_duration(self) -> None:
        live = CityTravelEdge(
            origin="北京", destination="上海",
            transport_minutes=126, mode="air", cost_per_person=600.0,
        )

        def provider(o: str, d: str, mode: Optional[str] = None) -> Optional[CityTravelEdge]:
            return live if (o, d) == ("北京", "上海") else None

        requirement = {
            "content": {
                "origin": "北京",
                "destination": "上海",
                "travel_schedule": {
                    "departure_date": "2026-09-01",
                    "departure_time": "08:00",
                    "return_date": "2026-09-05",
                    "return_time": "20:00",
                },
            }
        }
        segments = build_trip_segments({}, requirement, travel_provider=provider)
        seg = next(s for s in segments if s["details"]["kind"] == "outbound")
        self.assertEqual(seg["duration_minutes"], 126 + AIR_BUFFER_MIN)


class TestI06CacheKeyModeDate(unittest.TestCase):
    """I-06：缓存键 (mode, origin, destination, date) 齐全。"""

    def test_same_key_hits_date_isolated(self) -> None:
        cache = IntercityQueryCache()
        hits: List[str] = []

        def make(date_str: str):
            def fn() -> Optional[List[Dict[str, Any]]]:
                hits.append(date_str)
                return [{"code": "G7121", "from_station": "常州北",
                         "to_station": "上海虹桥", "depart_time": "12:30",
                         "arrive_time": "13:52", "price": 116.0}]
            return fn

        cache.get_or_query("train", "常州", "上海", "2026-09-01", make("2026-09-01"))
        cache.get_or_query("train", "常州", "上海", "2026-09-01", make("2026-09-01"))
        self.assertEqual(len(hits), 1)                      # 同 mode/date 命中缓存
        self.assertEqual(cache.train_calls("常州", "上海", "2026-09-01"), 1)

        cache.get_or_query("train", "常州", "上海", "2026-09-02", make("2026-09-02"))
        self.assertEqual(len(hits), 2)                      # date 不同 → 独立查询
        self.assertEqual(cache.train_calls("常州", "上海", "2026-09-02"), 1)
        self.assertEqual(cache.train_calls("常州", "上海", "2026-09-01"), 1)

    def test_mode_isolated_in_cache(self) -> None:
        cache = IntercityQueryCache()
        hits: List[str] = []

        def make(kind: str):
            def fn() -> Optional[List[Dict[str, Any]]]:
                hits.append(kind)
                return [{"test": True}]
            return fn

        cache.get_or_query("train", "常州", "上海", "2026-09-01", make("train"))
        cache.get_or_query("flight", "常州", "上海", "2026-09-01", make("flight"))
        self.assertEqual(len(hits), 2)                      # mode 不同 → 键分离
        self.assertEqual(cache.train_calls("常州", "上海", "2026-09-01"), 1)
        self.assertEqual(cache.flight_calls("常州", "上海", "2026-09-01"), 1)

    def test_negative_result_cached_per_key(self) -> None:
        cache = IntercityQueryCache()
        count = [0]

        def fn_none() -> Optional[List[Dict[str, Any]]]:
            count[0] += 1
            return None

        self.assertIsNone(cache.get_or_query("train", "新乡", "郑州", "2026-09-01", fn_none))
        self.assertIsNone(cache.get_or_query("train", "新乡", "郑州", "2026-09-01", fn_none))
        self.assertEqual(count[0], 1)                       # 负结果也缓存（不重复查询/计费）


if __name__ == "__main__":
    unittest.main()