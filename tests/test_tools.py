"""工具层测试：注册表、各 Tool 的 Mock 调用、统一返回契约。"""

import json
import unittest
from unittest.mock import MagicMock, patch

from core.schemas import ToolStatus
from tools import default_registry
from tools.amap_client import AmapClient
from tools.map_tool import MapToolLive
from tools.qweather_client import QWeatherClient
from tools.weather_tool import (
    AirQualityToolLive,
    WeatherForecastToolLive,
    WeatherToolLive,
    WeatherWarningToolLive,
)


def make_mock_client(geo_id="101010100"):
    """构造一个 mock QWeatherClient，get_location_id 返回固定 ID。"""
    client = MagicMock(spec=QWeatherClient)
    client.get_location_id.return_value = geo_id
    return client


class TestToolRegistry(unittest.TestCase):
    def test_registry_has_all_nine_tools(self) -> None:
        names = default_registry.names()
        for tool in ["map", "weather", "weather_warning", "air_quality",
                     "weather_forecast", "scenic", "traffic", "food", "booking"]:
            self.assertIn(tool, names)

    def test_unknown_tool_raises(self) -> None:
        with self.assertRaises(KeyError):
            default_registry.call("nonexistent")

    def test_duplicate_register_raises(self) -> None:
        from tools.base_tool import ToolRegistry
        from tools.weather_tool import WeatherTool
        reg = ToolRegistry()
        reg.register(WeatherTool())
        with self.assertRaises(ValueError):
            reg.register(WeatherTool())


class TestTools(unittest.TestCase):
    def test_weather_ok(self) -> None:
        r = default_registry.call("weather", city="北京")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.tool, "weather")
        self.assertIn("condition", r.data)
        self.assertIn("rain_probability", r.data)

    def test_map_search_poi(self) -> None:
        r = default_registry.call("map", action="search_poi", query="故宫")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(any(p["name"] == "故宫" for p in r.data))

    def test_scenic_queue_reflects_world(self) -> None:
        from tools.mock_data import MockWorld
        from tools.scenic_tool import ScenicTool
        world = MockWorld()
        world.set_queue("故宫", 120)
        tool = ScenicTool(world)
        r = tool.execute(place="故宫")
        self.assertEqual(r.data["queue_min"], 120)

    def test_scenic_unknown_place_errors(self) -> None:
        r = default_registry.call("scenic", place="不存在的地方")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIsNotNone(r.error)

    def test_booking_prepare_no_payment(self) -> None:
        r = default_registry.call("booking", action="prepare",
                                  place="故宫", target_date="2026-08-01", party_size=2)
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(r.data["payment_required"])
        self.assertIn("booking_id", r.data)

    def test_booking_status_by_booking_id(self) -> None:
        # 先 prepare 拿到 booking_id，再 status 按 booking_id 查
        prep = default_registry.call("booking", action="prepare",
                                     place="故宫", target_date="2026-08-01", party_size=2)
        bid = prep.data["booking_id"]
        r = default_registry.call("booking", action="status", booking_id=bid)
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["booking_id"], bid)
        self.assertEqual(r.data["place"], "故宫")

    def test_booking_status_missing_id_errors(self) -> None:
        r = default_registry.call("booking", action="status")
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_tool_result_to_json(self) -> None:
        r = default_registry.call("weather", city="北京")
        text = r.to_json()
        self.assertIn("condition", text)


class TestWeatherLive(unittest.TestCase):
    """WeatherToolLive 测试：mock QWeatherClient，验证字段映射。"""

    def test_live_field_mapping(self):
        """测试和风 API 返回字段正确映射到项目 dict 结构。"""
        client = make_mock_client()
        # _fetch_now 返回的 now 数据
        client.get.side_effect = [
            {"code": "200", "now": {
                "temp": "31", "text": "晴", "icon": "100",
                "windScale": "3", "humidity": "45", "precip": "0.0",
                "feelsLike": "33", "vis": "15",
            }},
            # _fetch_uv_index 返回
            {"code": "200", "daily": [{"category": "7"}]},
        ]

        tool = WeatherToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["city"], "北京")
        self.assertEqual(result["condition"], "晴")
        self.assertEqual(result["temperature_c"], 31.0)
        self.assertEqual(result["feels_like"], 33.0)
        self.assertEqual(result["wind_kmh"], 17)  # windScale 3 → 17 km/h
        self.assertEqual(result["uv_index"], 7)
        self.assertEqual(result["rain_probability"], 10)  # precip=0 → 10%
        self.assertEqual(result["humidity"], 45)
        self.assertEqual(result["visibility_km"], 15.0)

    def test_live_rain_probability_from_precip(self):
        """测试降水量 >0 时降雨概率推断为 80%。"""
        client = make_mock_client()
        client.get.side_effect = [
            {"code": "200", "now": {
                "temp": "22", "text": "暴雨", "icon": "310",
                "windScale": "5", "precip": "15.5",
            }},
            {"code": "200", "daily": [{"category": "2"}]},
        ]

        tool = WeatherToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["condition"], "暴雨")
        self.assertEqual(result["rain_probability"], 80)  # precip>0 → 80%

    def test_live_uv_failure_defaults_zero(self):
        """测试 UV 指数 API 失败时默认返回 0。"""
        client = make_mock_client()
        client.get.side_effect = [
            {"code": "200", "now": {
                "temp": "25", "icon": "100", "windScale": "1", "precip": "0.0",
            }},
            Exception("UV API failed"),  # _fetch_uv_index 会 catch
        ]

        tool = WeatherToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["uv_index"], 0)  # UV 失败默认 0

    def test_live_source_is_live(self):
        """测试 source 字段标记为 live。"""
        tool = WeatherToolLive(make_mock_client())
        self.assertEqual(tool.source, "live")

    def test_live_execute_wraps_as_tool_result(self):
        """测试通过 execute() 调用时正确包装为 ToolResult。"""
        client = make_mock_client()
        client.get.side_effect = [
            {"code": "200", "now": {
                "temp": "28", "icon": "100", "windScale": "2", "precip": "0.0",
            }},
            {"code": "200", "daily": [{"category": "5"}]},
        ]

        tool = WeatherToolLive(client)
        result = tool.execute(city="北京")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data["temperature_c"], 28.0)


class TestWeatherWarningLive(unittest.TestCase):
    """WeatherWarningToolLive 测试。"""

    def test_live_no_warnings(self):
        """测试无预警时返回空列表。"""
        client = make_mock_client()
        client.get.return_value = {"code": "200", "warning": []}

        tool = WeatherWarningToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["city"], "北京")
        self.assertFalse(result["has_warning"])
        self.assertEqual(result["warnings"], [])

    def test_live_with_warnings(self):
        """测试有暴雨预警时正确返回。"""
        client = make_mock_client()
        client.get.return_value = {
            "code": "200",
            "warning": [{
                "title": "北京市气象台发布暴雨橙色预警",
                "type": "暴雨",
                "level": "橙色",
                "text": "预计未来3小时降雨量将达50毫米以上",
            }],
        }

        tool = WeatherWarningToolLive(client)
        result = tool._run(city="北京")

        self.assertTrue(result["has_warning"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["type"], "暴雨")

    def test_live_source_is_live(self):
        tool = WeatherWarningToolLive(make_mock_client())
        self.assertEqual(tool.source, "live")


class TestAirQualityLive(unittest.TestCase):
    """AirQualityToolLive 测试。"""

    def test_live_field_mapping(self):
        """测试空气质量字段映射。"""
        client = make_mock_client()
        client.get.return_value = {
            "code": "200",
            "now": [{
                "aqi": "85", "category": "良", "pm2p5": "42.0",
                "pm10": "65.0", "no2": "30.0", "so2": "8.0",
                "co": "0.8", "o3": "55.0",
            }],
        }

        tool = AirQualityToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["city"], "北京")
        self.assertEqual(result["aqi"], 85)
        self.assertEqual(result["category"], "良")
        self.assertEqual(result["pm25"], 42.0)
        self.assertEqual(result["pm10"], 65.0)

    def test_live_empty_response(self):
        """测试 API 返回空时默认值。"""
        client = make_mock_client()
        client.get.return_value = {"code": "200", "now": []}

        tool = AirQualityToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["aqi"], 0)
        self.assertEqual(result["category"], "未知")

    def test_live_source_is_live(self):
        tool = AirQualityToolLive(make_mock_client())
        self.assertEqual(tool.source, "live")


class TestWeatherForecastLive(unittest.TestCase):
    """WeatherForecastToolLive 测试。"""

    def test_live_field_mapping(self):
        """测试逐小时预报字段映射。"""
        client = make_mock_client()
        client.get.return_value = {
            "code": "200",
            "hourly": [
                {"fxTime": "2026-08-05T14:00+08:00", "temp": "30",
                 "iconCode": "100", "text": "晴", "precip": "0.0"},
                {"fxTime": "2026-08-05T15:00+08:00", "temp": "31",
                 "iconCode": "310", "text": "暴雨", "precip": "5.0"},
            ],
        }

        tool = WeatherForecastToolLive(client)
        result = tool._run(city="北京", hours=2)

        self.assertEqual(result["city"], "北京")
        self.assertEqual(len(result["hours"]), 2)
        self.assertEqual(result["hours"][0]["temp"], 30.0)
        self.assertEqual(result["hours"][0]["condition"], "晴")
        self.assertEqual(result["hours"][1]["condition"], "暴雨")
        self.assertEqual(result["hours"][1]["rain_probability"], 80)
        self.assertIn("1小时可能降雨", result["summary"])

    def test_live_no_rain_summary(self):
        """测试无降雨时摘要正确。"""
        client = make_mock_client()
        client.get.return_value = {
            "code": "200",
            "hourly": [
                {"fxTime": "2026-08-05T14:00+08:00", "temp": "30",
                 "iconCode": "100", "precip": "0.0"},
            ],
        }

        tool = WeatherForecastToolLive(client)
        result = tool._run(city="北京", hours=1)

        self.assertIn("无降雨", result["summary"])

    def test_live_source_is_live(self):
        tool = WeatherForecastToolLive(make_mock_client())
        self.assertEqual(tool.source, "live")


class TestMapLive(unittest.TestCase):
    """MapToolLive 测试：mock AmapClient，验证字段映射。"""

    def make_mock_amap_client(self):
        """构造一个 mock AmapClient。"""
        client = MagicMock(spec=AmapClient)
        return client

    def test_live_search_poi_field_mapping(self):
        """测试 POI 搜索字段映射。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {
                "name": "故宫博物院",
                "lat": 39.916,
                "lng": 116.397,
                "address": "北京市东城区景山前街4号",
                "tel": "010-85007421",
                "type": "风景名胜",
            },
            {
                "name": "故宫北门",
                "lat": 39.925,
                "lng": 116.396,
                "address": "北京市东城区景山前街",
                "tel": "",
                "type": "风景名胜",
            },
        ]

        tool = MapToolLive(client)
        result = tool._run(action="search_poi", query="故宫")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "故宫博物院")
        self.assertEqual(result[0]["lat"], 39.916)
        self.assertEqual(result[0]["lng"], 116.397)
        self.assertEqual(result[0]["address"], "北京市东城区景山前街4号")
        self.assertEqual(result[0]["open"], "")    # 高德无此字段
        self.assertEqual(result[0]["price"], 0.0)  # 高德无此字段

    def test_live_route_transit(self):
        """测试公交路线规划字段映射。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),  # origin: 故宫
            (39.882, 116.407),  # destination: 天坛
        ]
        client.get_route.return_value = {
            "distance": 3500,   # 米
            "duration": 1500,   # 秒
        }

        tool = MapToolLive(client)
        result = tool._run(action="route", origin="故宫", destination="天坛", mode="transit")

        self.assertEqual(result["from"], "故宫")
        self.assertEqual(result["to"], "天坛")
        self.assertEqual(result["distance_km"], 3.5)    # 3500m → 3.5km
        self.assertEqual(result["duration_min"], 25)      # 1500s → 25min
        self.assertEqual(result["transit"], "公交")
        self.assertEqual(result["fare"], 0.0)

    def test_live_route_driving(self):
        """测试驾车路线规划。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        client.get_route.return_value = {
            "distance": 4200,
            "duration": 900,
        }

        tool = MapToolLive(client)
        result = tool._run(action="route", origin="故宫", destination="天坛", mode="driving")

        self.assertEqual(result["distance_km"], 4.2)
        self.assertEqual(result["duration_min"], 15)  # 900s → 15min
        self.assertEqual(result["transit"], "驾车")

    def test_live_route_riding(self):
        """测试骑行路线规划。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        client.get_route.return_value = {
            "distance": 2800,
            "duration": 600,
        }

        tool = MapToolLive(client)
        result = tool._run(action="route", origin="故宫", destination="天坛", mode="riding")

        self.assertEqual(result["distance_km"], 2.8)
        self.assertEqual(result["duration_min"], 10)
        self.assertEqual(result["transit"], "骑行")

    def test_live_route_walk(self):
        """测试步行路线规划。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.925, 116.396),
        ]
        client.get_route.return_value = {
            "distance": 1000,
            "duration": 720,
        }

        tool = MapToolLive(client)
        result = tool._run(action="route", origin="故宫", destination="景山公园", mode="walk")

        self.assertEqual(result["distance_km"], 1.0)
        self.assertEqual(result["duration_min"], 12)
        self.assertEqual(result["transit"], "步行")

    def test_live_source_is_live(self):
        """测试 source 字段标记为 live。"""
        tool = MapToolLive(self.make_mock_amap_client())
        self.assertEqual(tool.source, "live")

    def test_live_execute_wraps_as_tool_result(self):
        """测试通过 execute() 调用时正确包装为 ToolResult。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {"name": "故宫", "lat": 39.916, "lng": 116.397, "address": "", "tel": "", "type": ""},
        ]

        tool = MapToolLive(client)
        result = tool.execute(action="search_poi", query="故宫")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data[0]["name"], "故宫")

    def test_live_route_calls_geocode_twice(self):
        """测试路线规划时调用了两次 geocode（起点+终点）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        client.get_route.return_value = {"distance": 1000, "duration": 600}

        tool = MapToolLive(client)
        tool._run(action="route", origin="故宫", destination="天坛")

        self.assertEqual(client.geocode.call_count, 2)


if __name__ == "__main__":
    unittest.main()
