"""航班 Tool：航班查询（Mock 固定演示数据 / Live 真源，输出同构）。

Mock 版返回固定演示航班（京沪/沪蓉等），Live 版通过 FlightClient 调
aviationstack / juhe 真源；两者输出结构完全一致（list[dict]），调用方零改动。
注册方式：build_registry() 按 settings.use_real_flight_api 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from tools.base_tool import BaseTool
from tools.flight.airports import (
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

logger = logging.getLogger("tools.flight")


class FlightSearchTool(BaseTool):
    name = "flight_search"
    description = "航班查询：出发/到达城市（或机场三字码）+ 日期，返回当日全部航班（航班号、航司、时刻、历时、票价）。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "from_city": {"type": "string", "description": "出发城市，如 北京 / 天津 或机场三字码 PEK"},
            "to_city": {"type": "string", "description": "到达城市，如 上海 / 张掖 或机场三字码 SHA"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 60 天内）"},
            "limit": {"type": "integer", "description": "返回航班数量上限，默认 20"},
        },
        "required": ["from_city", "to_city", "date"],
    }

    def _run(self, from_city: str = "", to_city: str = "", date: str = "",
             limit: int = 20) -> List[Dict[str, Any]]:
        validate_flight_date(date)
        dep = resolve_city_airport(from_city)
        arr = resolve_city_airport(to_city)
        # Mock：固定演示数据（与 Live 输出同构）
        demo = [
            {"flight_no": "CA1501", "airline": "中国国航",
             "from_airport": "PEK", "to_airport": "SHA",
             "depart_time": "08:00", "arrive_time": "10:25",
             "duration_min": 145, "price": 1080.0, "date": date, "status": "scheduled"},
            {"flight_no": "MU5102", "airline": "东方航空",
             "from_airport": "PEK", "to_airport": "SHA",
             "depart_time": "09:30", "arrive_time": "11:50",
             "duration_min": 140, "price": 960.0, "date": date, "status": "scheduled"},
            {"flight_no": "FM9108", "airline": "上海航空",
             "from_airport": "PEK", "to_airport": "SHA",
             "depart_time": "13:15", "arrive_time": "15:35",
             "duration_min": 140, "price": 820.0, "date": date, "status": "scheduled"},
        ][: limit]
        # 展示机场中文名（补字段，Live 同构）
        for f in demo:
            f["from_airport_name"] = airport_name(f["from_airport"])
            f["to_airport_name"] = airport_name(f["to_airport"])
        return demo


class FlightSearchToolLive(FlightSearchTool):
    """航班查询 Live 版（aviationstack / juhe 真源）。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, from_city: str = "", to_city: str = "", date: str = "",
             limit: int = 20) -> List[Dict[str, Any]]:
        if not from_city or not to_city:
            raise ValueError("出发城市和到达城市不能为空")
        validate_flight_date(date)
        rows = self._client.query_flights(from_city, to_city, date)
        for f in rows:
            f.setdefault("from_airport_name", airport_name(f.get("from_airport", "")))
            f.setdefault("to_airport_name", airport_name(f.get("to_airport", "")))
        if limit and limit > 0:
            rows = rows[:limit]
        return rows