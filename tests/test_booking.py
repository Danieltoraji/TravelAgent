"""预约系统测试：BookingTool（submit/prepare/status）+ BookingManager 全状态机。

覆盖：
  - BookingTool: prepare（含新字段）、submit（成功/找不到/幂等）、status
  - BookingManager: prepare 自动填充、confirm 调 submit、submit 失败→FAILED、
    mark_confirmed、mark_failed、cancel、payment_action 按类型、完整流程后 ActionQueue
"""

import unittest

from booking.booking_manager import BookingManager, BookingRecord
from core.schemas import ActionStatus, BookingStatus, PermissionLevel, ToolStatus
from tools import default_registry
from tools.base_tool import ToolRegistry
from tools.booking_tool import BookingTool
from tools.mock_data import MockWorld
from tools.scenic_tool import ScenicTool


def _build_registry() -> ToolRegistry:
    """构造一个干净的 registry（仅 booking + scenic），避免全局状态污染。"""
    reg = ToolRegistry()
    reg.register(BookingTool())
    reg.register(ScenicTool(MockWorld()))
    return reg


# ──────────────────────────────────────────────────────────────────────
# BookingTool 单元测试
# ──────────────────────────────────────────────────────────────────────


class TestBookingTool(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = BookingTool()

    def test_prepare_returns_draft_with_new_fields(self) -> None:
        r = self.tool.execute(action="prepare", place="故宫",
                              target_date="2026-08-01", party_size=2)
        self.assertEqual(r.status, ToolStatus.OK)
        data = r.data
        self.assertIn("booking_id", data)
        self.assertEqual(data["place"], "故宫")
        self.assertEqual(data["booking_type"], "scenic")
        self.assertTrue(data["payment_required"])
        self.assertEqual(data["status"], "draft")
        self.assertEqual(data["confirm_code"], "")
        # 新字段存在
        for key in ("price", "tel", "ticket_required", "address", "open_hours"):
            self.assertIn(key, data)

    def test_prepare_with_booking_type_hotel(self) -> None:
        r = self.tool.execute(action="prepare", place="北京饭店",
                              target_date="2026-08-01", party_size=1,
                              booking_type="hotel")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["booking_type"], "hotel")

    def test_submit_success_generates_confirm_code(self) -> None:
        prep = self.tool.execute(action="prepare", place="故宫",
                                 target_date="2026-08-01", party_size=2)
        bid = prep.data["booking_id"]
        r = self.tool.execute(action="submit", booking_id=bid)
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["status"], "submitted")
        self.assertEqual(r.data["confirm_code"], f"CONF-{bid}")

    def test_submit_not_found_errors(self) -> None:
        r = self.tool.execute(action="submit", booking_id="NOSUCHID")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("not found", r.error)

    def test_submit_missing_booking_id_errors(self) -> None:
        r = self.tool.execute(action="submit")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("booking_id", r.error)

    def test_submit_idempotent(self) -> None:
        """重复 submit 同一 booking_id 不报错，返回相同 confirm_code。"""
        prep = self.tool.execute(action="prepare", place="天坛",
                                 target_date="2026-08-01", party_size=1)
        bid = prep.data["booking_id"]
        r1 = self.tool.execute(action="submit", booking_id=bid)
        r2 = self.tool.execute(action="submit", booking_id=bid)
        self.assertEqual(r1.status, ToolStatus.OK)
        self.assertEqual(r2.status, ToolStatus.OK)
        self.assertEqual(r1.data["confirm_code"], r2.data["confirm_code"])

    def test_status_returns_draft_before_submit(self) -> None:
        prep = self.tool.execute(action="prepare", place="故宫",
                                 target_date="2026-08-01", party_size=2)
        bid = prep.data["booking_id"]
        r = self.tool.execute(action="status", booking_id=bid)
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["status"], "draft")

    def test_status_returns_submitted_after_submit(self) -> None:
        prep = self.tool.execute(action="prepare", place="故宫",
                                 target_date="2026-08-01", party_size=2)
        bid = prep.data["booking_id"]
        self.tool.execute(action="submit", booking_id=bid)
        r = self.tool.execute(action="status", booking_id=bid)
        self.assertEqual(r.data["status"], "submitted")
        self.assertEqual(r.data["confirm_code"], f"CONF-{bid}")

    def test_status_missing_id_errors(self) -> None:
        r = self.tool.execute(action="status")
        self.assertEqual(r.status, ToolStatus.ERROR)

    def test_status_not_found_returns_empty(self) -> None:
        r = self.tool.execute(action="status", booking_id="NOSUCHID")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data, {})

    def test_unknown_action_errors(self) -> None:
        r = self.tool.execute(action="unknown")
        self.assertEqual(r.status, ToolStatus.ERROR)


# ──────────────────────────────────────────────────────────────────────
# BookingManager 状态机测试
# ──────────────────────────────────────────────────────────────────────


class TestBookingManagerPrepare(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _build_registry()
        self.bm = BookingManager(self.registry)

    def test_prepare_creates_record(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.assertIsInstance(rec, BookingRecord)
        self.assertEqual(rec.place, "故宫")
        self.assertEqual(rec.target_date, "2026-08-01")
        self.assertEqual(rec.party_size, 2)
        self.assertEqual(rec.status, BookingStatus.PENDING_CONFIRM)
        self.assertTrue(rec.payment_required)
        self.assertEqual(rec.booking_type, "scenic")

    def test_prepare_auto_fills_scenic_info(self) -> None:
        """prepare 调用 scenic Tool，price/tel/ticket_required 填入 record。"""
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        # MockWorld 中故宫 price=60.0, ticket=True
        self.assertEqual(rec.price, 60.0)
        self.assertTrue(rec.ticket_required)

    def test_prepare_scenic_failure_uses_defaults(self) -> None:
        """scenic 调用失败（未知景点）时用默认值，不阻断 prepare。"""
        rec = self.bm.prepare("不存在的景点", target_date="2026-08-01", party_size=1)
        self.assertEqual(rec.price, 0.0)
        self.assertTrue(rec.ticket_required)  # 默认 True
        self.assertEqual(rec.status, BookingStatus.PENDING_CONFIRM)

    def test_prepare_hotel_type_no_scenic_call(self) -> None:
        """booking_type=hotel 时不调用 scenic Tool。"""
        rec = self.bm.prepare("北京饭店", target_date="2026-08-01",
                             party_size=1, booking_type="hotel")
        self.assertEqual(rec.booking_type, "hotel")
        self.assertEqual(rec.price, 0.0)  # 未填充

    def test_prepare_food_type_auto_fills(self) -> None:
        """booking_type=food 时调用 food Tool 自动填充人均消费/电话/地址。"""
        # 需要注册 food Tool
        from tools.food_tool import FoodTool
        from tools.mock_data import MockWorld
        self.registry.register(FoodTool())
        rec = self.bm.prepare("全聚德(前门店)", target_date="2026-08-01",
                             party_size=2, booking_type="food")
        self.assertEqual(rec.booking_type, "food")
        self.assertTrue(rec.ticket_required)   # 餐厅默认需要预约
        # MockWorld 中全聚德 price_per_person=180
        self.assertEqual(rec.price, 180.0)
        self.assertEqual(rec.tel, "010-65112418")
        # ActionItem title 应使用"预订"动词
        act = self.bm.actions()[-1]
        self.assertTrue(act.title.startswith("预订"))

    def test_prepare_creates_confirm_action_item(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        actions = self.bm.actions()
        self.assertEqual(len(actions), 1)
        act = actions[0]
        self.assertEqual(act.permission, PermissionLevel.CONFIRM)
        self.assertEqual(act.status, ActionStatus.PENDING)
        self.assertEqual(act.target, f"booking:{rec.booking_id}")
        self.assertIn("故宫", act.title)

    def test_prepare_action_title_by_type(self) -> None:
        """不同 booking_type 的 ActionItem.title 动词不同。"""
        rec_s = self.bm.prepare("故宫", target_date="2026-08-01", party_size=1)
        self.assertTrue(self.bm.actions()[-1].title.startswith("预约"))

        rec_h = self.bm.prepare("北京饭店", target_date="2026-08-01",
                                party_size=1, booking_type="hotel")
        self.assertTrue(self.bm.actions()[-1].title.startswith("预订"))

        rec_t = self.bm.prepare("G123", target_date="2026-08-01",
                                party_size=1, booking_type="transport")
        self.assertTrue(self.bm.actions()[-1].title.startswith("购买"))


class TestBookingManagerConfirm(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _build_registry()
        self.bm = BookingManager(self.registry)

    def test_confirm_calls_submit(self) -> None:
        """confirm 后状态 SUBMITTED，confirm_code 非空。"""
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        rec = self.bm.confirm(rec.booking_id)
        self.assertEqual(rec.status, BookingStatus.SUBMITTED)
        self.assertEqual(rec.confirm_code, f"CONF-{rec.booking_id}")
        self.assertNotEqual(rec.confirm_code, "")

    def test_confirm_marks_action_executed(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.bm.confirm(rec.booking_id)
        act = self.bm.actions()[0]
        self.assertEqual(act.status, ActionStatus.EXECUTED)

    def test_confirm_wrong_status_raises(self) -> None:
        """对 CONFIRMED 状态调 confirm 抛 ValueError。"""
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.bm.confirm(rec.booking_id)
        self.bm.mark_confirmed(rec.booking_id)
        with self.assertRaises(ValueError):
            self.bm.confirm(rec.booking_id)

    def test_confirm_cancelled_raises(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.bm.cancel(rec.booking_id)
        with self.assertRaises(ValueError):
            self.bm.confirm(rec.booking_id)

    def test_confirm_unknown_booking_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.bm.confirm("NOSUCHID")


class TestBookingManagerLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _build_registry()
        self.bm = BookingManager(self.registry)

    def test_mark_confirmed(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.bm.confirm(rec.booking_id)
        rec = self.bm.mark_confirmed(rec.booking_id)
        self.assertEqual(rec.status, BookingStatus.CONFIRMED)

    def test_mark_failed(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        rec = self.bm.mark_failed(rec.booking_id, reason="名额已满")
        self.assertEqual(rec.status, BookingStatus.FAILED)
        self.assertEqual(rec.note, "名额已满")

    def test_cancel(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        rec = self.bm.cancel(rec.booking_id)
        self.assertEqual(rec.status, BookingStatus.CANCELLED)

    def test_get_unknown_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.bm.get("NOSUCHID")

    def test_records_returns_all(self) -> None:
        self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.bm.prepare("天坛", target_date="2026-08-02", party_size=1)
        self.assertEqual(len(self.bm.records()), 2)


class TestBookingManagerPayment(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = _build_registry()
        self.bm = BookingManager(self.registry)

    def test_payment_action_manual_permission(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        pay = self.bm.payment_action(rec.booking_id)
        self.assertEqual(pay.permission, PermissionLevel.MANUAL)
        self.assertEqual(pay.status, ActionStatus.PENDING)
        self.assertEqual(pay.target, f"payment:{rec.booking_id}")

    def test_payment_action_title_scenic(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        pay = self.bm.payment_action(rec.booking_id)
        self.assertIn("门票", pay.title)

    def test_payment_action_title_hotel(self) -> None:
        rec = self.bm.prepare("北京饭店", target_date="2026-08-01",
                              party_size=1, booking_type="hotel")
        pay = self.bm.payment_action(rec.booking_id)
        self.assertIn("房费", pay.title)

    def test_payment_action_title_transport(self) -> None:
        rec = self.bm.prepare("G123", target_date="2026-08-01",
                              party_size=1, booking_type="transport")
        pay = self.bm.payment_action(rec.booking_id)
        self.assertIn("车票", pay.title)

    def test_payment_action_unknown_booking_raises(self) -> None:
        with self.assertRaises(KeyError):
            self.bm.payment_action("NOSUCHID")


class TestBookingManagerFullFlow(unittest.TestCase):
    """完整流程：prepare → confirm → mark_confirmed → payment_action。"""

    def setUp(self) -> None:
        self.registry = _build_registry()
        self.bm = BookingManager(self.registry)

    def test_full_flow_action_queue(self) -> None:
        """完整流程后 actions() 包含预约 + 付款两个 ActionItem。"""
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.bm.confirm(rec.booking_id)
        self.bm.mark_confirmed(rec.booking_id)
        self.bm.payment_action(rec.booking_id)

        actions = self.bm.actions()
        self.assertEqual(len(actions), 2)

        # 预约 action: EXECUTED + CONFIRM
        booking_act = actions[0]
        self.assertEqual(booking_act.status, ActionStatus.EXECUTED)
        self.assertEqual(booking_act.permission, PermissionLevel.CONFIRM)

        # 付款 action: PENDING + MANUAL
        pay_act = actions[1]
        self.assertEqual(pay_act.status, ActionStatus.PENDING)
        self.assertEqual(pay_act.permission, PermissionLevel.MANUAL)

    def test_full_flow_record_status(self) -> None:
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.assertEqual(rec.status, BookingStatus.PENDING_CONFIRM)

        rec = self.bm.confirm(rec.booking_id)
        self.assertEqual(rec.status, BookingStatus.SUBMITTED)
        self.assertNotEqual(rec.confirm_code, "")

        rec = self.bm.mark_confirmed(rec.booking_id)
        self.assertEqual(rec.status, BookingStatus.CONFIRMED)

    def test_full_flow_with_scenic_autofill(self) -> None:
        """完整流程中 scenic 自动填充的票价在 record 中可查。"""
        rec = self.bm.prepare("故宫", target_date="2026-08-01", party_size=2)
        self.assertEqual(rec.price, 60.0)  # MockWorld 故宫票价
        self.bm.confirm(rec.booking_id)
        self.bm.mark_confirmed(rec.booking_id)
        # 确认后 record 仍保留 scenic 信息
        rec = self.bm.get(rec.booking_id)
        self.assertEqual(rec.price, 60.0)


class TestBookingPersistence(unittest.TestCase):
    """E5：persist_path 开启时动作/预约落盘并可恢复；不传保持纯内存。"""

    def test_persist_and_restore(self) -> None:
        import json
        import os
        import tempfile

        from booking.booking_manager import BookingManager
        from core.schemas import ActionStatus, PermissionLevel

        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "actions.json")
            bm = BookingManager(persist_path=path)
            rec = bm.prepare("故宫", target_date="2026-08-01", party_size=2)
            self.assertTrue(os.path.exists(path))

            # 新实例（同一文件）恢复未决动作与预约记录
            bm2 = BookingManager(persist_path=path)
            self.assertIn(rec.booking_id, bm2._records)
            actions = bm2.actions()
            self.assertEqual(len(actions), 1)
            self.assertEqual(actions[0].status, ActionStatus.PENDING)
            self.assertEqual(actions[0].permission, PermissionLevel.CONFIRM)

            # 文件结构合法（json 且含 records/actions 两组）
            with open(path, encoding="utf-8") as f:
                snapshot = json.load(f)
            self.assertIn("records", snapshot)
            self.assertIn("actions", snapshot)

    def test_no_persist_path_keeps_memory_only(self) -> None:
        from booking.booking_manager import BookingManager

        bm = BookingManager()  # 不传 persist_path → 纯内存（默认分支）
        bm.prepare("故宫", target_date="2026-08-01", party_size=1)
        self.assertEqual(len(bm.actions()), 1)


if __name__ == "__main__":
    unittest.main()
