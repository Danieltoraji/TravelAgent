"""工具层测试：注册表、各 Tool 的 Mock 调用、统一返回契约。"""

import json
import unittest
from unittest.mock import MagicMock, patch

from core.schemas import ToolStatus
from tools import default_registry
from tools.amap_client import AmapClient
from tools.map_tool import MapToolLive
from tools.mock_data import MockWorld
from tools.qweather_client import QWeatherClient
from tools.scenic_tool import ScenicToolLive
from tools.food_tool import FoodToolLive
from tools.traffic_tool import TrafficToolLive
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
    client.get_location_coord.return_value = (39.92, 116.41)
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
        # 使用 Mock 版注册表，避免 default_registry 在有 API Key 时走 Live
        from tools.base_tool import ToolRegistry
        from tools.map_tool import MapTool
        reg = ToolRegistry()
        reg.register(MapTool())
        r = reg.call("map", action="search_poi", query="故宫")
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
        from tools.scenic_tool import ScenicTool
        tool = ScenicTool()
        r = tool.execute(place="不存在的地方")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIsNotNone(r.error)

    def test_booking_prepare_no_payment(self) -> None:
        r = default_registry.call("booking", action="prepare",
                                  place="故宫", target_date="2026-08-01", party_size=2)
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertTrue(r.data["payment_required"])
        self.assertIn("booking_id", r.data)
        # 新字段
        self.assertEqual(r.data["booking_type"], "scenic")
        self.assertEqual(r.data["status"], "draft")
        self.assertEqual(r.data["confirm_code"], "")
        for key in ("price", "tel", "ticket_required", "address", "open_hours"):
            self.assertIn(key, r.data)

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

    def test_booking_submit_success(self) -> None:
        """prepare → submit，验证 confirm_code 和状态变化。"""
        prep = default_registry.call("booking", action="prepare",
                                     place="故宫", target_date="2026-08-01", party_size=2)
        bid = prep.data["booking_id"]
        r = default_registry.call("booking", action="submit", booking_id=bid)
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["status"], "submitted")
        self.assertEqual(r.data["confirm_code"], f"CONF-{bid}")

    def test_booking_submit_not_found_errors(self) -> None:
        r = default_registry.call("booking", action="submit", booking_id="NOSUCHID")
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_booking_submit_missing_id_errors(self) -> None:
        r = default_registry.call("booking", action="submit")
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
                "windScale": "3", "windDir": "东北", "humidity": "45", "precip": "0.0",
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
        self.assertEqual(result["wind_dir"], "东北")
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
        client.get.return_value = {"alerts": []}

        tool = WeatherWarningToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["city"], "北京")
        self.assertFalse(result["has_warning"])
        self.assertEqual(result["warnings"], [])

    def test_live_with_warnings(self):
        """测试有暴雨预警时正确返回。"""
        client = make_mock_client()
        client.get.return_value = {
            "alerts": [{
                "headline": "北京市气象台发布暴雨橙色预警",
                "eventType": {"name": "暴雨", "code": "1003"},
                "color": {"code": "orange"},
                "description": "预计未来3小时降雨量将达50毫米以上",
            }],
        }

        tool = WeatherWarningToolLive(client)
        result = tool._run(city="北京")

        self.assertTrue(result["has_warning"])
        self.assertEqual(len(result["warnings"]), 1)
        self.assertEqual(result["warnings"][0]["type"], "暴雨")
        self.assertEqual(result["warnings"][0]["level"], "orange")

    def test_live_source_is_live(self):
        tool = WeatherWarningToolLive(make_mock_client())
        self.assertEqual(tool.source, "live")

    def test_live_api_error_returns_empty(self):
        """测试 API 调用失败（如 403）时优雅降级返回空预警。"""
        client = make_mock_client()
        client.get.side_effect = Exception("HTTP Error 403")

        tool = WeatherWarningToolLive(client)
        result = tool._run(city="北京")

        self.assertFalse(result["has_warning"])
        self.assertEqual(result["warnings"], [])


class TestAirQualityLive(unittest.TestCase):
    """AirQualityToolLive 测试。"""

    def test_live_field_mapping(self):
        """测试空气质量字段映射。"""
        client = make_mock_client()
        client.get.return_value = {
            "indexes": [
                {"code": "us-epa", "aqi": 85, "category": "Moderate"},
            ],
            "pollutants": [
                {"code": "pm2p5", "concentration": {"value": 42.0, "unit": "μg/m3"}},
                {"code": "pm10", "concentration": {"value": 65.0, "unit": "μg/m3"}},
                {"code": "no2", "concentration": {"value": 30.0, "unit": "μg/m3"}},
                {"code": "so2", "concentration": {"value": 8.0, "unit": "μg/m3"}},
                {"code": "co", "concentration": {"value": 0.8, "unit": "mg/m3"}},
                {"code": "o3", "concentration": {"value": 55.0, "unit": "μg/m3"}},
            ],
        }

        tool = AirQualityToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["city"], "北京")
        self.assertEqual(result["aqi"], 85)
        self.assertEqual(result["category"], "Moderate")
        self.assertEqual(result["pm25"], 42.0)
        self.assertEqual(result["pm10"], 65.0)

    def test_live_empty_response(self):
        """测试 API 返回空时默认值。"""
        client = make_mock_client()
        client.get.return_value = {"indexes": [], "pollutants": []}

        tool = AirQualityToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["aqi"], 0)
        self.assertEqual(result["category"], "未知")

    def test_live_source_is_live(self):
        tool = AirQualityToolLive(make_mock_client())
        self.assertEqual(tool.source, "live")

    def test_live_api_error_returns_defaults(self):
        """测试 API 调用失败（如 403）时优雅降级返回默认值。"""
        client = make_mock_client()
        client.get.side_effect = Exception("HTTP Error 403")

        tool = AirQualityToolLive(client)
        result = tool._run(city="北京")

        self.assertEqual(result["aqi"], 0)
        self.assertEqual(result["category"], "未知")


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

    def test_live_iconcode_none_fallback_to_text(self):
        """测试 iconCode 为 None 时 fallback 到 text 字段。"""
        client = make_mock_client()
        client.get.return_value = {
            "code": "200",
            "hourly": [
                {"fxTime": "2026-08-05T14:00+08:00", "temp": "30",
                 "iconCode": None, "text": "晴", "precip": "0.0"},
            ],
        }

        tool = WeatherForecastToolLive(client)
        result = tool._run(city="北京", hours=1)

        self.assertEqual(result["hours"][0]["condition"], "晴")

    def test_live_no_iconcode_uses_text(self):
        """测试无 iconCode 字段时 fallback 到 text 字段。"""
        client = make_mock_client()
        client.get.return_value = {
            "code": "200",
            "hourly": [
                {"fxTime": "2026-08-05T14:00+08:00", "temp": "30",
                 "text": "多云", "precip": "0.0"},
            ],
        }

        tool = WeatherForecastToolLive(client)
        result = tool._run(city="北京", hours=1)

        self.assertEqual(result["hours"][0]["condition"], "多云")

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
                "rating": 4.8,
                "cost": 60.0,
                "opentime_today": "08:30-17:00",
            },
            {
                "name": "故宫北门",
                "lat": 39.925,
                "lng": 116.396,
                "address": "北京市东城区景山前街",
                "tel": "",
                "type": "风景名胜",
                "rating": 0,
                "cost": 0,
                "opentime_today": "",
            },
        ]

        tool = MapToolLive(client)
        result = tool._run(action="search_poi", query="故宫")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "故宫博物院")
        self.assertEqual(result[0]["lat"], 39.916)
        self.assertEqual(result[0]["lng"], 116.397)
        self.assertEqual(result[0]["address"], "北京市东城区景山前街4号")
        self.assertEqual(result[0]["open"], "08:30-17:00")  # 从 opentime_today 填充
        self.assertEqual(result[0]["price"], 60.0)          # 从 cost 填充
        self.assertEqual(result[0]["rating"], 4.8)
        self.assertEqual(result[0]["tel"], "010-85007421")
        self.assertEqual(result[0]["type"], "风景名胜")

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
            "cost": 6,          # 公交票价 6 元
        }

        tool = MapToolLive(client)
        result = tool._run(action="route", origin="故宫", destination="天坛", mode="transit")

        self.assertEqual(result["from"], "故宫")
        self.assertEqual(result["to"], "天坛")
        self.assertEqual(result["distance_km"], 3.5)    # 3500m → 3.5km
        self.assertEqual(result["duration_min"], 25)      # 1500s → 25min
        self.assertEqual(result["transit"], "公交")
        self.assertEqual(result["fare"], 6.0)             # 从 API cost 填充

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


class TestTrafficLive(unittest.TestCase):
    """TrafficToolLive 测试：mock AmapClient，验证字段映射与拥堵推断。"""

    def make_mock_amap_client(self):
        """构造一个 mock AmapClient。"""
        client = MagicMock(spec=AmapClient)
        return client

    def test_live_transit_field_mapping(self):
        """测试公交模式字段映射。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),  # origin: 故宫
            (39.882, 116.407),  # destination: 天坛
        ]
        client.get_route.return_value = {
            "distance": 3500,   # 米
            "duration": 1500,   # 秒
        }

        tool = TrafficToolLive(client)
        result = tool._run(origin="故宫", destination="天坛", mode="transit")

        self.assertEqual(result["origin"], "故宫")
        self.assertEqual(result["destination"], "天坛")
        self.assertEqual(result["mode"], "transit")
        self.assertEqual(result["duration_min"], 25)      # 1500s → 25min
        self.assertEqual(result["congestion"], "畅通")    # 公交不推断拥堵
        self.assertEqual(result["delay_min"], 0)
        self.assertEqual(result["distance_km"], 3.5)    # 3500m → 3.5km
        self.assertIn("3.5km", result["note"])

    def test_live_taxi_congestion_smooth(self):
        """测试打车模式畅通路况（速度高）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        # 10km, 600s = 10min → 速度 60km/h → 畅通
        client.get_route.return_value = {
            "distance": 10000,
            "duration": 600,
        }

        tool = TrafficToolLive(client)
        result = tool._run(origin="故宫", destination="天坛", mode="taxi")

        self.assertEqual(result["mode"], "taxi")
        self.assertEqual(result["duration_min"], 10)
        self.assertEqual(result["congestion"], "畅通")
        self.assertEqual(result["delay_min"], 0)
        self.assertEqual(result["distance_km"], 10.0)  # 10000m → 10.0km

    def test_live_taxi_congestion_heavy(self):
        """测试打车模式拥堵路况（速度低）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        # 3km, 900s = 15min → 速度 12km/h → 拥堵
        client.get_route.return_value = {
            "distance": 3000,
            "duration": 900,
        }

        tool = TrafficToolLive(client)
        result = tool._run(origin="故宫", destination="天坛", mode="taxi")

        self.assertEqual(result["duration_min"], 15)
        self.assertEqual(result["congestion"], "拥堵")
        # 畅通基准: 3km / 40km/h * 60 = 4.5min, delay = 15 - 5 = 10
        self.assertGreater(result["delay_min"], 0)
        self.assertIn("拥堵", result["note"])

    def test_live_taxi_congestion_slow(self):
        """测试打车模式缓行路况（速度中等）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        # 5km, 900s = 15min → 速度 20km/h → 缓行
        client.get_route.return_value = {
            "distance": 5000,
            "duration": 900,
        }

        tool = TrafficToolLive(client)
        result = tool._run(origin="故宫", destination="天坛", mode="taxi")

        self.assertEqual(result["congestion"], "缓行")
        self.assertGreater(result["delay_min"], 0)

    def test_live_walk_field_mapping(self):
        """测试步行模式字段映射。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.925, 116.396),
        ]
        client.get_route.return_value = {
            "distance": 1000,
            "duration": 720,
        }

        tool = TrafficToolLive(client)
        result = tool._run(origin="故宫", destination="景山公园", mode="walk")

        self.assertEqual(result["mode"], "walk")
        self.assertEqual(result["duration_min"], 12)      # 720s → 12min
        self.assertEqual(result["congestion"], "畅通")    # 步行不推断拥堵
        self.assertEqual(result["delay_min"], 0)

    def test_live_source_is_live(self):
        """测试 source 字段标记为 live。"""
        tool = TrafficToolLive(self.make_mock_amap_client())
        self.assertEqual(tool.source, "live")

    def test_live_calls_geocode_twice(self):
        """测试调用了两次 geocode（起点+终点）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        client.get_route.return_value = {"distance": 1000, "duration": 600}

        tool = TrafficToolLive(client)
        tool._run(origin="故宫", destination="天坛")

        self.assertEqual(client.geocode.call_count, 2)

    def test_live_execute_wraps_as_tool_result(self):
        """测试通过 execute() 调用时正确包装为 ToolResult。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        client.get_route.return_value = {"distance": 3500, "duration": 1500}

        tool = TrafficToolLive(client)
        result = tool.execute(origin="故宫", destination="天坛", mode="transit")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data["duration_min"], 25)


class TestScenicLive(unittest.TestCase):
    """ScenicToolLive 测试：mock AmapClient，验证字段映射。"""

    def make_mock_amap_client(self):
        """构造一个 mock AmapClient。"""
        client = MagicMock(spec=AmapClient)
        return client

    def test_live_scenic_field_mapping(self):
        """测试景点搜索字段映射。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {
                "name": "故宫博物院",
                "lat": 39.916,
                "lng": 116.397,
                "address": "北京市东城区景山前街4号",
                "tel": "010-85007421",
                "type": "风景名胜;风景名胜相关",
                "rating": 4.8,
                "cost": 60.0,
                "tag": "故宫,紫禁城",
                "opentime_today": "08:30-17:00",
                "opentime_week": "周一至周日:08:30-17:00",
            },
        ]

        tool = ScenicToolLive(client)
        result = tool._run(place="故宫")

        self.assertEqual(result["place"], "故宫")
        self.assertTrue(result["open"])
        self.assertGreater(result["queue_min"], 0)  # 从 MockWorld 取
        self.assertTrue(result["ticket_required"])
        self.assertEqual(result["price"], 60.0)     # 从 MockWorld 取
        self.assertEqual(result["open_hours"], "08:30-17:00")  # v5 API opentime_today
        self.assertEqual(result["rating"], 4.8)                    # v5 API rating
        self.assertEqual(result["address"], "北京市东城区景山前街4号")
        self.assertEqual(result["tel"], "010-85007421")
        self.assertEqual(result["open_hours_week"], "周一至周日:08:30-17:00")

    def test_live_scenic_unknown_place_uses_defaults(self):
        """测试未知景点使用默认值。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {
                "name": "某小众景点",
                "lat": 40.0,
                "lng": 116.5,
                "address": "某地址",
                "tel": "",
                "type": "风景名胜",
                "rating": 0,
                "cost": 0,
                "tag": "",
                "opentime_today": "",
                "opentime_week": "",
            },
        ]

        tool = ScenicToolLive(client)
        result = tool._run(place="某小众景点")

        self.assertEqual(result["place"], "某小众景点")
        self.assertTrue(result["open"])
        self.assertEqual(result["queue_min"], 20)   # 默认值
        self.assertTrue(result["ticket_required"])  # 默认值
        self.assertEqual(result["price"], 0.0)       # 默认值
        self.assertEqual(result["open_hours"], "")  # API 无数据，MockWorld 也无
        self.assertEqual(result["rating"], 0)        # 默认值
        self.assertEqual(result["address"], "某地址")
        self.assertEqual(result["tel"], "")
        self.assertEqual(result["open_hours_week"], "")

    def test_live_scenic_opentime_priority_over_mockworld(self):
        """测试 v5 API opentime_today 优先于 MockWorld 的 open 字段。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {
                "name": "故宫博物院",
                "lat": 39.916,
                "lng": 116.397,
                "address": "北京市东城区景山前街4号",
                "tel": "010-85007421",
                "type": "风景名胜;风景名胜相关",
                "rating": 4.8,
                "cost": 60.0,
                "tag": "故宫,紫禁城",
                "opentime_today": "09:00-16:30",  # API 返回，与 MockWorld 不同
                "opentime_week": "周一至周日:09:00-16:30",
            },
        ]

        tool = ScenicToolLive(client)
        result = tool._run(place="故宫")

        # MockWorld 中故宫的 open 是 "08:30-17:00"，但 API 返回 "09:00-16:30"
        self.assertEqual(result["open_hours"], "09:00-16:30")

    def test_live_scenic_opentime_fallback_to_mockworld(self):
        """测试 API opentime_today 为空时 fallback 到 MockWorld。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {
                "name": "故宫博物院",
                "lat": 39.916,
                "lng": 116.397,
                "address": "北京市东城区景山前街4号",
                "tel": "010-85007421",
                "type": "风景名胜;风景名胜相关",
                "rating": 4.8,
                "cost": 60.0,
                "tag": "故宫,紫禁城",
                "opentime_today": "",  # API 无数据
                "opentime_week": "",
            },
        ]

        tool = ScenicToolLive(client)
        result = tool._run(place="故宫")

        # API 无数据，fallback 到 MockWorld 的 "08:30-17:00"
        self.assertEqual(result["open_hours"], "08:30-17:00")

    def test_live_scenic_not_found_raises(self):
        """测试景点未找到时抛出 ValueError。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = []

        tool = ScenicToolLive(client)
        with self.assertRaises(ValueError):
            tool._run(place="不存在的景点")

    def test_live_scenic_source_is_live(self):
        """测试 source 字段标记为 live。"""
        tool = ScenicToolLive(self.make_mock_amap_client())
        self.assertEqual(tool.source, "live")

    def test_live_scenic_execute_wraps_as_tool_result(self):
        """测试通过 execute() 调用时正确包装为 ToolResult。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {"name": "故宫", "lat": 39.916, "lng": 116.397,
             "address": "", "tel": "", "type": "", "rating": 0, "cost": 0, "tag": "",
             "opentime_today": "", "opentime_week": ""},
        ]

        tool = ScenicToolLive(client)
        result = tool.execute(place="故宫")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data["place"], "故宫")


class TestFoodLive(unittest.TestCase):
    """FoodToolLive 测试：mock AmapClient，验证字段映射。"""

    def make_mock_amap_client(self):
        """构造一个 mock AmapClient。"""
        client = MagicMock(spec=AmapClient)
        return client

    def test_live_food_without_near(self):
        """测试无位置参数时的餐饮搜索。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {
                "name": "全聚德(前门店)",
                "lat": 39.899,
                "lng": 116.397,
                "address": "北京市东城区前门大街30号",
                "tel": "010-65112418",
                "type": "餐饮服务;中餐厅",
                "rating": 4.6,
                "cost": 180.0,
                "tag": "烤鸭,京菜",
                "opentime_today": "10:00-22:00",
                "opentime_week": "周一至周日:10:00-22:00",
            },
            {
                "name": "护国寺小吃",
                "lat": 39.940,
                "lng": 116.379,
                "address": "北京市西城区护国寺大街",
                "tel": "",
                "type": "餐饮服务;中式快餐",
                "rating": 4.3,
                "cost": 45.0,
                "tag": "小吃",
                "opentime_today": "06:00-20:00",
                "opentime_week": "周一至周日:06:00-20:00",
            },
        ]

        tool = FoodToolLive(client)
        result = tool._run(query="餐厅")

        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["name"], "全聚德(前门店)")
        self.assertEqual(result[0]["rating"], 4.6)
        self.assertEqual(result[0]["price_per_person"], 180.0)
        self.assertTrue(result[0]["open"])
        self.assertEqual(result[0]["distance_km"], 0.0)  # 无 near，无距离
        self.assertEqual(result[0]["cuisine"], "中餐厅")
        self.assertEqual(result[0]["queue_min"], 0)
        self.assertEqual(result[0]["open_hours"], "10:00-22:00")
        self.assertEqual(result[0]["specialty"], "烤鸭,京菜")
        self.assertEqual(result[0]["address"], "北京市东城区前门大街30号")
        self.assertEqual(result[0]["tel"], "010-65112418")

    def test_live_food_with_near(self):
        """测试有位置参数时的周边餐饮搜索。"""
        client = self.make_mock_amap_client()
        client.geocode.return_value = (39.916, 116.397)  # 故宫坐标
        client.search_poi_around.return_value = [
            {
                "name": "附近餐厅A",
                "lat": 39.918,
                "lng": 116.400,
                "address": "某地址",
                "tel": "010-12345678",
                "type": "餐饮服务;中餐厅",
                "rating": 4.5,
                "cost": 120.0,
                "tag": "",
                "distance": 500,  # 米
                "opentime_today": "10:00-22:00",
                "opentime_week": "周一至周日:10:00-22:00",
            },
        ]

        tool = FoodToolLive(client)
        result = tool._run(query="餐厅", near="故宫")

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["name"], "附近餐厅A")
        self.assertEqual(result[0]["rating"], 4.5)
        self.assertEqual(result[0]["price_per_person"], 120.0)
        self.assertEqual(result[0]["distance_km"], 0.5)  # 500m → 0.5km
        self.assertEqual(result[0]["cuisine"], "中餐厅")
        self.assertEqual(result[0]["open_hours"], "10:00-22:00")
        self.assertEqual(result[0]["tel"], "010-12345678")

    def test_live_food_geocode_failure_raises(self):
        """测试地理编码失败时抛出 ValueError。"""
        client = self.make_mock_amap_client()
        client.geocode.return_value = None

        tool = FoodToolLive(client)
        with self.assertRaises(ValueError):
            tool._run(query="餐厅", near="不存在的地点")

    def test_live_food_empty_results(self):
        """测试搜索结果为空时返回空列表。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = []

        tool = FoodToolLive(client)
        result = tool._run(query="不存在的菜系")

        self.assertEqual(result, [])

    def test_live_food_cuisine_extraction(self):
        """测试菜系提取逻辑。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {"name": "A", "lat": 0, "lng": 0, "address": "", "tel": "",
             "type": "餐饮服务;火锅", "rating": 0, "cost": 0, "tag": "",
             "opentime_today": "", "opentime_week": ""},
            {"name": "B", "lat": 0, "lng": 0, "address": "", "tel": "",
             "type": "餐饮服务;西餐", "rating": 0, "cost": 0, "tag": "",
             "opentime_today": "", "opentime_week": ""},
        ]

        tool = FoodToolLive(client)
        result = tool._run(query="餐厅")

        self.assertEqual(result[0]["cuisine"], "火锅")
        self.assertEqual(result[1]["cuisine"], "西餐")

    def test_live_food_source_is_live(self):
        """测试 source 字段标记为 live。"""
        tool = FoodToolLive(self.make_mock_amap_client())
        self.assertEqual(tool.source, "live")

    def test_live_food_execute_wraps_as_tool_result(self):
        """测试通过 execute() 调用时正确包装为 ToolResult。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {"name": "测试餐厅", "lat": 0, "lng": 0, "address": "", "tel": "",
             "type": "中餐", "rating": 4.0, "cost": 50, "tag": "",
             "opentime_today": "", "opentime_week": ""},
        ]

        tool = FoodToolLive(client)
        result = tool.execute(query="餐厅")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data[0]["name"], "测试餐厅")


class TestWeatherLiveOverride(unittest.TestCase):
    """WeatherToolLive + MockWorld override 测试：验证突发事件注入能覆盖 API 数据。"""

    def _make_mock_client(self):
        client = make_mock_client()
        client.get.side_effect = [
            {"code": "200", "now": {
                "temp": "31", "text": "晴", "icon": "100",
                "windScale": "3", "humidity": "45", "precip": "0.0",
                "feelsLike": "33", "vis": "15",
            }},
            {"code": "200", "daily": [{"category": "7"}]},
        ]
        return client

    def test_weather_override_applied(self):
        """set_weather(rain_probability=85) 后，WeatherToolLive 返回的 rain_probability 应为 85。"""
        world = MockWorld()
        world.set_weather(condition="暴雨", rain_probability=85, uv_index=2)

        tool = WeatherToolLive(self._make_mock_client(), world)
        result = tool._run(city="北京")

        self.assertEqual(result["condition"], "暴雨")       # override 覆盖了 API 的 "晴"
        self.assertEqual(result["rain_probability"], 85)      # override 覆盖了 API 的 10
        self.assertEqual(result["uv_index"], 2)               # override 覆盖了 API 的 7
        # 未 override 的字段仍来自 API
        self.assertEqual(result["temperature_c"], 31.0)
        self.assertEqual(result["humidity"], 45)

    def test_weather_no_override_returns_api_data(self):
        """无 override 时，WeatherToolLive 返回纯 API 数据。"""
        world = MockWorld()  # 无 set_weather 调用

        tool = WeatherToolLive(self._make_mock_client(), world)
        result = tool._run(city="北京")

        self.assertEqual(result["condition"], "晴")          # API 原始值
        self.assertEqual(result["rain_probability"], 10)     # API 原始值

    def test_weather_clear_override_restores_api_data(self):
        """clear_weather_overrides() 后恢复纯 API 数据。"""
        world = MockWorld()
        world.set_weather(rain_probability=85)
        world.clear_weather_overrides()

        tool = WeatherToolLive(self._make_mock_client(), world)
        result = tool._run(city="北京")

        self.assertEqual(result["rain_probability"], 10)     # 恢复 API 原始值

    def test_weather_override_partial(self):
        """只 override 部分字段，其余字段仍来自 API。"""
        world = MockWorld()
        world.set_weather(rain_probability=90)  # 只改降雨概率

        tool = WeatherToolLive(self._make_mock_client(), world)
        result = tool._run(city="北京")

        self.assertEqual(result["rain_probability"], 90)      # override 值
        self.assertEqual(result["condition"], "晴")          # API 原始值
        self.assertEqual(result["uv_index"], 7)               # API 原始值


class TestTrafficLiveOverride(unittest.TestCase):
    """TrafficToolLive + MockWorld override 测试：验证交通突发事件注入。"""

    def make_mock_amap_client(self):
        client = MagicMock(spec=AmapClient)
        client.geocode.side_effect = [
            (39.916, 116.397),
            (39.882, 116.407),
        ]
        client.get_route.return_value = {"distance": 3500, "duration": 1500}
        return client

    def test_traffic_override_applied(self):
        """set_traffic_delay() 后，TrafficToolLive 返回的 delay_min 和 congestion 应为 override 值。"""
        world = MockWorld()
        world.set_traffic_delay("北京", "故宫", delay_min=45, congestion="拥堵")

        tool = TrafficToolLive(self.make_mock_amap_client(), world)
        result = tool._run(origin="北京", destination="故宫", mode="transit")

        self.assertEqual(result["delay_min"], 45)             # override 覆盖了 API 的 0
        self.assertEqual(result["congestion"], "拥堵")       # override 覆盖了 API 的 "畅通"
        self.assertIn("拥堵", result["note"])

    def test_traffic_no_override_returns_api_data(self):
        """无 override 时，TrafficToolLive 返回纯 API 数据。"""
        world = MockWorld()

        tool = TrafficToolLive(self.make_mock_amap_client(), world)
        result = tool._run(origin="北京", destination="故宫", mode="transit")

        self.assertEqual(result["delay_min"], 0)              # API 原始值
        self.assertEqual(result["congestion"], "畅通")        # API 原始值

    def test_traffic_clear_override_restores_api_data(self):
        """clear_traffic_overrides() 后恢复纯 API 数据。"""
        world = MockWorld()
        world.set_traffic_delay("北京", "故宫", delay_min=45)
        world.clear_traffic_overrides()

        tool = TrafficToolLive(self.make_mock_amap_client(), world)
        result = tool._run(origin="北京", destination="故宫", mode="transit")

        self.assertEqual(result["delay_min"], 0)              # 恢复 API 原始值

    def test_traffic_override_different_route(self):
        """override 只影响匹配的路线，其他路线不受影响。"""
        world = MockWorld()
        world.set_traffic_delay("北京", "故宫", delay_min=45, congestion="拥堵")

        tool = TrafficToolLive(self.make_mock_amap_client(), world)
        # 查北京→天坛（无 override）
        result = tool._run(origin="北京", destination="天坛", mode="transit")

        self.assertEqual(result["delay_min"], 0)              # 无 override，API 原始值
        self.assertEqual(result["congestion"], "畅通")


class TestMockWorldOverrides(unittest.TestCase):
    """MockWorld override 机制本身测试。"""

    def test_weather_overrides_tracking(self):
        """set_weather() 后 weather_overrides property 返回正确 dict。"""
        world = MockWorld()
        self.assertEqual(world.weather_overrides, {})          # 初始为空

        world.set_weather(condition="暴雨", rain_probability=85)
        self.assertEqual(world.weather_overrides,
                         {"condition": "暴雨", "rain_probability": 85})

    def test_weather_overrides_clear(self):
        """clear_weather_overrides() 清空 override dict。"""
        world = MockWorld()
        world.set_weather(rain_probability=90)
        world.clear_weather_overrides()
        self.assertEqual(world.weather_overrides, {})

    def test_traffic_override_set_and_get(self):
        """set_traffic_delay() + get_traffic_override() 往返测试。"""
        world = MockWorld()

        # 初始无 override
        self.assertIsNone(world.get_traffic_override("北京", "故宫"))

        # 设置 override
        world.set_traffic_delay("北京", "故宫", delay_min=45, congestion="拥堵")

        # 读取 override
        override = world.get_traffic_override("北京", "故宫")
        self.assertIsNotNone(override)
        self.assertEqual(override["delay_min"], 45)
        self.assertEqual(override["congestion"], "拥堵")

    def test_traffic_override_different_routes(self):
        """不同路线的 override 互不影响。"""
        world = MockWorld()
        world.set_traffic_delay("北京", "故宫", delay_min=45)
        world.set_traffic_delay("北京", "天坛", delay_min=20, congestion="缓行")

        gugong = world.get_traffic_override("北京", "故宫")
        tiantan = world.get_traffic_override("北京", "天坛")

        self.assertEqual(gugong["delay_min"], 45)
        self.assertEqual(tiantan["delay_min"], 20)
        self.assertEqual(tiantan["congestion"], "缓行")

    def test_traffic_override_clear(self):
        """clear_traffic_overrides() 清空所有交通 override。"""
        world = MockWorld()
        world.set_traffic_delay("北京", "故宫", delay_min=45)
        world.clear_traffic_overrides()

        self.assertIsNone(world.get_traffic_override("北京", "故宫"))


class TestAmapClientNormalizePoi(unittest.TestCase):
    """AmapClient._normalize_poi() 字段提取测试。"""

    def test_normalize_poi_extracts_alias(self):
        """测试 alias（别名）字段正确提取。"""
        poi = {
            "name": "故宫博物院",
            "location": "116.397,39.916",
            "address": "北京市东城区景山前街4号",
            "type": "风景名胜;风景名胜相关",
            "business": {
                "rating": "4.8",
                "cost": "60",
                "tel": "010-85007421",
                "tag": "故宫,紫禁城",
                "opentime_today": "08:30-17:00",
                "opentime_week": "周一至周日:08:30-17:00",
                "alias": "紫禁城",
                "business_area": "天安门",
            },
        }
        result = AmapClient._normalize_poi(poi)
        self.assertEqual(result["alias"], "紫禁城")

    def test_normalize_poi_extracts_business_area(self):
        """测试 business_area（商圈）字段正确提取。"""
        poi = {
            "name": "某餐厅",
            "location": "116.400,39.920",
            "address": "某地址",
            "type": "餐饮服务;中餐厅",
            "business": {
                "rating": "4.5",
                "cost": "120",
                "tel": "010-12345678",
                "tag": "烤鸭",
                "opentime_today": "10:00-22:00",
                "opentime_week": "",
                "alias": "",
                "business_area": "王府井",
            },
        }
        result = AmapClient._normalize_poi(poi)
        self.assertEqual(result["business_area"], "王府井")

    def test_normalize_poi_alias_empty_when_missing(self):
        """测试 alias 为空时不报错。"""
        poi = {
            "name": "某地点",
            "location": "116.0,40.0",
            "address": "",
            "type": "",
            "business": {},
        }
        result = AmapClient._normalize_poi(poi)
        self.assertEqual(result["alias"], "")
        self.assertEqual(result["business_area"], "")


class TestAmapClientRouteExtractors(unittest.TestCase):
    """AmapClient 路线提取方法测试。"""

    def test_extract_transit_route_cost(self):
        """测试公交路线提取包含 cost（票价）。"""
        resp = {
            "route": {
                "transits": [
                    {"distance": "3500", "duration": "1500", "cost": "6"},
                ],
            },
        }
        result = AmapClient._extract_transit_route(resp)
        self.assertEqual(result["distance"], 3500)
        self.assertEqual(result["duration"], 1500)
        self.assertEqual(result["cost"], 6)

    def test_extract_driving_route_tolls(self):
        """测试驾车路线提取包含 tolls（过路费）。"""
        resp = {
            "route": {
                "paths": [
                    {"distance": "4200", "duration": "900", "tolls": "15"},
                ],
            },
        }
        result = AmapClient._extract_driving_route(resp)
        self.assertEqual(result["distance"], 4200)
        self.assertEqual(result["duration"], 900)
        self.assertEqual(result["tolls"], 15)


if __name__ == "__main__":
    unittest.main()
