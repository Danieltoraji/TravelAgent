"""ticket_book 动作技能：车票预订意图组装（P3）。

``prepare``：经 train_trip 技能查询班次并组装购票意图（车次/时刻/二等座价），
幂等、无副作用；**边界明示**：12306 无公开自动化购票 API——确认/出票走
12306 官方候补或人工渠道，``commit`` 拒绝直调，支付恒 MANUAL。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core.schemas import ToolStatus
from tools.action_skill import ActionSkill

logger = logging.getLogger("tools.ticket_book")


class TicketBookSkill(ActionSkill):
    name = "ticket_book"
    description = (
        "车票预订意图：查询班次并组装购票信息（车次/时刻/票价）。12306 无公开"
        "自动化购票 API，确认/出票走官方候补或人工渠道，不自动下单、不代付。"
    )
    domain = "train"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "from_station": {"type": "string", "description": "出发城市或车站"},
            "to_station": {"type": "string", "description": "到达城市或车站"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 14 天内）"},
            "preference": {"enum": ["earliest", "cheapest"], "description": "earliest=历时最短（默认）/cheapest=二等座最低价"},
        },
        "required": ["from_station", "to_station", "date"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "from_station": {"type": "string"},
            "to_station": {"type": "string"},
            "date": {"type": "string"},
            "code": {"type": "string"},
            "transport_minutes": {"type": "integer"},
            "price_per_person": {"type": "number"},
            "channel": {"type": "string"},
            "payment": {"type": "string"},
        },
        "required": ["intent", "code", "date"],
    }

    def __init__(self) -> None:
        super().__init__()
        from tools.train.trip import TrainTripSkill
        self._trip = TrainTripSkill()

    def prepare(self, from_station: str = "", to_station: str = "",
                date: str = "", preference: str = "earliest") -> Dict[str, Any]:
        result = self._trip.execute(
            from_city=from_station, to_city=to_station,
            date=date, preference=preference,
        )
        if result.status != ToolStatus.OK or not result.data:
            raise ValueError(f"班次查询失败: {result.error or '无结果'}")
        trip = result.data
        logger.info("ticket_book intent: %s→%s %s (%s) → 已组装",
                    trip["from_station"], trip["to_station"],
                    trip["code"], date)
        return {
            "intent": "ticket_booking",
            "from_station": trip["from_station"],
            "to_station": trip["to_station"],
            "from_station_code": trip.get("from_station_code", ""),
            "to_station_code": trip.get("to_station_code", ""),
            "date": date,
            "code": trip["code"],
            "train_no": trip.get("train_no", ""),
            "depart_time": trip["depart_time"],
            "arrive_time": trip["arrive_time"],
            "transport_minutes": trip["transport_minutes"],
            "price_per_person": trip["cost_per_person"],
            "seats": trip.get("seats", {}),
            "channel": "12306 官方候补/购票渠道（无公开自动化购票 API）",
            "payment": "MANUAL（Agent 不代付）",
            "note": "购票意图已组装；出票须经 12306 官方渠道或人工完成",
        }

    def commit(self, **kwargs: Any) -> Any:
        raise RuntimeError(
            "ticket_book.commit：12306 无公开自动化购票 API，出票须经"
            "12306 官方候补/人工渠道完成；支付恒为 MANUAL（Agent 不代付）"
        )


class TicketBookSkillLive(TicketBookSkill):
    """Live 版：内部组装 TrainTripSkillLive（12306 直连）。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        from tools.train.trip import TrainTripSkillLive
        self._trip = TrainTripSkillLive(client)
