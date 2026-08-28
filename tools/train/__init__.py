"""火车票 Tool 组：12306 官方接口直连的只读查询。

工具清单（均 readonly，Mock/Live 输出同构）：
- train_ticket   余票/时刻/坐席
- train_transfer 中转换乘方案
- train_route    经停站
- train_price    票价

用法：
    from tools.train import TrainClient, TrainTicketToolLive
    client = TrainClient()
    tool = TrainTicketToolLive(client)
    result = tool.execute(from_station="北京南", to_station="上海虹桥",
                          date="2026-09-01")
"""

from tools.train.client import (
    TrainClient,
    parse_price_row,
    parse_route_station,
    parse_ticket_row,
    parse_transfer_item,
    validate_depart_date,
)
from tools.train.stations import resolve_station, station_name
from tools.train.tools import (
    TrainPriceTool,
    TrainPriceToolLive,
    TrainRouteTool,
    TrainRouteToolLive,
    TrainTicketTool,
    TrainTicketToolLive,
    TrainTransferTool,
    TrainTransferToolLive,
)

__all__ = [
    "TrainClient",
    "TrainPriceTool",
    "TrainPriceToolLive",
    "TrainRouteTool",
    "TrainRouteToolLive",
    "TrainTicketTool",
    "TrainTicketToolLive",
    "TrainTransferTool",
    "TrainTransferToolLive",
    "parse_price_row",
    "parse_route_station",
    "parse_ticket_row",
    "parse_transfer_item",
    "resolve_station",
    "station_name",
    "validate_depart_date",
]
