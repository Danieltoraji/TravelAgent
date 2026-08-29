"""weather_brief 技能：实况 + 逐时预报 + 空气质量 + 预警的聚合简报（P2a）。

组合 weather / weather_forecast / air_quality / weather_warning 四个原子工具，
产出面向"出行要不要调整安排"的单一意图视图。单段失败降级为空段，不整体失败；
消费方：execution_agent 事件佐证、LLM 白名单（P4）、C 端简报卡片。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core.schemas import ToolStatus
from tools.skill import Skill

logger = logging.getLogger("tools.weather_brief")


class WeatherBriefSkill(Skill):
    name = "weather_brief"
    description = "出行天气简报：实况、未来24小时逐时趋势、空气质量与预警的聚合视图（单段失败自动降级）。"
    source = "mock"
    domain = "weather"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
        },
        "required": ["city"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string"},
            "current": {"type": "object", "description": "实况（condition/temperature_c/rain_probability...）"},
            "forecast_hours": {"type": "array", "description": "逐时预报 [{time,temp,condition,rain_probability}]"},
            "air_quality": {"type": "object", "description": "aqi/pm25/category..."},
            "warnings": {"type": "array", "description": "生效预警列表"},
            "summary": {"type": "string", "description": "一句话出行简报"},
        },
        "required": ["city", "summary"],
    }

    def __init__(self, world: Any = None) -> None:
        super().__init__()
        from tools.mock_data import MockWorld
        from tools.weather_tool import (
            AirQualityTool,
            WeatherForecastTool,
            WeatherTool,
            WeatherWarningTool,
        )
        world = world or MockWorld()
        self._weather = WeatherTool(world)
        self._forecast = WeatherForecastTool(world)
        self._air = AirQualityTool(world)
        self._warning = WeatherWarningTool(world)

    def _run(self, city: str = "") -> Dict[str, Any]:
        if not city:
            raise ValueError("city 不能为空（必填参数）")
        return self._aggregate(city)

    def _aggregate(self, city: str) -> Dict[str, Any]:
        current = self._section(self._weather, city=city)
        forecast = self._section(self._forecast, city=city, hours=24)
        air = self._section(self._air, city=city)
        warning = self._section(self._warning, city=city)

        hours = forecast.get("hours", []) if isinstance(forecast, dict) else []
        warnings = warning.get("warnings", []) if isinstance(warning, dict) else []

        rain = current.get("rain_probability", 0)
        summary_parts = [
            f"{current.get('condition', '未知')}，{current.get('temperature_c', '?')}°C，"
            f"降水概率{rain}%",
        ]
        aqi = air.get("aqi")
        if aqi is not None:
            summary_parts.append(f"AQI {aqi}（{air.get('category', '')}）".strip())
        if warnings:
            summary_parts.append(f"{len(warnings)} 条生效预警，注意调整安排")
        elif rain and rain >= 60:
            summary_parts.append("降雨概率较高，建议备伞或调整户外安排")

        return {
            "city": city,
            "current": current,
            "forecast_hours": hours,
            "air_quality": air,
            "warnings": warnings,
            "summary": "；".join(summary_parts),
        }

    @staticmethod
    def _section(tool: Any, **kwargs: Any) -> Dict[str, Any]:
        """单段 best-effort：失败/空 → 空段（聚合视图不因单段故障整体失败）。"""
        try:
            result = tool.execute(**kwargs)
        except Exception as exc:  # noqa: BLE001  工具实现异常同样降级
            logger.warning("weather_brief 段调用失败 (%s): %s", type(tool).__name__, exc)
            return {}
        if result.status != ToolStatus.OK or not isinstance(result.data, dict):
            return {}
        return result.data


class WeatherBriefSkillLive(WeatherBriefSkill):
    """Live 版：内部组装四个 Live 天气工具（共享 QWeatherClient 与 MockWorld override）。"""

    source = "live"

    def __init__(self, client: Any, world: Any = None) -> None:
        super().__init__(world)
        from tools.weather_tool import (
            AirQualityToolLive,
            WeatherForecastToolLive,
            WeatherToolLive,
            WeatherWarningToolLive,
        )
        self._weather = WeatherToolLive(client, world)
        self._forecast = WeatherForecastToolLive(client)
        self._air = AirQualityToolLive(client)
        self._warning = WeatherWarningToolLive(client)
