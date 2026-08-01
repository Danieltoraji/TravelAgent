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
                note: str = "") -> BookingRecord:
        """准备预约（填写信息），产出待用户确认的 ActionItem。不付款。"""
        result = self._registry.call(
            "booking", action="prepare", place=place,
            target_date=target_date, party_size=party_size,
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
        )
        self._records[record.booking_id] = record
        self._actions.append(ActionItem(
            action_id=f"act-{record.booking_id}",
            title=f"预约 {place}（{target_date}，{party_size}人）",
            description=data["note"],
            status=ActionStatus.PENDING,
            permission=PermissionLevel.CONFIRM,     # 需用户确认后执行
            target=f"booking:{record.booking_id}",
        ))
        return record

    def confirm(self, booking_id: str) -> BookingRecord:
        """用户确认 → 提交预约（仍不付款，付款需人工）。"""
        rec = self.get(booking_id)
        if rec.status not in (BookingStatus.PENDING_CONFIRM, BookingStatus.SUBMITTED):
            raise ValueError(f"Booking {booking_id} not confirmable: {rec.status.value}")
        rec.status = BookingStatus.SUBMITTED
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
        item = ActionItem(
            action_id=f"pay-{rec.booking_id}",
            title=f"支付 {rec.place} 门票/订金",
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
