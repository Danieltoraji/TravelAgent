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


# ---------------------------------------------------------------------------
# 地理保真（十一节，2026-09-04）：市内段 enrich 距离 sanity + same_city 透传
# ---------------------------------------------------------------------------

import unittest as _ut

from tools.map_tool import _resolve_coord_fallback  # noqa: E402


class TestEnrichDistanceSanity(_ut.TestCase):
    """市内段（无 kind）enrich 距离 > 80km（POI 漂移跨市）→ 放弃合并保留矩阵值；
    城际段（kind=outbound）整体跳过 enrich（T5，2026-09-05：城市级地名查
    transit 产生跨市怪路线覆盖展示，真源班次数据已足够）。"""

    def _tl(self, kind=None):
        details = {"from": "A", "to": "B", "duration_min": 5}
        if kind:
            details["kind"] = kind
        place = Place(name=f"{details['from']} → {details['to']}",
                      category="transport", arrival="10:00", end_time="10:05",
                      details=details)
        day = DayPlan(day=1, date=None, items=[place])
        return TripTimeline(id="t", city="天津", start_date=None, end_date=None,
                            days=[day])

    def test_intra_city_far_distance_not_merged(self) -> None:
        tl = self._tl()  # 市内段（无 kind）
        fake_result = mock.MagicMock()
        fake_result.status.value = "ok"
        fake_result.data = {"mode": "transit", "distance_km": 86.95,
                            "duration_min": 141, "transit_text": "霸州1路 9站",
                            "source": "live"}
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.return_value = fake_result
            runtime.enrich_transport_details(tl)
        seg = tl.days[0].items[0]
        self.assertNotIn("distance_km", seg.details)  # 漂移值未合并
        self.assertNotIn("transit_text", seg.details)
        self.assertEqual(seg.details["duration_min"], 5)  # 矩阵值保留

    def test_intra_city_normal_distance_merged(self) -> None:
        tl = self._tl()
        fake_result = mock.MagicMock()
        fake_result.status.value = "ok"
        fake_result.data = {"mode": "transit", "distance_km": 2.43,
                            "duration_min": 16, "transit_text": "地铁6号线 2站",
                            "source": "live"}
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.return_value = fake_result
            runtime.enrich_transport_details(tl)
        seg = tl.days[0].items[0]
        self.assertEqual(seg.details["distance_km"], 2.43)

    def test_intercity_kind_skipped_entirely(self) -> None:
        """T5（2026-09-05）：城际段（kind=outbound）不做 transit enrich——
        from/to 是城市级地名（「天津」/「北京」），查询返回跨市/被夹转怪路线
        （实测 天津地铁+机场巴士 184.81km、锦州→张掖被覆盖 2657km），且真源
        班次（service_no/时刻/票价）已足够展示 → registry 不调用、details
        原样保留。"""
        tl = self._tl(kind="outbound")
        fake_result = mock.MagicMock()
        fake_result.status.value = "ok"
        fake_result.data = {"mode": "transit", "distance_km": 184.81,
                            "duration_min": 362,
                            "transit_text": "天津地铁5号线 4站 → 机场巴士",
                            "source": "live"}
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.return_value = fake_result
            runtime.enrich_transport_details(tl)
            reg.call.assert_not_called()
        seg = tl.days[0].items[0]
        self.assertNotIn("distance_km", seg.details)
        self.assertNotIn("transit_text", seg.details)
        self.assertNotIn("mode", seg.details)
        self.assertEqual(seg.details["duration_min"], 5)  # 矩阵值原样

    def test_enrich_passes_same_city_flag_for_intra_city(self) -> None:
        tl = self._tl()  # 市内段 → same_city=True 透传（城市归属校验）
        fake_result = mock.MagicMock()
        fake_result.status.value = "ok"
        fake_result.data = {"mode": "transit", "distance_km": 2.0,
                            "duration_min": 10, "source": "live"}
        with mock.patch.object(runtime, "registry") as reg:
            reg.call.return_value = fake_result
            runtime.enrich_transport_details(tl)
        _, kwargs = reg.call.call_args
        self.assertTrue(kwargs.get("same_city"))


class TestResolveCoordSameCityGuard(_ut.TestCase):
    """全国兜底城市归属校验：require_same_city 时命中外省行政区 → 拒绝。"""

    def _client_stub(self, limited_raises: bool, matched_city: str):
        client = mock.MagicMock()

        def geocode(address, city=""):
            if limited_raises and city:
                raise ValueError("高德地理编码未找到地址")  # 仅限定 city 失败
            return (39.1, 117.2)  # 全国兜底（city=""）正常返回

        def geocode_detail(address, city=""):
            # 全国兜底（city=""）：正常返回命中行政区（漂移与否由 matched_city 决定）
            return (39.1, 117.2, matched_city)

        client.geocode = geocode
        client.geocode_detail = geocode_detail
        return client

    def test_nationwide_other_province_rejected(self) -> None:
        client = self._client_stub(limited_raises=True, matched_city="霸州市")
        with self.assertRaises(ValueError):
            _resolve_coord_fallback(
                "蛋先生餐车", "天津", client.geocode,
                geocode_detail=client.geocode_detail, require_same_city=True,
            )

    def test_nationwide_same_city_accepted(self) -> None:
        client = self._client_stub(limited_raises=True, matched_city="天津市")
        lat, lng = _resolve_coord_fallback(
            "某店", "天津", client.geocode,
            geocode_detail=client.geocode_detail, require_same_city=True,
        )
        self.assertEqual((lat, lng), (39.1, 117.2))

    def test_default_off_keeps_cross_city_fallback(self) -> None:
        """require_same_city=False（城际 driving 跨城场景）→ 全国兜底照常。"""
        client = self._client_stub(limited_raises=True, matched_city="霸州市")
        lat, lng = _resolve_coord_fallback(
            "某地", "天津", client.geocode, geocode_detail=client.geocode_detail,
        )
        self.assertEqual((lat, lng), (39.1, 117.2))
