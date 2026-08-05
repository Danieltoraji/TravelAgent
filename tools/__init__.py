"""工具层：统一抽象 + 各领域 Tool + Mock 数据源。

使用方式：
    from tools import default_registry
    result = default_registry.call("weather", city="北京")

Demo 剧情需共享同一个 MockWorld（以便触发天气/排队变化）时：
    from tools import build_registry
    from tools.mock_data import MockWorld
    world = MockWorld()
    registry = build_registry(world)
"""

from __future__ import annotations

from tools.amap_client import AmapClient
from tools.base_tool import BaseTool, ToolRegistry
from tools.booking_tool import BookingTool
from tools.food_tool import FoodTool, FoodToolLive
from tools.map_tool import MapTool, MapToolLive
from tools.mock_data import PLACES, MockWorld, WeatherData
from tools.qweather_client import QWeatherClient
from tools.scenic_tool import ScenicTool, ScenicToolLive
from tools.traffic_tool import TrafficTool, TrafficToolLive
from tools.weather_tool import (
    AirQualityTool,
    AirQualityToolLive,
    WeatherForecastTool,
    WeatherForecastToolLive,
    WeatherTool,
    WeatherToolLive,
    WeatherWarningTool,
    WeatherWarningToolLive,
)


def build_registry(world: MockWorld | None = None) -> ToolRegistry:
    """构建一个注册了全部 6 个领域 Tool 的注册表。

    若传入共享的 MockWorld，天气/景点 Tool 将共享同一模拟世界（Demo 剧情用）。
    当 settings.use_real_api=True 时，天气 Tool 自动切换为和风天气 Live 版。
    """
    from config.settings import settings

    registry = ToolRegistry()
    world = world or MockWorld()

    # 地图/景点/餐饮 Tool：按配置自动切换 Mock / Live（共享 AmapClient）
    if settings.use_real_map_api:
        amap_client = AmapClient(api_key=settings.amap_api_key)
        registry.register(MapToolLive(amap_client))
        registry.register(TrafficToolLive(amap_client))
        registry.register(ScenicToolLive(amap_client, world))
        registry.register(FoodToolLive(amap_client))
    else:
        registry.register(MapTool())
        registry.register(TrafficTool())
        registry.register(ScenicTool(world))
        registry.register(FoodTool())

    # 天气相关 Tool：按配置自动切换 Mock / Live
    if settings.use_real_api:
        client = QWeatherClient(
            api_key=settings.qweather_api_key,
            api_host=settings.qweather_api_host,
        )
        registry.register(WeatherToolLive(client))
        registry.register(WeatherWarningToolLive(client))
        registry.register(AirQualityToolLive(client))
        registry.register(WeatherForecastToolLive(client))
    else:
        registry.register(WeatherTool(world))
        registry.register(WeatherWarningTool(world))
        registry.register(AirQualityTool(world))
        registry.register(WeatherForecastTool(world))

    registry.register(BookingTool())
    return registry


# 默认注册表（独立 MockWorld）。需要共享世界时请用 build_registry(world)。
default_registry: ToolRegistry = build_registry()

__all__ = [
    "PLACES",
    "AirQualityTool",
    "AirQualityToolLive",
    "AmapClient",
    "BaseTool",
    "FoodTool",
    "FoodToolLive",
    "MapTool",
    "MapToolLive",
    "MockWorld",
    "QWeatherClient",
    "ScenicTool",
    "ScenicToolLive",
    "ToolRegistry",
    "TrafficTool",
    "TrafficToolLive",
    "WeatherData",
    "WeatherForecastTool",
    "WeatherForecastToolLive",
    "WeatherTool",
    "WeatherToolLive",
    "WeatherWarningTool",
    "WeatherWarningToolLive",
    "build_registry",
    "default_registry",
]
