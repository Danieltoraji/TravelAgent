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

from tools.base_tool import BaseTool, ToolRegistry
from tools.booking_tool import BookingTool
from tools.food_tool import FoodTool
from tools.map_tool import MapTool
from tools.mock_data import PLACES, MockWorld, WeatherData
from tools.scenic_tool import ScenicTool
from tools.traffic_tool import TrafficTool
from tools.weather_tool import WeatherTool, WeatherToolLive


def build_registry(world: MockWorld | None = None) -> ToolRegistry:
    """构建一个注册了全部 6 个领域 Tool 的注册表。

    若传入共享的 MockWorld，天气/景点 Tool 将共享同一模拟世界（Demo 剧情用）。
    当 settings.use_real_api=True 时，天气 Tool 自动切换为和风天气 Live 版。
    """
    from config.settings import settings

    registry = ToolRegistry()
    world = world or MockWorld()
    registry.register(MapTool())

    # 天气 Tool：按配置自动切换 Mock / Live
    if settings.use_real_api:
        weather_tool = WeatherToolLive(
            api_key=settings.qweather_api_key,
            api_host=settings.qweather_api_host,
        )
    else:
        weather_tool = WeatherTool(world)
    registry.register(weather_tool)

    registry.register(ScenicTool(world))
    registry.register(TrafficTool())
    registry.register(FoodTool())
    registry.register(BookingTool())
    return registry


# 默认注册表（独立 MockWorld）。需要共享世界时请用 build_registry(world)。
default_registry: ToolRegistry = build_registry()

__all__ = [
    "PLACES",
    "BaseTool",
    "MockWorld",
    "ToolRegistry",
    "WeatherData",
    "WeatherTool",
    "WeatherToolLive",
    "build_registry",
    "default_registry",
]
