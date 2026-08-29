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
    source = "demo_fixture"  # I-04/I-12：固定演示样例，绝不以 live 名义出现
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
        # 固定演示样例（仅在 DEMO_MODE 下注册，I-04）：校验城市对，
        # 绝不把京沪样例塞给任意城市——旧版对任意 from/to 都返回 PEK→SHA 假数据。
        # 京沪样例 + 锦州→常州样例（Demo 验收链）分开维护。
        if (dep, arr) == ("PEK", "SHA"):
            demo: List[Dict[str, Any]] = [
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
            ]
        elif (dep, arr) == ("JNZ", "CZX"):
            # 锦州→常州 验收链样例（有向直飞，演示候选链路用；来源 demo_fixture）
            demo = [
                {"flight_no": "KN5621", "airline": "中国联合航空",
                 "from_airport": "JNZ", "to_airport": "CZX",
                 "depart_time": "08:00", "arrive_time": "09:55",
                 "duration_min": 115, "price": 199.0, "date": date, "status": "scheduled"},
            ]
        else:
            demo = []  # 未收录城市对：诚实返回空，不返回方向错误的京沪样例
        # 展示机场中文名 + 来源标记（补字段，Live 同构）
        for f in demo:
            f["source"] = self.source
            f["from_airport_name"] = airport_name(f["from_airport"])
            f["to_airport_name"] = airport_name(f["to_airport"])
        return demo[: limit]


class FlightSearchToolUnavailable(BaseTool):
    """航班查询不可用（非 Demo 且未配置真源 Key）：抛业务错误，由上层降级估算。

    I-04（四小时作战包）：非 Demo 无 Key 时不得注册固定京沪 Mock 冒充真实数据；
    结构化不可用状态由 A 侧适配层决定估算降级。
    """

    name = "flight_search"
    description = "航班查询不可用：未配置真实航班 API Key（且非 DEMO_MODE）。"
    source = "unavailable"
    input_schema = {
        "type": "object",
        "properties": {
            "from_city": {"type": "string", "description": "出发城市"},
            "to_city": {"type": "string", "description": "到达城市"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD"},
            "limit": {"type": "integer", "description": "返回上限"},
        },
        "required": ["from_city", "to_city", "date"],
    }

    def _run(self, from_city: str = "", to_city: str = "", date: str = "",
             limit: int = 20) -> List[Dict[str, Any]]:
        raise ValueError(
            "航班查询不可用：未配置真实航班 API Key，且当前非 DEMO_MODE"
            "（不返回固定演示航班冒充实时数据，请上层降级估算）"
        )


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