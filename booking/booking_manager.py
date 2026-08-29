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

import json
import logging
import os
from dataclasses import dataclass, asdict
from typing import Callable, Dict, List, Optional

from core.schemas import (
    ActionItem,
    ActionStatus,
    BookingStatus,
    PermissionLevel,
    ToolStatus,
)
from tools import ToolRegistry, default_registry

logger = logging.getLogger("booking")


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
    booking_type: str = "scenic"          # scenic / hotel / transport / food
    price: float = 0.0                    # 票价/房费（自动填充）
    tel: str = ""                         # 联系电话（自动填充）
    ticket_required: bool = True          # 是否需要预约
    address: str = ""                     # 地址（自动填充）
    open_hours: str = ""                  # 营业时间（自动填充）
    confirm_code: str = ""                # submit 后生成的确认码


class BookingManager:
    """预约状态机。每个动作同步产出一个 ActionItem（供 C 的 Action Queue 展示/确认）。

    ``on_booking_failed``：可选回调（由 AgentRuntime 注入），预订提交失败时
    在状态置 FAILED / Action 置 BLOCKED 之后调用，用于补发 BOOKING 事件
    闭合「确认预订 → 失败 → 事件 → A 换酒店」闭环（AB 合码方案 §三.7）。
    """

    def __init__(
        self,
        registry: Optional[ToolRegistry] = None,
        on_booking_failed: Optional[Callable[["BookingRecord"], None]] = None,
        persist_path: Optional[str] = None,
        restore: bool = True,
    ) -> None:
        self._registry = registry or default_registry
        self._records: Dict[str, BookingRecord] = {}
        self._actions: List[ActionItem] = []
        self._on_booking_failed = on_booking_failed
        # E5：持久化（生产传 "logs/actions.json"，测试不传保持内存态）。
        # restore=False 用于"新会话清空重建"场景（AgentRuntime.init_from_requirement）：
        # 不加载旧状态，首次落盘时覆写旧文件。
        self._persist_path = persist_path
        if persist_path and restore:
            self._load_persisted()

    # -- E5：持久化 ---------------------------------------------------------
    def _persist(self) -> None:
        """动作/预约状态落盘（单用户 Demo 规模：整体 json 覆写）。"""
        if not self._persist_path:
            return
        snapshot = {
            "records": [asdict(r) | {"status": r.status.value} for r in self._records.values()],
            "actions": [
                asdict(a) | {
                    "status": a.status.value,
                    "permission": a.permission.value,
                }
                for a in self._actions
            ],
        }
        try:
            os.makedirs(os.path.dirname(self._persist_path) or ".", exist_ok=True)
            with open(self._persist_path, "w", encoding="utf-8") as f:
                json.dump(snapshot, f, ensure_ascii=False, indent=1)
        except OSError:
            logger.exception("booking actions persist failed: %s", self._persist_path)

    def _load_persisted(self) -> None:
        """启动时恢复未决动作与预约记录；文件缺失/损坏静默跳过。"""
        if not os.path.exists(self._persist_path):
            return
        try:
            with open(self._persist_path, encoding="utf-8") as f:
                snapshot = json.load(f)
            for d in snapshot.get("records", []):
                rec = BookingRecord(**{**d, "status": BookingStatus(d["status"])})
                self._records[rec.booking_id] = rec
            for d in snapshot.get("actions", []):
                item = ActionItem(**{
                    **d,
                    "status": ActionStatus(d["status"]),
                    "permission": PermissionLevel(d["permission"]),
                })
                self._actions.append(item)
            logger.info("booking state restored: %d records, %d actions",
                        len(self._records), len(self._actions))
        except (OSError, ValueError, TypeError, KeyError):
            logger.exception("booking state restore failed: %s", self._persist_path)

    # -- 查询 --------------------------------------------------------------
    def records(self) -> List[BookingRecord]:
        return list(self._records.values())

    def actions(self) -> List[ActionItem]:
        return list(self._actions)

    def enqueue_actions(self, items: List[ActionItem]) -> None:
        """把外部产出的动作（如重规划后的「更新路线 / 换宿预订」）并入队列。

        按 ``action_id`` 去重（重复入队幂等）；状态一律 PENDING，由 C 端按
        ``permission`` 分级确认 / 直接执行。修复 0827：replan 动作回填入口。
        """
        existing = {a.action_id for a in self._actions}
        for item in items:
            if item.action_id not in existing:
                self._actions.append(item)
                existing.add(item.action_id)
        self._persist()

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
        elif booking_type == "food":
            food_result = self._registry.call("food", near=place)
            if food_result.status == ToolStatus.OK and food_result.data:
                # food Tool 返回餐厅列表，取第一条匹配的
                restaurants = food_result.data
                if isinstance(restaurants, list) and restaurants:
                    rd = restaurants[0]
                    price = float(rd.get("price_per_person", 0.0))
                    tel = str(rd.get("tel", ""))
                    ticket_required = True   # 餐厅默认需要预约
                    address = str(rd.get("address", ""))
                    open_hours = str(rd.get("open_hours", ""))
        elif booking_type == "hotel":
            # E7：hotel 类型自动填充（此前 price/address 全留空）
            filled = self._autofill_hotel(place)
            price = filled.get("price", 0.0)
            address = filled.get("address", "")
        elif booking_type == "transport":
            # E7：transport（车票）自动填充——place 约定 "出发→到达" 格式
            filled = self._autofill_transport(place, target_date)
            price = filled.get("price", 0.0)
            note = (note + "；" if note else "") + filled.get("note", "")

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
        if note:
            record.note = (record.note + "；" + note) if record.note else note
        # ActionItem.title 根据 booking_type 调整动词
        verb = {"scenic": "预约", "hotel": "预订", "transport": "购买", "food": "预订"}.get(booking_type, "预约")
        self._actions.append(ActionItem(
            action_id=f"act-{record.booking_id}",
            title=f"{verb} {place}（{target_date}，{party_size}人）",
            description=data["note"],
            status=ActionStatus.PENDING,
            permission=PermissionLevel.CONFIRM,     # 需用户确认后执行
            target=f"booking:{record.booking_id}",
            type="BOOK_TICKET",
            date=target_date,
            quantity=party_size,
        ))
        self._persist()
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
            self._persist()
            if self._on_booking_failed is not None:
                try:
                    self._on_booking_failed(rec)
                except Exception:  # noqa: BLE001  回调失败不阻断 confirm 的报错返回
                    logger.exception("on_booking_failed handler failed")
            raise RuntimeError(result.error or f"booking submit failed: {booking_id}")
        rec.status = BookingStatus.SUBMITTED
        rec.confirm_code = result.data.get("confirm_code", "")
        rec.note = result.data.get("note", rec.note)
        self._mark_action(booking_id, ActionStatus.EXECUTED)
        self._persist()
        return rec

    def mark_confirmed(self, booking_id: str) -> BookingRecord:
        """服务方确认成功。"""
        rec = self.get(booking_id)
        rec.status = BookingStatus.CONFIRMED
        self._persist()
        return rec

    def mark_failed(self, booking_id: str, reason: str = "") -> BookingRecord:
        rec = self.get(booking_id)
        rec.status = BookingStatus.FAILED
        if reason:
            rec.note = reason
        self._persist()
        return rec

    def cancel(self, booking_id: str) -> BookingRecord:
        rec = self.get(booking_id)
        rec.status = BookingStatus.CANCELLED
        self._persist()
        return rec

    # -- E1/E7：hotel / transport 填充与动作执行 ---------------------------

    def _autofill_hotel(self, place: str) -> Dict[str, Any]:
        """E7：hotel 类型 prepare 自动填充（房价/地址）；查不到留空不阻断。"""
        if "hotel" not in self._registry:
            return {}
        try:
            result = self._registry.call(
                "hotel", action="search", place=place, placeType="酒店", size=5,
            )
        except Exception:  # noqa: BLE001  工具缺失/网络失败 → 留空
            logger.warning("hotel autofill search failed: %s", place, exc_info=True)
            return {}
        if result.status != ToolStatus.OK or not result.data:
            return {}
        hotels = result.data.get("hotels") if isinstance(result.data, dict) else result.data
        if not isinstance(hotels, list):
            return {}
        match = next((h for h in hotels if isinstance(h, dict)
                      and (h.get("name") == place or place in str(h.get("name", "")))), None)
        if match is None:
            match = next((h for h in hotels if isinstance(h, dict)
                          and str(h.get("name", "")) in place), None)
        if match is None:
            return {}
        return {
            "price": float(match.get("price_per_night") or 0.0),
            "address": str(match.get("address") or ""),
        }

    def _autofill_transport(self, place: str, target_date: str) -> Dict[str, Any]:
        """E7：transport（车票）自动填充。place 约定 ``出发→到达``；失败留空不阻断。

        选班次：可预订（status=预订）车次中历时最短者；票价取该车的二等座
        （train_price），查不到则只填车次/时刻进 note。
        """
        if "train_ticket" not in self._registry or "→" not in place:
            return {}
        parts = [p.strip() for p in place.split("→") if p.strip()]
        if len(parts) != 2:
            return {}
        try:
            result = self._registry.call(
                "train_ticket", from_station=parts[0], to_station=parts[1],
                date=target_date, limit=20,
            )
        except Exception:  # noqa: BLE001
            logger.warning("transport autofill query failed: %s", place, exc_info=True)
            return {}
        if result.status != ToolStatus.OK or not result.data:
            return {}
        trains = [t for t in result.data if isinstance(t, dict) and t.get("status") == "预订"]
        if not trains:
            return {}

        def _minutes(train: Dict[str, Any]) -> int:
            try:
                hours, minutes = str(train.get("duration", "")).split(":")
                return int(hours) * 60 + int(minutes)
            except ValueError:
                return 10 ** 9

        best = min(trains, key=_minutes)
        info: Dict[str, Any] = {}
        if best.get("code"):
            info["note"] = (f"车次 {best['code']} {best.get('depart_time', '')}-"
                            f"{best.get('arrive_time', '')}")
        price_result = self._registry.call(
            "train_price", from_station=parts[0], to_station=parts[1],
            date=target_date, train=best.get("code", ""),
        )
        if (price_result.status == ToolStatus.OK and price_result.data):
            for row in price_result.data:
                if row.get("code") == best.get("code"):
                    second = (row.get("prices") or {}).get("second_class")
                    if second:
                        info["price"] = float(second)
                    break
        return info

    def execute_hotel_booking(self, name: str, target_date: str = "",
                              party_size: int = 1) -> BookingRecord:
        """E1：HOTEL_BOOK（``target=hotel:{name}``）动作的执行器。

        此前该类动作无任何执行路径（死信）：approve 后据此创建真实
        BookingRecord（hotel 自动填充走 _autofill_hotel），由调用方
        （approve 端点）把原动作标 EXECUTED 并回填 booking_id。
        """
        return self.prepare(
            place=name, target_date=target_date,
            party_size=party_size, booking_type="hotel",
        )

    def execute_action(self, action: ActionItem) -> Optional[BookingRecord]:
        """E1：按动作 target 执行；仅 ``hotel:`` 前缀有执行器（booking:/payment:
        等保持既有语义，approval 执行化见设计文档 §4）。"""
        if not action.target.startswith("hotel:"):
            return None
        name = action.target.split(":", 1)[1].strip()
        rec = self.execute_hotel_booking(
            name, target_date=action.date, party_size=action.quantity or 1,
        )
        action.status = ActionStatus.EXECUTED
        action.description = (
            (action.description + "；" if action.description else "")
            + f"已生成预约单 {rec.booking_id}（待用户 confirm）"
        )
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
            "food": "餐费",
        }.get(rec.booking_type, "费用")
        item = ActionItem(
            action_id=f"pay-{rec.booking_id}",
            title=f"支付 {rec.place} {pay_label}",
            description="付款必须由用户手动完成，Agent 不代付。",
            status=ActionStatus.PENDING,
            permission=PermissionLevel.MANUAL,
            target=f"payment:{rec.booking_id}",
            type="PAYMENT",
            date=rec.target_date,
            quantity=1,
        )
        self._actions.append(item)
        self._persist()
        return item

    # -- 内部 --------------------------------------------------------------
    def _mark_action(self, booking_id: str, status: ActionStatus) -> None:
        for action in self._actions:
            if action.target == f"booking:{booking_id}":
                action.status = status
