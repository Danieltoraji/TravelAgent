"""动作技能测试：两段式契约、hotel_book/ticket_book 意图组装、commit 拦截（P3）。"""

import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock

from booking.booking_manager import BookingManager
from core.schemas import ActionItem, ActionStatus, BookingStatus, PermissionLevel, ToolStatus
from tools.base_tool import ToolRegistry
from tools.booking_tool import BookingTool
from tools.hotel_book import HotelBookSkill, HotelBookSkillLive
from tools.mock_data import MockWorld
from tools.scenic_tool import ScenicTool
from tools.ticket_book import TicketBookSkill, TicketBookSkillLive
from tools.train import TrainPriceTool, TrainTicketTool, TrainTripSkill, TrainTripSkillLive
from tools.tool_provider import ToolProvider


def _full_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(BookingTool())
    reg.register(ScenicTool(MockWorld()))
    reg.register(TrainTicketTool())
    reg.register(TrainPriceTool())
    reg.register(TrainTripSkill())
    return reg


def _provider():
    from tools import default_registry
    return ToolProvider(default_registry)


def _future_date() -> str:
    return (date.today() + timedelta(days=3)).strftime("%Y-%m-%d")


class TestActionSkillContract(unittest.TestCase):
    def test_commit_blocked_by_default(self) -> None:
        # 两技能的 commit 都被守卫拦截，但拒绝原因按领域透出
        hotel = HotelBookSkill().execute(action="commit")
        self.assertEqual(hotel.status, ToolStatus.ERROR)
        self.assertIn("RollingGo", hotel.error)
        ticket = TicketBookSkill().execute(action="commit")
        self.assertEqual(ticket.status, ToolStatus.ERROR)
        self.assertIn("12306", ticket.error)

    def test_unknown_action_rejected(self) -> None:
        r = HotelBookSkill().execute(action="cancel")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("未知 action", r.error)

    def test_action_metadata_excluded_from_llm(self) -> None:
        from tools import default_registry
        for skill in (HotelBookSkill(), TicketBookSkill()):
            spec = skill.spec()
            self.assertEqual(spec.safety, "action")
            self.assertFalse(spec.readonly)
            self.assertEqual(spec.kind, "skill")
        # readonly=False → 不进只读白名单，LLM 永远不可见
        provider_names = {s.name for s in _provider().list_tools()}
        self.assertNotIn("hotel_book", provider_names)
        self.assertNotIn("ticket_book", provider_names)


class TestHotelBookSkill(unittest.TestCase):
    def test_prepare_known_hotel(self) -> None:
        r = HotelBookSkill().execute(
            action="prepare", city="北京", hotel_name="北京王府井酒店",
            checkin_date="2026-09-05", checkout_date="2026-09-06", guests=2,
        )
        self.assertEqual(r.status, ToolStatus.OK)
        intent = r.data
        self.assertEqual(intent["intent"], "hotel_booking")
        self.assertEqual(intent["hotel_id"], "H001")
        self.assertGreater(intent["price_per_night"], 0)
        self.assertIn("booking_url", intent)
        self.assertIn("MANUAL", intent["payment"])

    def test_prepare_unknown_hotel_errors(self) -> None:
        r = HotelBookSkill().execute(action="prepare", city="北京",
                                     hotel_name="不存在酒店XYZ")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("未找到酒店", r.error)

    def test_prepare_missing_name_errors(self) -> None:
        r = HotelBookSkill().execute(action="prepare", city="北京")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("hotel_name", r.error)

    def test_prepare_idempotent(self) -> None:
        tool = HotelBookSkill()
        a = tool.execute(action="prepare", city="北京", hotel_name="北京王府井酒店")
        b = tool.execute(action="prepare", city="北京", hotel_name="北京王府井酒店")
        self.assertEqual(a.data, b.data)   # 纯计算，天然幂等

    def test_live_version_uses_injected_client(self) -> None:
        client = MagicMock()
        client.call_tool.return_value = {
            "hotelList": [{
                "hotelId": 9001, "hotelName": "王府井半岛酒店",
                "coordinate": {"lat": 39.909, "lng": 116.411},
                "starRating": 5, "price": 1580, "address": "王府井大街",
                "bookingUrl": "https://example.com/book/9001",
            }],
        }
        skill = HotelBookSkillLive(client)
        self.assertEqual(skill.source, "live")
        r = skill.execute(action="prepare", city="北京", hotel_name="王府井")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data["hotel_name"], "王府井半岛酒店")
        self.assertEqual(r.data["price_per_night"], 1580)


class TestTicketBookSkill(unittest.TestCase):
    def test_prepare_assembles_intent(self) -> None:
        skill = TicketBookSkill()
        r = skill.execute(action="prepare", from_station="北京南",
                          to_station="上海虹桥", date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        intent = r.data
        self.assertEqual(intent["intent"], "ticket_booking")
        self.assertEqual(intent["code"], "G39")
        self.assertEqual(intent["price_per_person"], 662.0)
        self.assertIn("12306", intent["channel"])
        self.assertIn("MANUAL", intent["payment"])

    def test_commit_blocked(self) -> None:
        r = TicketBookSkill().execute(action="commit")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("12306", r.error)

    def test_live_version_with_injected_client(self) -> None:
        client = MagicMock()
        client.query_tickets.return_value = []
        client.query_price.return_value = []
        book = TicketBookSkillLive(client)
        self.assertEqual(book.source, "live")
        r = book.execute(action="prepare", from_station="北京南",
                         to_station="上海虹桥", date=_future_date())
        # 空班次 → 查询失败语义（真源正常需有票）
        self.assertEqual(r.status, ToolStatus.ERROR)


class TestTicketExecutor(unittest.TestCase):
    def test_ticket_executor_creates_transport_record(self) -> None:
        bm = BookingManager(registry=_full_registry())
        action = ActionItem(
            action_id="ticket-TEST1", title="购买车票",
            target="ticket:北京南→上海虹桥", type="TICKET_BOOK",
            permission=PermissionLevel.CONFIRM,
            date=_future_date(), quantity=1,
        )
        bm.enqueue_actions([action])
        rec = bm.execute_action(action)
        self.assertEqual(action.status, ActionStatus.EXECUTED)
        self.assertEqual(rec.booking_type, "transport")
        self.assertEqual(rec.price, 662.0)     # E7 transport 自动填充
        self.assertIn("G39", rec.note)


if __name__ == "__main__":
    unittest.main()
