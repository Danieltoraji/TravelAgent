"""火车票 Tool 组：余票 / 中转换乘 / 经停站 / 票价（12306 官方接口直连）。

Mock 版返回固定演示数据；Live 版通过共享的 TrainClient 调 12306 真实接口，
两者输出结构完全一致（list[dict]），调用方零改动。
注册方式：build_registry() 按 settings.use_real_train_api 自动选择。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List

from tools.base_tool import BaseTool
from tools.train.client import (
    parse_price_row,
    parse_ticket_row,
    parse_transfer_item,
    parse_route_station,
    validate_depart_date,
)
from tools.train.stations import resolve_station, station_name

logger = logging.getLogger("tools.train")

# 车次号（G39 / K1234）；官方编号含小写字母或更长，不会命中
_TRAIN_CODE_RE = re.compile(r"[A-Z]+\d+")


class TrainTicketTool(BaseTool):
    name = "train_ticket"
    description = "火车余票查询：出发/到达站（中文名、全拼或电报码）+ 日期，返回车次、时刻、历时、各坐席余票。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "from_station": {"type": "string", "description": "出发车站，如 北京南 / beijingnan / VNP"},
            "to_station": {"type": "string", "description": "到达车站，如 上海虹桥 / AOH"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 14 天内）"},
            "purpose_codes": {"type": "string", "description": "乘客类型：ADULT=成人（默认），0X=学生"},
            "limit": {"type": "integer", "description": "返回车次数量上限，默认 20"},
        },
        "required": ["from_station", "to_station", "date"],
    }

    def _run(self, from_station: str = "", to_station: str = "",
             date: str = "", purpose_codes: str = "ADULT",
             limit: int = 20) -> List[Dict[str, Any]]:
        # Mock：固定车次（与 Live 输出同构）
        return [
            {"code": "G39", "train_no": "24000000G390I", "status": "预订",
             "from_station": "北京南", "to_station": "上海虹桥",
             "from_station_code": "VNP", "to_station_code": "AOH",
             "depart_time": "08:00", "arrive_time": "13:24", "duration": "05:24",
             "seats": {"business": "12", "first_class": "有", "second_class": "23",
                       "no_seat": "有"}},
            {"code": "D311", "train_no": "24000000D3110", "status": "预订",
             "from_station": "北京南", "to_station": "上海虹桥",
             "from_station_code": "VNP", "to_station_code": "AOH",
             "depart_time": "19:21", "arrive_time": "01:52", "duration": "06:31",
             "seats": {"soft_sleeper": "无", "dongwo": "候补", "second_class": "8"}},
        ]


class TrainTicketToolLive(TrainTicketTool):
    """12306 余票查询 Live 版。

    车站先转电报码再查询；返回所有可购车次（含同城其他车站出发的车次），
    limit 截断数量。输出与 Mock 版同构。
    """

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, from_station: str = "", to_station: str = "",
             date: str = "", purpose_codes: str = "ADULT",
             limit: int = 20) -> List[Dict[str, Any]]:
        if not from_station or not to_station:
            raise ValueError("出发站和到达站不能为空")
        validate_depart_date(date)
        from_code = resolve_station(from_station)
        to_code = resolve_station(to_station)

        rows = self._client.query_tickets(from_code, to_code, date,
                                          purpose_codes=purpose_codes)
        trains: List[Dict[str, Any]] = []
        for row in rows:
            parsed = parse_ticket_row(row)
            if parsed is None:
                continue
            # C3：中文名与电报码并存（与 Mock 同构），电报码供经停/票价联动使用
            parsed["from_station"] = station_name(parsed["from_station_code"])
            parsed["to_station"] = station_name(parsed["to_station_code"])
            trains.append(parsed)

        logger.info("Train tickets: %s(%s)→%s(%s) %s → %d 车次",
                    from_station, from_code, to_station, to_code, date, len(trains))
        if limit and limit > 0:
            trains = trains[:limit]
        return trains


class TrainTransferTool(BaseTool):
    name = "train_transfer"
    description = "火车中转换乘方案查询：两站间无直达车时的两段换乘方案（含换乘等待时间与总历时）。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "from_station": {"type": "string", "description": "出发车站"},
            "to_station": {"type": "string", "description": "到达车站"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 14 天内）"},
            "middle_station": {"type": "string", "description": "指定中转站（可选）"},
            "purpose_codes": {"type": "string", "description": "乘客类型：00=普通（默认），0X=学生"},
            "max_results": {"type": "integer", "description": "返回方案数量上限，默认 10"},
        },
        "required": ["from_station", "to_station", "date"],
    }

    def _run(self, from_station: str = "", to_station: str = "", date: str = "",
             middle_station: str = "", purpose_codes: str = "00",
             max_results: int = 10) -> List[Dict[str, Any]]:
        # Mock：固定一个换乘方案
        return [
            {"middle_station": "南京南", "wait_time": "28分", "total_duration": "06:10",
             "segments": [
                 {"code": "G111", "from_station": "北京南", "to_station": "南京南",
                  "depart_time": "08:00", "arrive_time": "11:15", "duration": "03:15",
                  "seats": {"second_class": "有", "first_class": "12"}},
                 {"code": "G7357", "from_station": "南京南", "to_station": "上海虹桥",
                  "depart_time": "11:43", "arrive_time": "12:43", "duration": "01:00",
                  "seats": {"second_class": "15"}},
             ]},
        ]


class TrainTransferToolLive(TrainTransferTool):
    """12306 中转换乘 Live 版（客户端自动翻页抓全后截断 max_results）。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, from_station: str = "", to_station: str = "", date: str = "",
             middle_station: str = "", purpose_codes: str = "00",
             max_results: int = 10) -> List[Dict[str, Any]]:
        if not from_station or not to_station:
            raise ValueError("出发站和到达站不能为空")
        validate_depart_date(date)
        from_code = resolve_station(from_station)
        to_code = resolve_station(to_station)
        middle_code = resolve_station(middle_station) if middle_station else ""

        items = self._client.query_transfer(from_code, to_code, date,
                                            middle_code=middle_code,
                                            purpose_codes=purpose_codes)
        transfers = []
        for item in items:
            parsed = parse_transfer_item(item)
            if parsed is not None:
                transfers.append(parsed)

        logger.info("Train transfer: %s→%s %s → %d 方案",
                    from_station, to_station, date, len(transfers))
        if max_results and max_results > 0:
            transfers = transfers[:max_results]
        return transfers


class TrainRouteTool(BaseTool):
    name = "train_route"
    description = "列车经停站查询：输入车次号（如 G39）或官方编号，返回全部经停站与到发时刻、停站时长。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "train": {"type": "string", "description": "车次号（如 G39）或官方编号（如 5l000G155600）"},
            "from_station": {"type": "string", "description": "上车车站"},
            "to_station": {"type": "string", "description": "下车车站"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 14 天内）"},
        },
        "required": ["train", "from_station", "to_station", "date"],
    }

    def _run(self, train: str = "", from_station: str = "", to_station: str = "",
             date: str = "") -> List[Dict[str, Any]]:
        # Mock：固定经停表
        return [
            {"station_no": "01", "station_name": "北京南", "arrive_time": "----",
             "depart_time": "08:00", "stopover_time": "----"},
            {"station_no": "02", "station_name": "济南西", "arrive_time": "09:41",
             "depart_time": "09:43", "stopover_time": "2分"},
            {"station_no": "03", "station_name": "南京南", "arrive_time": "11:45",
             "depart_time": "11:48", "stopover_time": "3分"},
            {"station_no": "04", "station_name": "上海虹桥", "arrive_time": "13:24",
             "depart_time": "----", "stopover_time": "----"},
        ]


class TrainRouteToolLive(TrainRouteTool):
    """12306 经停站 Live 版。车次号入参会先经余票接口转换为官方编号。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, train: str = "", from_station: str = "", to_station: str = "",
             date: str = "") -> List[Dict[str, Any]]:
        if not train:
            raise ValueError("车次号不能为空")
        if not from_station or not to_station:
            raise ValueError("出发站和到达站不能为空")
        validate_depart_date(date)
        from_code = resolve_station(from_station)
        to_code = resolve_station(to_station)
        train_no = self._client.resolve_train_no(train, from_code, to_code, date)

        stations = self._client.query_route(train_no, from_code, to_code, date)
        result = [parse_route_station(st) for st in stations]
        logger.info("Train route: %s(%s) %s→%s %s → %d 站",
                    train, train_no, from_station, to_station, date, len(result))
        return result


class TrainPriceTool(BaseTool):
    name = "train_price"
    description = "火车票价查询：两站间全部（或指定车次的）坐席票价。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "from_station": {"type": "string", "description": "出发车站"},
            "to_station": {"type": "string", "description": "到达车站"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 14 天内）"},
            "train": {"type": "string", "description": "车次号过滤（如 G39），可选；缺省返回全部车次"},
            "purpose_codes": {"type": "string", "description": "乘客类型：ADULT=成人（默认），0X=学生"},
        },
        "required": ["from_station", "to_station", "date"],
    }

    def _run(self, from_station: str = "", to_station: str = "", date: str = "",
             train: str = "", purpose_codes: str = "ADULT") -> List[Dict[str, Any]]:
        # Mock：固定一条票价
        return [
            {"code": "G39", "train_no": "24000000G390I",
             "from_station": "北京南", "to_station": "上海虹桥",
             "depart_time": "08:00", "arrive_time": "13:24", "duration": "05:24",
             "prices": {"business": 2120.0, "first_class": 1060.0,
                        "second_class": 662.0}},
        ]


class TrainPriceToolLive(TrainPriceTool):
    """12306 票价 Live 版。价格接口按线路返回全部车次，train 参数做客户端过滤。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, from_station: str = "", to_station: str = "", date: str = "",
             train: str = "", purpose_codes: str = "ADULT") -> List[Dict[str, Any]]:
        if not from_station or not to_station:
            raise ValueError("出发站和到达站不能为空")
        validate_depart_date(date)
        from_code = resolve_station(from_station)
        to_code = resolve_station(to_station)

        rows = self._client.query_price(from_code, to_code, date,
                                        purpose_codes=purpose_codes)
        target = (train or "").strip().upper()
        result = []
        for dto in rows:
            parsed = parse_price_row(dto)
            if target and parsed["code"].upper() != target:
                continue
            result.append(parsed)

        logger.info("Train price: %s→%s %s train=%s → %d 条",
                    from_station, to_station, date, target or "全部", len(result))
        return result
