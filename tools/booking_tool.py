"""预约 Tool：景点预约、酒店预订（只准备，不付款）。

对应《任务整理.md》模块3 的 Booking Agent：
"这里只负责准备预约，不负责付款。可以替用户完成信息填写等大多数工作，但是付款必须人工。"
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
            "action": {"enum": ["prepare", "status"], "description": "prepare 准备预约；status 按 booking_id 查询"},
            "place": {"type": "string"},
            "booking_id": {"type": "string", "description": "status 查询时必填"},
            "target_date": {"type": "string", "description": "YYYY-MM-DD"},
            "party_size": {"type": "integer"},
        },
        "required": ["action"],
    }

    def __init__(self) -> None:
        super().__init__()
        self._drafts: Dict[str, Dict[str, Any]] = {}

    def _run(self, action: str = "prepare", place: str = "",
             booking_id: str = "", target_date: str = "",
             party_size: int = 1, **kwargs: Any) -> Dict[str, Any]:
        if action == "prepare":
            new_id = uuid.uuid4().hex[:8].upper()
            draft = {
                "booking_id": new_id,
                "place": place,
                "target_date": target_date,
                "party_size": party_size,
                "status": "draft",
                "payment_required": True,   # 只准备，不付款
                "note": "信息已填写完毕，等待用户确认后提交；付款需用户手动完成。",
            }
            self._drafts[new_id] = draft
            return draft
        if action == "status":
            # 修复：prepare 用 booking_id 存，status 必须按 booking_id 查（原按 place 查永远查不到）
            if not booking_id:
                raise ValueError("status action requires booking_id")
            return self._drafts.get(booking_id, {})
        raise ValueError(f"Unknown booking action: {action}")
