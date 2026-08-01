"""工具层测试：注册表、各 Tool 的 Mock 调用、统一返回契约。"""

import unittest

from core.schemas import ToolStatus
from tools import default_registry


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


if __name__ == "__main__":
    unittest.main()
