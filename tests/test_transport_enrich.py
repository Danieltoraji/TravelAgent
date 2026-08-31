"""市内交通导航测试（2026-09-01）：

1. amap_client._extract_transit_route 具体线路提取（transit_text/walking_m）；
2. map_tool._route_live transit 透传；
3. b_contract._node_to_place transport 段 details 透传；
4. runtime.enrich_transport_details 并发 enrich（成功/失败/无真源）。
"""

import os
import sys
import unittest
from datetime import date
from unittest import mock

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"),
           os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

from core.schemas import DayPlan, Place, TripTimeline  # noqa: E402
from runtime.agent_runtime import runtime  # noqa: E402
from tools.amap_client import AmapClient  # noqa: E402


# 高德 v3 transit 响应 fixture（实测结构：segments[].bus.buslines /
# walking.distance，顶层 walking_distance）
TRANSIT_RESP = {
    "route": {
        "transits": [{
            "distance": "1762",
            "duration": "2178",
            "cost": "2.0",
            "walking_distance": "1256",
            "segments": [
                {"walking": {"distance": "858", "steps": [{}]},
                 "bus": {"buslines": [{
                     "name": "124路(西四路口东--景山公园)",
                     "via_num": "0",
                     "departure_stop": {"name": "西四路口东"},
                     "arrival_stop": {"name": "景山公园"},
                 }]}},
                {"walking": {"distance": "398", "steps": [{}]},
                 "bus": {"buslines": []}},
            ],
        }]
    }
}

MULTI_LINE_RESP = {
    "route": {
        "transits": [{
            "distance": "5000", "duration": "1800", "cost": "5.0",
            "walking_distance": "300",
            "segments": [
                {"walking": {"distance": "300", "steps": []},
                 "bus": {"buslines": [{"name": "地铁8号线", "via_num": "2"}]}},
            ],
        }]
    }
}


class TestTransitExtraction(unittest.TestCase):
    def test_extract_transit_text(self) -> None:
        out = AmapClient._extract_transit_route(TRANSIT_RESP)
        self.assertEqual(out["distance"], 1762)
        self.assertEqual(out["duration"], 2178)
        self.assertEqual(out["cost"], 2)
        self.assertIn("步行858m", out["transit_text"])
        self.assertIn("124路", out["transit_text"])
        self.assertIn("步行398m", out["transit_text"])
        self.assertEqual(out["walking_m"], 1256)

    def test_extract_station_count(self) -> None:
        out = AmapClient._extract_transit_route(MULTI_LINE_RESP)
        self.assertIn("地铁8号线 3站", out["transit_text"])
        self.assertEqual(out["walking_m"], 300)

    def test_empty_transit_raises(self) -> None:
        with self.assertRaises(ValueError):
            AmapClient._extract_transit_route({"route": {"transits": []}})


class TestNodeToPlacePassthrough(unittest.TestCase):
    def test_transport_details_preserved(self) -> None:
        from a_side.data_transmission.b_contract import _node_to_place

        place = _node_to_place({
            "type": "transport",
            "name": "故宫博物院 → 景山公园",
            "start_minutes": 900,
            "end_minutes": 918,
            "details": {"from": "故宫博物院", "to": "景山公园",
                        "distance_km": 1.76, "source": "live_map_api"},
        })
        self.assertEqual(place.category, "transport")
        self.assertEqual(place.details["from"], "故宫博物院")
        self.assertEqual(place.details["distance_km"], 1.76)
        self.assertEqual(place.details["source"], "live_map_api")


def _timeline_with_transport():
    return TripTimeline(
        city="北京",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        days=[DayPlan(
            day=1, date=date(2026, 8, 1),
            items=[
                Place(name="故宫博物院", category="scenic", arrival="09:00"),
                Place(name="故宫博物院 → 景山公园", category="transport",
                      arrival="15:00", end_time="15:18",
                      details={"from": "故宫博物院", "to": "景山公园",
                               "distance_km": 1.76, "source": "live_map_api"}),
                Place(name="景山公园", category="scenic", arrival="15:18"),
            ],
        )],
    )


class TestEnrichTransportDetails(unittest.TestCase):
    def setUp(self) -> None:
        self._timeline = runtime.timeline
        self._map_api = getattr(runtime, "_use_real_map_api_backup", None)
        runtime.timeline = None

    def tearDown(self) -> None:
        runtime.timeline = self._timeline

    def test_enrich_success(self) -> None:
        tl = _timeline_with_transport()
        fake_result = mock.MagicMock()
        fake_result.status.value = "ok"
        fake_result.data = {
            "from": "故宫博物院", "to": "景山公园", "mode": "transit",
            "distance_km": 1.76, "duration_min": 37, "fare": 2.0,
            "transit": "公交", "transit_text": "步行858m → 124路 1站 → 步行398m",
            "walking_m": 1256, "source": "live",
        }
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.return_value = fake_result
            runtime.enrich_transport_details(tl)
        seg = tl.days[0].items[1]
        self.assertEqual(seg.details["mode"], "transit")
        self.assertEqual(seg.details["duration_min"], 37)
        self.assertIn("124路", seg.details["transit_text"])
        self.assertEqual(seg.details["source"], "live")

    def test_enrich_failure_keeps_matrix(self) -> None:
        tl = _timeline_with_transport()
        fake_result = mock.MagicMock()
        fake_result.status.value = "error"
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.return_value = fake_result
            runtime.enrich_transport_details(tl)
        seg = tl.days[0].items[1]
        # 保留透传的矩阵信息，无 mode/transit_text
        self.assertEqual(seg.details["distance_km"], 1.76)
        self.assertNotIn("mode", seg.details)
        self.assertNotIn("transit_text", seg.details)

    def test_enrich_exception_silent(self) -> None:
        tl = _timeline_with_transport()
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.side_effect = RuntimeError("amap down")
            runtime.enrich_transport_details(tl)  # 不应抛出
        seg = tl.days[0].items[1]
        self.assertEqual(seg.details["distance_km"], 1.76)

    def test_enrich_skips_without_map_key(self) -> None:
        tl = _timeline_with_transport()
        from config.settings import settings as app_settings

        with mock.patch.object(
            type(app_settings), "use_real_map_api",
            new_callable=mock.PropertyMock, return_value=False,
        ):
            with mock.patch.object(runtime, "registry") as reg:
                runtime.enrich_transport_details(tl)
                reg.call.assert_not_called()
        seg = tl.days[0].items[1]
        self.assertNotIn("mode", seg.details)


if __name__ == "__main__":
    unittest.main()
