"""预约管理器：预约状态机 + 对接 Action Queue 契约（C 负责展示/确认）。

对应《任务整理.md》：
  - Booking Agent 只准备预约，不付款；
  - 提交预约属于"需用户确认"（Permission.CONFIRM）；
  - 付款属于"提醒用户自己执行"（Permission.MANUAL），绝不自动执行。

状态机：PENDING_CONFIRM →(用户确认) SUBMITTED →(服务方确认) CONFIRMED
                                        ↘ (失败) FAILED
                     任一状态可 → CANCELLED
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional

from core.schemas import (
    ActionItem,
    ActionStatus,
    BookingStatus,
    PermissionLevel,
    ToolStatus,
)
from tools import ToolRegistry, default_registry


@dataclass
class BookingRecord:
    """一条预约记录。"""
    booking_id: str
    place: str
    target_date: str
    party_size: int
    status: BookingStatus
    payment_required: bool
    note: str = ""
    booking_type: str = "scenic"          # scenic / hotel / transport
    price: float = 0.0                    # 票价/房费（自动填充）
    tel: str = ""                         # 联系电话（自动填充）
    ticket_required: bool = True          # 是否需要预约
    address: str = ""                     # 地址（自动填充）
    open_hours: str = ""                  # 营业时间（自动填充）
    confirm_code: str = ""                # submit 后生成的确认码


class BookingManager:
    """预约状态机。每个动作同步产出一个 ActionItem（供 C 的 Action Queue 展示/确认）。"""

    def __init__(self, registry: Optional[ToolRegistry] = None) -> None:
        self._registry = registry or default_registry
        self._records: Dict[str, BookingRecord] = {}
        self._actions: List[ActionItem] = []

    # -- 查询 --------------------------------------------------------------
    def records(self) -> List[BookingRecord]:
        return list(self._records.values())

    def actions(self) -> List[ActionItem]:
        return list(self._actions)

    def get(self, booking_id: str) -> BookingRecord:
        if booking_id not in self._records:
            raise KeyError(f"Unknown booking: {booking_id}")
        return self._records[booking_id]

    # -- 状态流转 ----------------------------------------------------------
    def prepare(self, place: str, target_date: str, party_size: int = 1,
                note: str = "", booking_type: str = "scenic") -> BookingRecord:
        """准备预约（填写信息），产出待用户确认的 ActionItem。不付款。

        当 booking_type == "scenic" 时，自动调用 scenic Tool 获取景点真实信息
        （票价、电话、是否需预约、地址、营业时间）并填入预约草稿。
        """
        # 自动填充景点信息
        price = 0.0
        tel = ""
        ticket_required = True
        address = ""
        open_hours = ""

        if booking_type == "scenic":
            scenic_result = self._registry.call("scenic", place=place)
            if scenic_result.status == ToolStatus.OK and scenic_result.data:
                sd = scenic_result.data
                price = float(sd.get("price", 0.0))
                tel = str(sd.get("tel", ""))
                ticket_required = bool(sd.get("ticket_required", True))
                address = str(sd.get("address", ""))
                open_hours = str(sd.get("open_hours", ""))

        result = self._registry.call(
            "booking", action="prepare", place=place,
            target_date=target_date, party_size=party_size,
            booking_type=booking_type,
            price=price, tel=tel,
            ticket_required=ticket_required,
            address=address, open_hours=open_hours,
        )
        if result.status != ToolStatus.OK:
            raise RuntimeError(result.error or "booking prepare failed")
        data = result.data
        record = BookingRecord(
            booking_id=data["booking_id"],
            place=place,
            target_date=target_date,
            party_size=party_size,
            status=BookingStatus.PENDING_CONFIRM,
            payment_required=bool(data["payment_required"]),
            note=data["note"],
            booking_type=booking_type,
            price=price,
            tel=tel,
            ticket_required=ticket_required,
            address=address,
            open_hours=open_hours,
        )
        self._records[record.booking_id] = record
        # ActionItem.title 根据 booking_type 调整动词
        verb = {"scenic": "预约", "hotel": "预订", "transport": "购买"}.get(booking_type, "预约")
        self._actions.append(ActionItem(
            action_id=f"act-{record.booking_id}",
            title=f"{verb} {place}（{target_date}，{party_size}人）",
            description=data["note"],
            status=ActionStatus.PENDING,
            permission=PermissionLevel.CONFIRM,     # 需用户确认后执行
            target=f"booking:{record.booking_id}",
        ))
        return record

    def confirm(self, booking_id: str) -> BookingRecord:
        """用户确认 → 提交预约（仍不付款，付款需人工）。

        调用 BookingTool.submit 模拟提交，成功后读取 confirm_code。
        """
        rec = self.get(booking_id)
        if rec.status not in (BookingStatus.PENDING_CONFIRM, BookingStatus.SUBMITTED):
            raise ValueError(f"Booking {booking_id} not confirmable: {rec.status.value}")
        # 调用 submit 模拟提交
        result = self._registry.call("booking", action="submit", booking_id=booking_id)
        if result.status != ToolStatus.OK:
            rec.status = BookingStatus.FAILED
            rec.note = result.error or "submit failed"
            self._mark_action(booking_id, ActionStatus.BLOCKED)
            raise RuntimeError(result.error or f"booking submit failed: {booking_id}")
        rec.status = BookingStatus.SUBMITTED
        rec.confirm_code = result.data.get("confirm_code", "")
        rec.note = result.data.get("note", rec.note)
        self._mark_action(booking_id, ActionStatus.EXECUTED)
        return rec

    def mark_confirmed(self, booking_id: str) -> BookingRecord:
        """服务方确认成功。"""
        rec = self.get(booking_id)
        rec.status = BookingStatus.CONFIRMED
        return rec

    def mark_failed(self, booking_id: str, reason: str = "") -> BookingRecord:
        rec = self.get(booking_id)
        rec.status = BookingStatus.FAILED
        if reason:
            rec.note = reason
        return rec

    def cancel(self, booking_id: str) -> BookingRecord:
        rec = self.get(booking_id)
        rec.status = BookingStatus.CANCELLED
        return rec

    # -- 付款提醒（人工执行） ----------------------------------------------
    def payment_action(self, booking_id: str) -> ActionItem:
        """付款属于高风险操作：只生成提醒，必须由用户手动完成。"""
        rec = self.get(booking_id)
        # title 根据 booking_type 调整
        pay_label = {
            "scenic": "门票",
            "hotel": "房费/订金",
            "transport": "车票",
        }.get(rec.booking_type, "费用")
        item = ActionItem(
            action_id=f"pay-{rec.booking_id}",
            title=f"支付 {rec.place} {pay_label}",
            description="付款必须由用户手动完成，Agent 不代付。",
            status=ActionStatus.PENDING,
            permission=PermissionLevel.MANUAL,
            target=f"payment:{rec.booking_id}",
        )
        self._actions.append(item)
        return item

    # -- 内部 --------------------------------------------------------------
    def _mark_action(self, booking_id: str, status: ActionStatus) -> None:
        for action in self._actions:
            if action.target == f"booking:{booking_id}":
                action.status = status
