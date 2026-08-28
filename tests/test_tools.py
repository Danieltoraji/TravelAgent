"""工具层测试：注册表、各 Tool 的 Mock 调用、统一返回契约。"""

import json
import unittest
from unittest.mock import MagicMock, patch

from core.schemas import ToolStatus
from tools import default_registry
from tools.amap_client import AmapClient
from tools.map_tool import MapTool, MapToolLive
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
    def test_registry_has_all_eleven_tools(self) -> None:
        names = default_registry.names()
        for tool in ["map", "weather", "weather_warning", "air_quality",
                     "weather_forecast", "scenic", "traffic", "food", "booking",
                     "web_fetch", "web_search"]:
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


class TestRetry(unittest.TestCase):
    """BaseTool 重试机制测试。"""

    def test_retry_on_network_error(self):
        """网络错误应重试，最终成功。"""
        from tools.base_tool import BaseTool
        from config.settings import settings

        # 临时设置快速重试
        old_retries = settings.max_retries
        old_backoff = settings.retry_backoff_base
        settings.max_retries = 2
        settings.retry_backoff_base = 0.01

        call_count = 0

        class FlakyTool(BaseTool):
            name = "flaky"
            description = "test"
            source = "mock"

            def _run(self, **kwargs):
                nonlocal call_count
                call_count += 1
                if call_count < 2:
                    from urllib.error import URLError
                    raise URLError("timeout")
                return {"ok": True}

        try:
            tool = FlakyTool()
            result = tool.execute()
            self.assertEqual(result.status, ToolStatus.OK)
            self.assertEqual(call_count, 2)
        finally:
            settings.max_retries = old_retries
            settings.retry_backoff_base = old_backoff

    def test_no_retry_on_value_error(self):
        """业务错误（ValueError）不应重试。"""
        from tools.base_tool import BaseTool
        from config.settings import settings

        old_retries = settings.max_retries
        settings.max_retries = 3

        call_count = 0

        class FailTool(BaseTool):
            name = "fail"
            description = "test"
            source = "mock"

            def _run(self, **kwargs):
                nonlocal call_count
                call_count += 1
                raise ValueError("business error")

        try:
            tool = FailTool()
            result = tool.execute()
            self.assertEqual(result.status, ToolStatus.ERROR)
            self.assertEqual(call_count, 1)
        finally:
            settings.max_retries = old_retries

    def test_retry_exhausted_returns_error(self):
        """重试耗尽后返回 ERROR。"""
        from tools.base_tool import BaseTool
        from config.settings import settings

        old_retries = settings.max_retries
        old_backoff = settings.retry_backoff_base
        settings.max_retries = 1
        settings.retry_backoff_base = 0.01

        call_count = 0

        class AlwaysFailTool(BaseTool):
            name = "always_fail"
            description = "test"
            source = "mock"

            def _run(self, **kwargs):
                nonlocal call_count
                call_count += 1
                from urllib.error import URLError
                raise URLError("timeout")

        try:
            tool = AlwaysFailTool()
            result = tool.execute()
            self.assertEqual(result.status, ToolStatus.ERROR)
            self.assertEqual(call_count, 2)  # 1 initial + 1 retry
        finally:
            settings.max_retries = old_retries
            settings.retry_backoff_base = old_backoff


class TestSchemaFieldAlignment(unittest.TestCase):
    """验证数据结构对齐后的新字段存在且有默认值。"""

    def test_place_has_id_and_end_time(self) -> None:
        from core.schemas import Place
        p = Place(name="故宫")
        self.assertTrue(hasattr(p, "id"))
        self.assertEqual(p.id, "")
        self.assertTrue(hasattr(p, "end_time"))
        self.assertEqual(p.end_time, "")

    def test_triptimeline_has_new_fields(self) -> None:
        from datetime import date
        from core.schemas import TripTimeline
        tl = TripTimeline(city="北京", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2))
        self.assertTrue(hasattr(tl, "id"))
        self.assertEqual(tl.id, "")
        self.assertTrue(hasattr(tl, "total_cost"))
        self.assertEqual(tl.total_cost, 0.0)
        self.assertTrue(hasattr(tl, "walking_distance"))
        self.assertEqual(tl.walking_distance, 0.0)

    def test_monitorevent_has_spot_id(self) -> None:
        from datetime import datetime
        from core.schemas import MonitorEvent, EventType
        ev = MonitorEvent(
            event_id="test-1", event_type=EventType.WEATHER,
            place="北京", observed_at=datetime.now(), rule_name="test",
        )
        self.assertTrue(hasattr(ev, "spot_id"))
        self.assertEqual(ev.spot_id, "")

    def test_replanrequest_has_new_fields(self) -> None:
        from core.schemas import ReplanRequest
        req = ReplanRequest(reason="test")
        self.assertTrue(hasattr(req, "need_replan"))
        self.assertTrue(req.need_replan)
        self.assertTrue(hasattr(req, "impact"))
        self.assertEqual(req.impact, 0.0)
        self.assertTrue(hasattr(req, "affected_spots"))
        self.assertEqual(req.affected_spots, [])

    def test_actionitem_has_new_fields(self) -> None:
        from core.schemas import ActionItem
        item = ActionItem(action_id="act-1", title="test")
        self.assertTrue(hasattr(item, "type"))
        self.assertEqual(item.type, "")
        self.assertTrue(hasattr(item, "date"))
        self.assertEqual(item.date, "")
        self.assertTrue(hasattr(item, "quantity"))
        self.assertEqual(item.quantity, 0)


class TestMapBatchRoute(unittest.TestCase):
    """B3：批量 ETA（batch_route）— Mock 与 Live 行结构 + 规范字段 transport_minutes。"""

    def make_mock_amap_client(self) -> AmapClient:
        return MagicMock(spec=AmapClient)

    def test_mock_batch_route_returns_rows(self) -> None:
        from tools.map_tool import MapTool

        tool = MapTool()
        rows = tool._run(
            action="batch_route",
            origins=["故宫", "天坛"],
            destinations=["景山公园", "王府井"],
        )
        self.assertEqual(len(rows), 4)
        row = rows[0]
        self.assertEqual(set(row), {"origin", "destination", "distance_km",
                                    "transport_minutes", "mode", "fare"})
        self.assertEqual(row["origin"], "故宫")
        self.assertEqual(row["transport_minutes"], 25)  # Mock 固定值
        self.assertEqual(rows[3]["destination"], "王府井")

    def test_mock_route_has_transport_minutes(self) -> None:
        from tools.map_tool import MapTool

        result = MapTool()._run(action="route", origin="故宫", destination="天坛")
        self.assertEqual(result["duration_min"], 25)
        self.assertEqual(result["transport_minutes"], 25)

    def test_live_batch_route_maps_rows(self) -> None:
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.916, 116.397),  # 故宫
            (39.882, 116.407),  # 天坛
            (39.925, 116.396),  # 景山公园
        ]
        client.get_distances.return_value = [
            {"origin": (39.916, 116.397), "destination": (39.925, 116.396),
             "distance_m": 2100, "duration_s": 900},
            {"origin": (39.882, 116.407), "destination": (39.925, 116.396),
             "distance_m": 3600, "duration_s": 1500},
        ]

        tool = MapToolLive(client)
        rows = tool._run(
            action="batch_route",
            origins=["故宫", "天坛"],
            destinations=["景山公园"],
            city="北京",
        )
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["origin"], "故宫")
        self.assertEqual(rows[1]["origin"], "天坛")
        self.assertEqual(rows[0]["transport_minutes"], 15)   # 900s → 15min
        self.assertEqual(rows[0]["distance_km"], 2.1)
        self.assertEqual(rows[1]["transport_minutes"], 25)   # 1500s → 25min
        # 地理编码限定城市
        for call in client.geocode.call_args_list:
            self.assertEqual(call.kwargs.get("city"), "北京")
        # 批量测量（驾车近似）
        client.get_distances.assert_called_once_with(
            [(39.916, 116.397), (39.882, 116.407)],
            [(39.925, 116.396)],
            mode="driving",
        )

    def test_live_batch_route_coord_direct_skips_geocode(self) -> None:
        """B 档：坐标字符串（"lng,lat"）直连，跳过地理编码（消灭怪名 POI 30001）。"""
        client = self.make_mock_amap_client()
        client.get_distances.return_value = [
            {"origin": (39.916, 116.397), "destination": (39.882, 116.407),
             "distance_m": 5000, "duration_s": 1500},
        ]
        rows = MapToolLive(client)._run(
            action="batch_route",
            origins=["116.397,39.916"],
            destinations=["116.407,39.882"],
            city="上海",
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["transport_minutes"], 25)
        client.geocode.assert_not_called()
        client.get_distances.assert_called_once_with(
            [(39.916, 116.397)], [(39.882, 116.407)], mode="driving"
        )

    def test_live_route_coord_direct_skips_geocode(self) -> None:
        client = self.make_mock_amap_client()
        client.get_route.return_value = {"distance": 3500, "duration": 1500, "cost": 6}
        result = MapToolLive(client)._run(
            action="route",
            origin="116.397,39.916",
            destination="116.407,39.882",
            mode="driving",
            city="上海",
        )
        self.assertEqual(result["transport_minutes"], 25)
        client.geocode.assert_not_called()

    def test_live_route_has_transport_minutes(self) -> None:
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [(39.916, 116.397), (39.882, 116.407)]
        client.get_route.return_value = {"distance": 3500, "duration": 1500, "cost": 6}

        result = MapToolLive(client)._run(
            action="route", origin="故宫", destination="天坛", mode="transit"
        )
        self.assertEqual(result["duration_min"], 25)
        self.assertEqual(result["transport_minutes"], 25)


class TestScenicSearch(unittest.TestCase):
    """B5：scenic action=search 城市候选池 — 输出对齐 A 侧 spot dict 字段。"""

    def make_mock_amap_client(self) -> AmapClient:
        return MagicMock(spec=AmapClient)

    def test_mock_scenic_search_returns_spot_dicts(self) -> None:
        from tools.scenic_tool import ScenicTool

        spots = ScenicTool()._run(action="search", place="北京", limit=3)
        self.assertEqual(len(spots), 3)
        spot = spots[0]
        self.assertEqual(set(spot), {
            "id", "name", "alias", "location", "suggest_duration",
            "opening_time", "closing_time", "price", "tags", "rating",
        })
        self.assertEqual(spot["name"], "故宫")
        self.assertEqual(spot["opening_time"], "08:30")
        self.assertEqual(spot["closing_time"], "17:00")
        self.assertEqual(spot["price"], 60.0)
        self.assertEqual(spot["suggest_duration"], 120)
        self.assertEqual(spot["location"], {"lat": 39.916, "lng": 116.397})

    def test_live_scenic_search_field_mapping(self) -> None:
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
                "opentime_today": "08:30-17:00",
                "tag": "故宫,紫禁城",
                "alias": "紫禁城",
            },
            {
                "name": "天坛公园",
                "lat": 39.882,
                "lng": 116.407,
                "address": "",
                "tel": "",
                "type": "风景名胜",
                "rating": 4.6,
                "cost": 15.0,
                "opentime_today": "06:00-22:00",
                "tag": "",
                "alias": "",
            },
        ]

        tool = ScenicToolLive(client)
        spots = tool._run(action="search", place="北京", limit=2)

        self.assertEqual(len(spots), 2)
        first = spots[0]
        self.assertEqual(first["id"], "scenic_0")
        self.assertEqual(first["name"], "故宫博物院")
        self.assertEqual(first["location"], {"lat": 39.916, "lng": 116.397})
        self.assertEqual(first["opening_time"], "08:30")
        self.assertEqual(first["closing_time"], "17:00")
        self.assertEqual(first["price"], 60.0)
        self.assertEqual(first["suggest_duration"], 120)
        self.assertEqual(first["tags"][0], "风景名胜")  # type 大类置首
        self.assertIn("故宫", first["tags"])
        self.assertEqual(first["alias"], ["紫禁城"])
        self.assertEqual(first["rating"], 4.8)
        # 搜索「城市+景点」
        self.assertEqual(client.search_poi.call_args.args[0], "北京 景点")
        self.assertEqual(client.search_poi.call_args.kwargs["city"], "北京")

    def test_live_scenic_search_fallback_broadens_query(self) -> None:
        client = self.make_mock_amap_client()
        client.search_poi.side_effect = [
            [],  # 「北京 景点」无结果
            [{"name": "北京城", "lat": 39.9, "lng": 116.4, "opentime_today": "",
              "type": "", "tag": "", "cost": 0, "rating": 0}],
        ]

        spots = ScenicToolLive(client)._run(action="search", place="北京")
        self.assertEqual(len(spots), 1)
        self.assertEqual(spots[0]["name"], "北京城")
        # 回落：第二次按城市名搜索
        self.assertEqual(client.search_poi.call_args_list[1].args[0], "北京")
        # 无营业时间 → 默认 09:00-17:00
        self.assertEqual(spots[0]["opening_time"], "09:00")
        self.assertEqual(spots[0]["closing_time"], "17:00")

    def test_live_scenic_search_messy_opentime_sanitized(self) -> None:
        """真实高德 opentime_today 杂乱（多段/无分隔符）→ 取首个区间，不炸下游 _parse_time。"""
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {"name": "深夜书房", "lat": 30.6, "lng": 104.06, "opentime_today": "14:00 18:30-22:00",
             "type": "", "tag": "", "cost": 0, "rating": 0},
            {"name": "两段开放", "lat": 30.6, "lng": 104.07, "opentime_today": "09:00-12:00;14:00-18:00",
             "type": "", "tag": "", "cost": 0, "rating": 0},
            {"name": "乱文", "lat": 30.6, "lng": 104.08, "opentime_today": "随时开放",
             "type": "", "tag": "", "cost": 0, "rating": 0},
        ]
        spots = ScenicToolLive(client)._run(action="search", place="成都", limit=3)
        self.assertEqual(spots[0]["opening_time"], "18:30")
        self.assertEqual(spots[0]["closing_time"], "22:00")
        self.assertEqual(spots[1]["opening_time"], "09:00")
        self.assertEqual(spots[1]["closing_time"], "12:00")
        self.assertEqual(spots[2]["opening_time"], "09:00")   # 无法解析 → 默认
        self.assertEqual(spots[2]["closing_time"], "17:00")

    def test_live_scenic_status_unaffected(self) -> None:
        client = self.make_mock_amap_client()
        client.search_poi.return_value = [
            {"name": "故宫", "opentime_today": "08:30-17:00", "rating": 4.8,
             "address": "x", "tel": "y", "opentime_week": ""},
        ]
        result = ScenicToolLive(client)._run(place="故宫")  # 默认 status
        self.assertIn("place", result)
        self.assertIn("queue_min", result)
        self.assertNotIn("suggest_duration", result)  # status 模式不带 search 字段
        self.assertEqual(client.search_poi.call_args.kwargs["limit"], 1)


class TestAmapClientDistance(unittest.TestCase):
    """AmapClient 批量距离测量（/v3/distance）提取与多终点分请求。"""

    def test_extract_distance_rows(self) -> None:
        resp = {
            "status": "1",
            "results": [
                {"origin_id": "1", "destination": "116.397,39.916",
                 "distance": "2100", "duration": "900"},
                {"origin_id": "2", "destination": "116.397,39.916",
                 "distance": "3600", "duration": "1500"},
                {"origin_id": "3", "destination": "116.397,39.916",
                 "distance": "", "duration": ""},  # 非法 → 跳过
            ],
        }
        rows = AmapClient._extract_distance_rows(resp)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0], {"origin_id": 0, "distance_m": 2100,
                                   "duration_s": 900})
        self.assertEqual(rows[1], {"origin_id": 1, "distance_m": 3600,
                                   "duration_s": 1500})

    def test_get_distances_multiple_destinations(self) -> None:
        client = AmapClient(api_key="test-key")
        client._get = MagicMock(side_effect=[
            # 终点 A（北京故宫）— 高德 origin_id 从 1 开始
            {"status": "1", "results": [
                {"origin_id": "1", "distance": "2100", "duration": "900"}]},
            # 终点 B（天坛）
            {"status": "1", "results": [
                {"origin_id": "1", "distance": "4200", "duration": "1200"}]},
        ])

        origins = [(39.916, 116.397)]
        destinations = [(39.925, 116.396), (39.882, 116.407)]
        rows = client.get_distances(origins, destinations, mode="driving")

        self.assertEqual(len(rows), 2)
        self.assertEqual(client._get.call_count, 2)  # 每个终点一次请求
        self.assertEqual(rows[0]["origin"], (39.916, 116.397))
        self.assertEqual(rows[0]["destination"], (39.925, 116.396))
        self.assertEqual(rows[0]["duration_s"], 900)
        self.assertEqual(rows[1]["destination"], (39.882, 116.407))

        # 参数格式校验：origins 用 | 分隔 lng,lat；type=1 驾车
        first_params = client._get.call_args_list[0][0][1]
        self.assertEqual(first_params["origins"], "116.397,39.916")
        self.assertEqual(first_params["destination"], "116.396,39.925")
        self.assertEqual(first_params["type"], "1")

    def test_get_distances_chunks_over_100_origins(self) -> None:
        client = AmapClient(api_key="test-key")

        def fake_get(path, params):  # 第一分片 100 条，第二分片 20 条（贴近真实 API）
            page = 100 if params["origins"].count("|") == 99 else 20
            return {
                "status": "1",
                "results": [
                    {"origin_id": str(i + 1), "distance": str(1000 + i),
                     "duration": "600"}   # 高德 1 基
                    for i in range(page)
                ],
            }

        client._get = MagicMock(side_effect=fake_get)

        origins = [(39.9 + i * 0.001, 116.3 + i * 0.001) for i in range(120)]
        rows = client.get_distances(origins, [(39.925, 116.396)])
        # 120 起点 → 2 个分片请求，各返回 100/20 条 → 共 120 行
        self.assertEqual(client._get.call_count, 2)
        self.assertEqual(len(rows), 120)
        self.assertEqual(rows[0]["origin"], origins[0])
        self.assertEqual(rows[-1]["origin"], origins[-1])  # 第二分片 origin_id 偏移修正

    def test_get_distances_skips_out_of_range_origin_id(self) -> None:
        """异常响应（origin_id 越界/非法）跳过而不是抛 IndexError。"""
        client = AmapClient(api_key="test-key")
        client._get = MagicMock(return_value={
            "status": "1",
            "results": [
                {"origin_id": "1", "distance": "1000", "duration": "600"},  # 合法（1 基→索引 0）
                {"origin_id": "5", "distance": "2000", "duration": "700"},  # 越界（仅 1 个起点）
            ],
        })
        rows = client.get_distances([(39.9, 116.3)], [(39.925, 116.396)])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["origin"], (39.9, 116.3))

    def test_get_distances_rejects_unsupported_mode(self) -> None:
        client = AmapClient(api_key="test-key")
        with self.assertRaises(ValueError, msg="批量测量仅支持 driving / walk"):
            client.get_distances([(1.0, 1.0)], [(2.0, 2.0)], mode="transit")

    def test_get_distances_retries_transient_then_recovers(self) -> None:
        """B 档：单终点瞬时 10021 → 退避重试后成功（矩阵不丢行）。"""
        from unittest.mock import patch

        client = AmapClient(api_key="test-key")
        # 第一次调用抛 10021，第二次成功
        client._get = MagicMock(side_effect=[
            ValueError("高德 API 错误 [10021]: CUQPS_HAS_EXCEEDED_THE_LIMIT"),
            {"status": "1", "results": [
                {"origin_id": "1", "distance": "2100", "duration": "900"}]},
        ])
        with patch("tools.amap_client._DISTANCE_INTER_REQUEST_DELAY", 0), \
             patch("tools.amap_client._DISTANCE_RETRY_BACKOFF", 0):
            rows = client.get_distances(
                [(39.916, 116.397)], [(39.925, 116.396)]
            )
        self.assertEqual(len(rows), 1)
        self.assertEqual(client._get.call_count, 2, "瞬时错误应重试一次")

    def test_get_distances_skips_destination_after_persistent_failure(self) -> None:
        """B 档：单终点持续失败 → 跳过该终点（不拖垮整矩阵），其余终点照常返回。"""
        from unittest.mock import patch

        client = AmapClient(api_key="test-key")
        client._get = MagicMock(side_effect=[
            ValueError("高德 API 错误 [10021]: CUQPS_HAS_EXCEEDED_THE_LIMIT"),  # 终点A 持续失败
            ValueError("高德 API 错误 [10021]: CUQPS_HAS_EXCEEDED_THE_LIMIT"),
            ValueError("高德 API 错误 [10021]: CUQPS_HAS_EXCEEDED_THE_LIMIT"),
            {"status": "1", "results": [          # 终点B 成功
                {"origin_id": "1", "distance": "4200", "duration": "1200"}]},
        ])
        with patch("tools.amap_client._DISTANCE_INTER_REQUEST_DELAY", 0), \
             patch("tools.amap_client._DISTANCE_RETRY_BACKOFF", 0):
            rows = client.get_distances(
                [(39.916, 116.397)],
                [(39.925, 116.396), (39.882, 116.407)],
            )
        self.assertEqual(len(rows), 1, "终点A 失败应跳过，只保留终点B 的行")
        self.assertEqual(rows[0]["destination"], (39.882, 116.407))


class TestMapIntercity(unittest.TestCase):
    """批次 1a：map 工具城际模式（train/air）估算兜底。

    - mode enum 含 train/air；表外城市对回退 driving（Mock 固定值 / Live 高德真源）；
    - Live 版必须在调 get_route 前拦截（高德无 train/air 端点，透传会 ValueError）。
    """

    def make_mock_amap_client(self):
        return MagicMock(spec=AmapClient)

    def test_schema_enum_includes_train_air(self) -> None:
        mode_enum = MapTool.input_schema["properties"]["mode"]["enum"]
        self.assertIn("train", mode_enum)
        self.assertIn("air", mode_enum)
        # Live 版 schema 与基类同步（train/air 可被上层 LLM 看到）
        self.assertEqual(
            MapToolLive.input_schema["properties"]["mode"]["enum"], mode_enum
        )

    def test_mock_route_train_returns_estimate(self) -> None:
        result = MapTool()._run(
            action="route", origin="北京", destination="上海", mode="train"
        )
        self.assertEqual(result["mode"], "train")
        self.assertEqual(result["source"], "estimate")
        self.assertEqual(result["duration_min"], 280)
        self.assertEqual(result["transport_minutes"], 280)   # 兼容规范字段
        self.assertEqual(result["cost_per_person"], 553.0)
        self.assertEqual(result["to"], "上海")
        # 车站粒度（8.28 估算表升级）：train 具体到车站对
        self.assertEqual(result["from_station"], "北京南站")
        self.assertEqual(result["to_station"], "上海虹桥站")
        self.assertIn("北京南→上海虹桥", result["transit_text"])

    def test_mock_route_air_returns_estimate(self) -> None:
        result = MapTool()._run(
            action="route", origin="上海", destination="成都", mode="air"
        )
        self.assertEqual(result["mode"], "air")
        self.assertEqual(result["source"], "estimate")
        self.assertEqual(result["duration_min"], 200)
        self.assertEqual(result["cost_per_person"], 1300.0)
        # 车站粒度：air 具体到机场对
        self.assertEqual(result["from_station"], "上海虹桥国际机场")
        self.assertEqual(result["to_station"], "成都天府国际机场")

    def test_mock_route_driving_has_no_station(self) -> None:
        """driving 保持城市级：Mock 走固定值（无站点字段）；估算表 driving 条目供阶段二 provider 选模式。"""
        from tools.map_tool import _lookup_intercity_estimate

        result = MapTool()._run(
            action="route", origin="北京", destination="上海", mode="driving"
        )
        self.assertEqual(result["source"], "mock")
        self.assertNotIn("from_station", result)
        # 表内 driving 估算条目可查（阶段二 live provider 选 driving 模式时消费）
        opt = _lookup_intercity_estimate("北京", "上海", "driving")
        self.assertIsNotNone(opt)
        self.assertEqual(opt["transport_minutes"], 720)
        self.assertEqual(opt["cost_per_person"], 450.0)
        self.assertNotIn("from_station", opt)

    def test_mock_route_train_missing_edge_falls_back_driving(self) -> None:
        """表外城市对（未收录）→ 回退 driving 固定值，不报错。"""
        result = MapTool()._run(
            action="route", origin="北京", destination="广州", mode="train"
        )
        self.assertEqual(result["mode"], "driving")
        self.assertNotEqual(result.get("source"), "estimate")
        self.assertIn("distance_km", result)

    def test_mock_route_transit_unchanged(self) -> None:
        """回归：默认市内 transit 仍走 Mock 固定值结构（重构不破坏现状）。"""
        result = MapTool()._run(action="route", origin="故宫", destination="天坛")
        self.assertEqual(result["distance_km"], 3.5)
        self.assertEqual(result["transport_minutes"], 25)
        self.assertEqual(result["source"], "mock")

    def test_live_route_train_intercepts_before_get_route(self) -> None:
        """Live 版 train 必须在 get_route 前拦截（防 ValueError「不支持的路线模式」）。"""
        client = self.make_mock_amap_client()
        tool = MapToolLive(client)
        result = tool._run(
            action="route", origin="北京", destination="上海", mode="train"
        )
        self.assertEqual(result["source"], "estimate")
        self.assertEqual(result["duration_min"], 280)
        client.get_route.assert_not_called()
        client.geocode.assert_not_called()

    def test_live_route_train_missing_edge_falls_back_driving_live(self) -> None:
        """表外城市对 → 回退高德 driving 真源（geocode + get_route mode=driving）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (39.9, 116.4),   # 北京
            (23.1, 113.3),   # 广州
        ]
        client.get_route.return_value = {
            "distance": 2_200_000,   # 2200km
            "duration": 72_000,      # 1200min
        }
        tool = MapToolLive(client)
        result = tool._run(
            action="route", origin="北京", destination="广州", mode="train"
        )
        self.assertEqual(result["mode"], "driving")
        self.assertEqual(result["source"], "live")
        self.assertEqual(result["duration_min"], 1200)
        client.get_route.assert_called_once()
        self.assertEqual(client.get_route.call_args.kwargs["mode"], "driving")

    def test_live_route_fallback_geocodes_by_own_city(self) -> None:
        """防 30001：城际回退两端坐标按各自城市名限定 geocode，
        不沿用统一默认 city=北京 解析他城城市名（线上复探实测 30001）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [
            (43.8256, 87.6168),   # 乌鲁木齐
            (39.9042, 116.4074),  # 北京
        ]
        client.get_route.return_value = {"distance": 3_300_000, "duration": 108_000}
        tool = MapToolLive(client)
        # 逆程（乌鲁木齐→北京）也应各自限定，而不是统一 city=北京
        result = tool._run(
            action="route", origin="乌鲁木齐", destination="北京", mode="train"
        )
        self.assertEqual(result["mode"], "driving")
        self.assertEqual(result["source"], "live")
        self.assertEqual(client.geocode.call_count, 2)
        self.assertEqual(client.geocode.call_args_list[0].args[0], "乌鲁木齐")
        self.assertEqual(client.geocode.call_args_list[0].kwargs["city"], "乌鲁木齐")
        self.assertEqual(client.geocode.call_args_list[1].args[0], "北京")
        self.assertEqual(client.geocode.call_args_list[1].kwargs["city"], "北京")

    def test_live_route_unknown_mode_rejected(self) -> None:
        """mode 校验：enum 之外的模式（如 taxi）仍被 amap_client 拒绝（不静默吞错）。"""
        client = self.make_mock_amap_client()
        client.geocode.side_effect = [(39.9, 116.4), (39.9, 116.4)]
        client.get_route.side_effect = ValueError("不支持的路线模式: taxi")
        tool = MapToolLive(client)
        with self.assertRaises(ValueError):
            tool._run(action="route", origin="故宫", destination="天坛", mode="taxi")


if __name__ == "__main__":
    unittest.main()
