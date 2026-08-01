"""天气 Tool：气温、降雨、紫外线、风力等（对应 Weather Agent 的 API 封装）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tools.base_tool import BaseTool
from tools.mock_data import CITY_MOCK, MockWorld


class WeatherTool(BaseTool):
    name = "weather"
    description = "查询城市天气：天气状况、气温、降雨概率、紫外线、风力。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD，缺省为当天"},
        },
        "required": ["city"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, city: str = "", date: Optional[str] = None) -> Dict[str, Any]:
        w = self._world.get_weather()
        return {
            "city": city or CITY_MOCK,
            "date": date or self._world.now.date().isoformat(),
            "condition": w.condition,
            "temperature_c": w.temperature_c,
            "rain_probability": w.rain_probability,
            "uv_index": w.uv_index,
            "wind_kmh": w.wind_kmh,
        }
