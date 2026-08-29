"""live_data normalize 单测：主字段/别名命中、logger 埋点（A3 + 热修回归）。

历史 bug：A3 埋点用了 logger.debug 但模块未定义 logger——别名分支命中即
NameError。本文件所有别名用例都跑在 assertLogs 下，同时验证埋点与 logger 存在。
"""

import os
import sys
import unittest

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from data_transmission.live_data import (  # noqa: E402
    _normalize_live_hotel,
    _normalize_live_restaurant,
    normalize_live_spot,
)

_LIVE_LOGGER = "data_transmission.live_data"


class TestNormalizeLiveSpot(unittest.TestCase):
    def test_primary_fields(self) -> None:
        spot = normalize_live_spot({
            "id": "L1", "name": "故宫", "location": {"lat": 39.9, "lng": 116.4},
            "price": 60, "duration": 180, "tags": ["历史文化"],
        }, "北京")
        self.assertEqual(spot["name"], "故宫")
        self.assertEqual(spot["price"], 60.0)
        self.assertEqual(spot["duration"], 180)
        self.assertEqual(spot["location"], {"lat": 39.9, "lng": 116.4})

    def test_alias_fields_hit_debug_logging(self) -> None:
        """别名命中走 _pick → logger.debug（logger 未定义时此处曾 NameError）。"""
        with self.assertLogs(_LIVE_LOGGER, level="DEBUG") as captured:
            spot = normalize_live_spot({
                "title": "故宫博物院",            # name 别名
                "ticket_price": 60,               # price 别名
                "suggest_duration": 90,           # duration 别名
            }, "北京", index=3)
        self.assertEqual(spot["name"], "故宫博物院")
        self.assertEqual(spot["price"], 60.0)
        self.assertEqual(spot["duration"], 90)
        self.assertEqual(spot["id"], "live_3")
        self.assertTrue(any("别名命中" in message for message in captured.output))

    def test_no_name_returns_none(self) -> None:
        self.assertIsNone(normalize_live_spot({"price": 60}, "北京"))


class TestNormalizeLiveHotel(unittest.TestCase):
    def test_rooms_price_primary(self) -> None:
        hotel = _normalize_live_hotel({
            "id": "H1", "name": "酒店", "location": {"lat": 39.9, "lng": 116.4},
            "rooms": [{"price": 320}, {"price": 280}], "rating": 4.5,
        })
        self.assertEqual(hotel.price_per_night, 280)   # rooms 最低价
        self.assertEqual(hotel.star, 4)                # rating 4.5 分档

    def test_price_per_night_fallback_logged(self) -> None:
        with self.assertLogs(_LIVE_LOGGER, level="DEBUG"):
            hotel = _normalize_live_hotel({
                "id": "H2", "name": "酒店", "location": {"lat": 39.9, "lng": 116.4},
                "price_per_night": 300, "rating": 4.5,
            })
        self.assertEqual(hotel.price_per_night, 300.0)


class TestNormalizeLiveRestaurant(unittest.TestCase):
    def test_top_level_latlng_primary(self) -> None:
        restaurant = _normalize_live_restaurant({
            "id": "F1", "name": "店", "lat": 39.9, "lng": 116.4,
            "cuisine": "京菜", "price_per_person": 50,
        })
        self.assertEqual(restaurant.location, (39.9, 116.4))
        self.assertEqual(restaurant.average_cost, 50.0)

    def test_location_alias_logged(self) -> None:
        """location 串命中（B 侧真实形状是顶层 lat/lng）→ debug 埋点。"""
        with self.assertLogs(_LIVE_LOGGER, level="DEBUG"):
            restaurant = _normalize_live_restaurant({
                "id": "F2", "name": "店", "location": "116.4,39.9",
                "lat": 39.9, "lng": 116.4, "price_per_person": 50,
            })
        self.assertEqual(restaurant.location, (39.9, 116.4))

    def test_no_coordinates_dropped(self) -> None:
        self.assertIsNone(_normalize_live_restaurant({"id": "F3", "name": "店"}))


if __name__ == "__main__":
    unittest.main()
