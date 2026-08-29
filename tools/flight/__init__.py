"""航班 Tool 组：航班查询（juhe 聚合数据-航班查询 1962 / aviationstack 双后端）。

工具清单（均 readonly，Mock/Live 输出同构）：
- flight_search 当日航班时刻+票价查询（城市对/机场对）

用法：
    from tools.flight import FlightClient, FlightSearchToolLive
    client = FlightClient(backend="juhe", api_key=...)
    tool = FlightSearchToolLive(client)
    result = tool.execute(from_city="北京", to_city="张掖", date="2026-09-05")
"""

from tools.flight.airports import (
    all_airports,
    airport_name,
    resolve_airport,
    resolve_city_airport,
)
from tools.flight.client import (
    FlightClient,
    parse_avstack_row,
    parse_juhe_row,
    validate_flight_date,
)
from tools.flight.tools import FlightSearchTool, FlightSearchToolLive

__all__ = [
    "FlightClient",
    "FlightSearchTool",
    "FlightSearchToolLive",
    "all_airports",
    "airport_name",
    "parse_avstack_row",
    "parse_juhe_row",
    "resolve_airport",
    "resolve_city_airport",
    "validate_flight_date",
]