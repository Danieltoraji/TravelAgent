"""预约 Tool：景点预约、酒店预订（只准备，不付款）。

对应《任务整理.md》模块3 的 Booking Agent：
"这里只负责准备预约，不负责付款。可以替用户完成信息填写等大多数工作，但是付款必须人工。"

三个 action：
  - prepare : 准备预约（填写信息），生成 draft，不付款
  - submit  : 模拟提交预约（Mock），生成 confirm_code
  - status  : 按 booking_id 查询预约状态
"""

from __future__ import annotations

import uuid
from typing import Any, Dict

from tools.base_tool import BaseTool


class BookingTool(BaseTool):
    name = "booking"
    description = "预约服务：为景点/酒店准备预约（填写信息），不涉及付款。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "enum": ["prepare", "submit", "status"],
                "description": "prepare 准备预约；submit 模拟提交；status 按 booking_id 查询",
            },
            "place": {"type": "string"},
            "booking_id": {"type": "string", "description": "submit/status 时必填"},
            "target_date": {"type": "string", "description": "YYYY-MM-DD"},
            "party_size": {"type": "integer"},
            "booking_type": {
                "type": "string",
                "enum": ["scenic", "hotel", "transport"],
                "description": "预约类型（默认 scenic）",
            },
            "price": {"type": "number", "description": "票价/房费（由 BookingManager 自动填充）"},
            "tel": {"type": "string", "description": "联系电话（由 BookingManager 自动填充）"},
            "ticket_required": {"type": "boolean", "description": "是否需要预约"},
            "address": {"type": "string", "description": "地址"},
            "open_hours": {"type": "string", "description": "营业时间"},
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        super().__init__()
        self._drafts: Dict[str, Dict[str, Any]] = {}

    def _run(self, action: str = "prepare", place: str = "",
             booking_id: str = "", target_date: str = "",
             party_size: int = 1, booking_type: str = "scenic",
             price: float = 0.0, tel: str = "",
             ticket_required: bool = True, address: str = "",
             open_hours: str = "", **kwargs: Any) -> Dict[str, Any]:
        if action == "prepare":
            new_id = uuid.uuid4().hex[:8].upper()
            draft = {
                "booking_id": new_id,
                "place": place,
                "target_date": target_date,
                "party_size": party_size,
                "booking_type": booking_type,
                "price": price,
                "tel": tel,
                "ticket_required": ticket_required,
                "address": address,
                "open_hours": open_hours,
                "status": "draft",
                "payment_required": True,   # 只准备，不付款
                "confirm_code": "",         # submit 后填充
                "note": "信息已填写完毕，等待用户确认后提交；付款需用户手动完成。",
            }
            self._drafts[new_id] = draft
            return draft

        if action == "submit":
            if not booking_id:
                raise ValueError("submit action requires booking_id")
            draft = self._drafts.get(booking_id)
            if draft is None:
                raise ValueError(f"booking not found: {booking_id}")
            if draft["status"] == "submitted":
                # 幂等：已提交则直接返回
                return draft
            draft["status"] = "submitted"
            draft["confirm_code"] = f"CONF-{booking_id}"
            draft["note"] = "预约已提交，等待服务方确认；付款需用户手动完成。"
            return draft

        if action == "status":
            if not booking_id:
                raise ValueError("status action requires booking_id")
            return self._drafts.get(booking_id, {})

        raise ValueError(f"Unknown booking action: {action}")
