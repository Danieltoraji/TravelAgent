"""hotel_book 动作技能：酒店预订意图组装（P3）。

``prepare``：按城市/酒店名查询并组装预订意图（房价/地址/预订落地页），
幂等、无副作用； RollingGo MCP 当前仅暴露 3 个只读工具（0829 探测：
searchHotels/getHotelDetail/getHotelSearchTags），**无下单通道**——
``commit`` 拒绝直调，确认/下单/支付走人工与批准链路（booking_url 落地页）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict

from core.schemas import ToolStatus
from tools.action_skill import ActionSkill

logger = logging.getLogger("tools.hotel_book")


class HotelBookSkill(ActionSkill):
    name = "hotel_book"
    description = (
        "酒店预订意图：查询并组装预订信息（房价/地址/预订落地页），产出待确认"
        "预订单。不自动下单、不代付。"
    )
    domain = "hotel"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "酒店所在城市"},
            "hotel_name": {"type": "string", "description": "酒店名称（必填）"},
            "checkin_date": {"type": "string", "description": "入住日期 YYYY-MM-DD"},
            "checkout_date": {"type": "string", "description": "离店日期 YYYY-MM-DD"},
            "guests": {"type": "integer", "description": "入住人数（默认 1）"},
        },
        "required": ["hotel_name"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "intent": {"type": "string"},
            "hotel_id": {"type": "string"},
            "hotel_name": {"type": "string"},
            "price_per_night": {"type": "number"},
            "address": {"type": "string"},
            "booking_url": {"type": "string"},
            "payment": {"type": "string"},
        },
        "required": ["intent", "hotel_name"],
    }

    def __init__(self, world: Any = None) -> None:
        super().__init__()
        from tools.hotel_tool import HotelTool
        self._hotel = HotelTool()

    def prepare(self, city: str = "", hotel_name: str = "",
                checkin_date: str = "", checkout_date: str = "",
                guests: int = 1) -> Dict[str, Any]:
        if not hotel_name:
            raise ValueError("hotel_name 不能为空")
        result = self._hotel.execute(
            action="search", place=city or "北京", size=20,
        )
        if result.status != ToolStatus.OK or not result.data:
            raise ValueError(f"酒店查询失败: {result.error or '无结果'}")
        hotels = result.data.get("hotels", [])
        match = next((h for h in hotels if isinstance(h, dict) and (
            h.get("name") == hotel_name
            or hotel_name in str(h.get("name", ""))
            or str(h.get("name", "")) in hotel_name
        )), None)
        if match is None:
            raise ValueError(f"未找到酒店: {hotel_name}")

        logger.info("hotel_book intent: %s (%s) → 已组装", hotel_name, match["id"])
        return {
            "intent": "hotel_booking",
            "hotel_id": match["id"],
            "hotel_name": match["name"],
            "city": city,
            "checkin_date": checkin_date,
            "checkout_date": checkout_date,
            "guests": guests,
            "price_per_night": match.get("price_per_night", 0.0),
            "address": match.get("address", ""),
            "booking_url": match.get("booking_url", ""),
            "payment": "MANUAL（Agent 不代付）",
            "note": "预订意图已组装；确认与下单须经批准链路，付款需人工完成",
        }

    def commit(self, **kwargs: Any) -> Any:
        # 0829 探测：RollingGo MCP 仅 3 个只读工具，无下单通道——
        # 本形态即终态：意图 + booking_url 落地页 + 人工支付
        raise RuntimeError(
            "hotel_book.commit 需真实下单通道（当前 RollingGo 无 order 工具）；"
            "请走批准链路并以 booking_url 落地页人工完成预订与支付"
        )


class HotelBookSkillLive(HotelBookSkill):
    """Live 版：内部组装 HotelToolLive（RollingGo MCP）。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        from tools.hotel_tool import HotelToolLive
        self._hotel = HotelToolLive(client)
