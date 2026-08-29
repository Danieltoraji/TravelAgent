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
from tools.tool_provider import ToolProvider
from tools.booking_tool import BookingTool
from tools.food_tool import FoodTool, FoodToolLive
from tools.hotel_tool import HotelTool, HotelToolLive
from tools.map_tool import MapTool, MapToolLive
from tools.mock_data import PLACES, MockWorld, WeatherData
from tools.qweather_client import QWeatherClient
from tools.rollinggo_client import RollingGoClient
from tools.scenic_tool import ScenicTool, ScenicToolLive
from tools.traffic_tool import TrafficTool, TrafficToolLive
from tools.train import (
    TrainClient,
    TrainPriceTool,
    TrainPriceToolLive,
    TrainRouteTool,
    TrainRouteToolLive,
    TrainTicketTool,
    TrainTicketToolLive,
    TrainTransferTool,
    TrainTransferToolLive,
)
from tools.weather_brief import WeatherBriefSkill, WeatherBriefSkillLive
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
from tools.web_client import WebClient
from tools.web_fetch_tool import WebFetchTool, WebFetchToolLive
from tools.web_search_tool import WebSearchTool, WebSearchToolLive


def build_registry(world: MockWorld | None = None) -> ToolRegistry:
    """构建注册了全部领域 Tool 的注册表。

    若传入共享的 MockWorld，天气/景点 Tool 将共享同一模拟世界（Demo 剧情用）。
    各领域 Tool 按 settings 的 use_real_* 开关自动切换 Mock / Live 版。
    """
    from config.settings import settings

    registry = ToolRegistry()
    world = world or MockWorld()

    # 地图/景点/餐饮 Tool：按配置自动切换 Mock / Live（共享 AmapClient）
    if settings.use_real_map_api:
        amap_client = AmapClient(api_key=settings.amap_api_key)
        registry.register(MapToolLive(amap_client))
        registry.register(TrafficToolLive(amap_client, world))
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
        registry.register(WeatherToolLive(client, world))
        registry.register(WeatherWarningToolLive(client))
        registry.register(AirQualityToolLive(client))
        registry.register(WeatherForecastToolLive(client))
        registry.register(WeatherBriefSkillLive(client, world))
    else:
        registry.register(WeatherTool(world))
        registry.register(WeatherWarningTool(world))
        registry.register(AirQualityTool(world))
        registry.register(WeatherForecastTool(world))
        registry.register(WeatherBriefSkill(world))

    registry.register(BookingTool())

    # 酒店 Tool：按配置自动切换 Mock / Live（RollingGo MCP）
    if settings.use_real_hotel_api:
        rollinggo_client = RollingGoClient(
            url=settings.rollinggo_mcp_url,
            api_key=settings.rollinggo_api_key,
            timeout=settings.rollinggo_mcp_timeout,
            max_retries=settings.rollinggo_mcp_max_retries,
            retry_backoff_base=settings.rollinggo_mcp_retry_backoff_base,
        )
        registry.register(HotelToolLive(rollinggo_client))
    else:
        registry.register(HotelTool())

    # 火车票查询 Tool 组：按配置自动切换 Mock / Live（12306 公开接口，无需 API Key）
    if settings.use_real_train_api:
        train_client = TrainClient()
        registry.register(TrainTicketToolLive(train_client))
        registry.register(TrainTransferToolLive(train_client))
        registry.register(TrainRouteToolLive(train_client))
        registry.register(TrainPriceToolLive(train_client))
    else:
        registry.register(TrainTicketTool())
        registry.register(TrainTransferTool())
        registry.register(TrainRouteTool())
        registry.register(TrainPriceTool())

    # 网页抓取/搜索 Tool：按配置自动切换 Mock / Live（无需 API Key）
    if settings.use_real_web:
        web_client = WebClient()
        registry.register(WebFetchToolLive(web_client))
        registry.register(WebSearchToolLive(web_client))
    else:
        registry.register(WebFetchTool())
        registry.register(WebSearchTool())

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
    "HotelTool",
    "HotelToolLive",
    "MapTool",
    "MapToolLive",
    "MockWorld",
    "QWeatherClient",
    "RollingGoClient",
    "ScenicTool",
    "ScenicToolLive",
    "ToolProvider",
    "ToolRegistry",
    "TrafficTool",
    "TrafficToolLive",
    "TrainClient",
    "TrainPriceTool",
    "TrainPriceToolLive",
    "TrainRouteTool",
    "TrainRouteToolLive",
    "TrainTicketTool",
    "TrainTicketToolLive",
    "TrainTransferTool",
    "TrainTransferToolLive",
    "WeatherBriefSkill",
    "WeatherBriefSkillLive",
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
