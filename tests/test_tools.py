"""工具层测试：注册表、各 Tool 的 Mock 调用、统一返回契约。"""

import json
import unittest
from unittest.mock import patch

from core.schemas import ToolStatus
from tools import default_registry
from tools.weather_tool import WeatherToolLive


class TestToolRegistry(unittest.TestCase):
    def test_registry_has_all_six_tools(self) -> None:
        names = default_registry.names()
        for tool in ["map", "weather", "scenic", "traffic", "food", "booking"]:
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
    """WeatherToolLive 测试：mock urllib.request.urlopen，验证字段映射。"""

    def _make_mock_response(self, geo_body, now_body, indices_body=None):
        """构造 3 个 mock 响应对象（GeoAPI / 实况 / 指数）。"""
        import io
        import gzip
        from unittest.mock import MagicMock

        def make_resp(body_dict):
            raw = json.dumps(body_dict).encode()
            resp = MagicMock()
            resp.__enter__ = MagicMock(return_value=resp)
            resp.__exit__ = MagicMock(return_value=False)
            resp.read = MagicMock(return_value=raw)
            resp.headers = {"Content-Encoding": None}
            return resp

        responses = [make_resp(geo_body), make_resp(now_body)]
        if indices_body is not None:
            responses.append(make_resp(indices_body))
        return responses

    @patch("tools.weather_tool.urlopen")
    def test_live_field_mapping(self, mock_urlopen):
        """测试和风 API 返回字段正确映射到项目 dict 结构。"""
        geo_resp = {"code": "200", "location": [{"id": "101010100", "name": "北京"}]}
        now_resp = {"code": "200", "now": {
            "temp": "31", "text": "晴", "icon": "100",
            "windScale": "3", "humidity": "45", "precip": "0.0",
        }}
        indices_resp = {"code": "200", "daily": [{"category": "7"}]}

        mock_urlopen.side_effect = self._make_mock_response(
            geo_resp, now_resp, indices_resp)

        tool = WeatherToolLive(api_key="test_key", api_host="test.qweatherapi.com")
        result = tool._run(city="北京")

        self.assertEqual(result["city"], "北京")
        self.assertEqual(result["condition"], "晴")
        self.assertEqual(result["temperature_c"], 31.0)
        self.assertEqual(result["wind_kmh"], 17)  # windScale 3 → 17 km/h
        self.assertEqual(result["uv_index"], 7)
        self.assertEqual(result["rain_probability"], 10)  # precip=0 → 10%

    @patch("tools.weather_tool.urlopen")
    def test_live_rain_probability_from_precip(self, mock_urlopen):
        """测试降水量 >0 时降雨概率推断为 80%。"""
        geo_resp = {"code": "200", "location": [{"id": "101010100"}]}
        now_resp = {"code": "200", "now": {
            "temp": "22", "text": "暴雨", "icon": "310",
            "windScale": "5", "precip": "15.5",
        }}
        indices_resp = {"code": "200", "daily": [{"category": "2"}]}

        mock_urlopen.side_effect = self._make_mock_response(
            geo_resp, now_resp, indices_resp)

        tool = WeatherToolLive(api_key="test_key", api_host="test.qweatherapi.com")
        result = tool._run(city="北京")

        self.assertEqual(result["condition"], "暴雨")
        self.assertEqual(result["rain_probability"], 80)  # precip>0 → 80%

    @patch("tools.weather_tool.urlopen")
    def test_live_location_id_cached(self, mock_urlopen):
        """测试同一城市第二次调用不重复请求 GeoAPI。"""
        geo_resp = {"code": "200", "location": [{"id": "101010100"}]}
        now_resp = {"code": "200", "now": {
            "temp": "28", "icon": "100", "windScale": "2", "precip": "0.0",
        }}
        indices_resp = {"code": "200", "daily": [{"category": "5"}]}

        mock_urlopen.side_effect = self._make_mock_response(
            geo_resp, now_resp, indices_resp)

        tool = WeatherToolLive(api_key="test_key", api_host="test.qweatherapi.com")
        tool._run(city="北京")

        # 第二次调用：GeoAPI 缓存命中，不应再被调用
        # 所以 side_effect 只需 now + indices 两个响应
        now_resp2 = {"code": "200", "now": {
            "temp": "30", "icon": "100", "windScale": "2", "precip": "0.0",
        }}
        indices_resp2 = {"code": "200", "daily": [{"category": "6"}]}
        mock_urlopen.side_effect = self._make_mock_response(
            now_resp2, indices_resp2)  # 只有 2 个响应（无 GeoAPI）

        result = tool._run(city="北京")
        self.assertEqual(result["temperature_c"], 30.0)
        # 验证 Location ID 缓存命中
        self.assertIn("北京", tool._location_cache)

    @patch("tools.weather_tool.urlopen")
    def test_live_uv_failure_defaults_zero(self, mock_urlopen):
        """测试 UV 指数 API 失败时默认返回 0。"""
        geo_resp = {"code": "200", "location": [{"id": "101010100"}]}
        now_resp = {"code": "200", "now": {
            "temp": "25", "icon": "100", "windScale": "1", "precip": "0.0",
        }}

        # 只 mock 2 个响应（GeoAPI + now），indices 调用会 IndexError
        mock_urlopen.side_effect = self._make_mock_response(geo_resp, now_resp)

        tool = WeatherToolLive(api_key="test_key", api_host="test.qweatherapi.com")
        result = tool._run(city="北京")

        self.assertEqual(result["uv_index"], 0)  # UV 失败默认 0

    @patch("tools.weather_tool.urlopen")
    def test_live_geo_not_found_raises(self, mock_urlopen):
        """测试 GeoAPI 找不到城市时抛出 ValueError。"""
        geo_resp = {"code": "200", "location": []}
        mock_urlopen.side_effect = self._make_mock_response(
            geo_resp, {"now": {}}, {"daily": []})

        tool = WeatherToolLive(api_key="test_key", api_host="test.qweatherapi.com")
        with self.assertRaises(ValueError):
            tool._run(city="不存在的城市")

    def test_live_source_is_live(self):
        """测试 source 字段标记为 live。"""
        tool = WeatherToolLive(api_key="k", api_host="h.qweatherapi.com")
        self.assertEqual(tool.source, "live")

    @patch("tools.weather_tool.urlopen")
    def test_live_execute_wraps_as_tool_result(self, mock_urlopen):
        """测试通过 execute() 调用时正确包装为 ToolResult。"""
        geo_resp = {"code": "200", "location": [{"id": "101010100"}]}
        now_resp = {"code": "200", "now": {
            "temp": "28", "icon": "100", "windScale": "2", "precip": "0.0",
        }}
        indices_resp = {"code": "200", "daily": [{"category": "5"}]}

        mock_urlopen.side_effect = self._make_mock_response(
            geo_resp, now_resp, indices_resp)

        tool = WeatherToolLive(api_key="test_key", api_host="test.qweatherapi.com")
        result = tool.execute(city="北京")

        self.assertEqual(result.status, ToolStatus.OK)
        self.assertEqual(result.source, "live")
        self.assertEqual(result.data["temperature_c"], 28.0)


if __name__ == "__main__":
    unittest.main()
