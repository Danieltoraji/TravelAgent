"""weather_brief 技能测试：Mock 聚合、Live 组装、单段降级（P2a）。"""

import unittest
from unittest.mock import MagicMock

from core.schemas import ToolResult, ToolStatus
from tools.weather_brief import WeatherBriefSkill, WeatherBriefSkillLive
from tools.mock_data import MockWorld


def _result(data, status=ToolStatus.OK):
    return ToolResult(tool="t", status=status, data=data, source="mock")


class TestWeatherBriefMock(unittest.TestCase):
    def setUp(self) -> None:
        self.skill = WeatherBriefSkill(MockWorld())

    def test_aggregate_sections(self) -> None:
        r = self.skill.execute(city="北京")
        self.assertEqual(r.status, ToolStatus.OK)
        data = r.data
        self.assertEqual(data["city"], "北京")
        self.assertIn("condition", data["current"])
        self.assertIsInstance(data["forecast_hours"], list)
        self.assertIn("aqi", data["air_quality"])
        self.assertIsInstance(data["warnings"], list)
        self.assertTrue(data["summary"])

    def test_missing_city_errors(self) -> None:
        r = self.skill.execute()
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_skill_kind_marked(self) -> None:
        self.assertEqual(WeatherBriefSkill.kind, "skill")
        spec = self.skill.spec()
        self.assertEqual(spec.kind, "skill")
        self.assertEqual(spec.domain, "weather")
        self.assertEqual(spec.safety, "query")


class TestWeatherBriefLive(unittest.TestCase):
    def _live_skill(self, weather=None, forecast=None, air=None, warning=None):
        skill = WeatherBriefSkillLive.__new__(WeatherBriefSkillLive)
        skill._weather = weather or MagicMock()
        skill._forecast = forecast or MagicMock()
        skill._air = air or MagicMock()
        skill._warning = warning or MagicMock()
        return skill

    def test_aggregates_live_sections(self) -> None:
        skill = self._live_skill(
            weather=MagicMock(execute=MagicMock(return_value=_result(
                {"condition": "小雨", "temperature_c": 22, "rain_probability": 80}))),
            forecast=MagicMock(execute=MagicMock(return_value=_result(
                {"hours": [{"time": "14:00", "temp": 22, "rain_probability": 80}]}))),
            air=MagicMock(execute=MagicMock(return_value=_result(
                {"aqi": 42, "category": "优"}))),
            warning=MagicMock(execute=MagicMock(return_value=_result(
                {"warnings": [{"title": "暴雨蓝色预警"}]}))),
        )
        r = skill.execute(city="北京")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.source, "live")
        self.assertEqual(len(r.data["warnings"]), 1)
        self.assertIn("1 条生效预警", r.data["summary"])
        self.assertIn("小雨", r.data["summary"])

    def test_section_failure_degrades_not_fails(self) -> None:
        # 单段 ERROR → 空段，聚合仍 OK（含异常抛出的工具）
        broken = MagicMock(execute=MagicMock(side_effect=RuntimeError("api down")))
        skill = self._live_skill(weather=broken)
        r = skill.execute(city="北京")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["current"], {})
        self.assertTrue(r.data["summary"])


if __name__ == "__main__":
    unittest.main()
