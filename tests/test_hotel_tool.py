"""hotel_tool 测试：Mock 返回、Live 参数构造、结果标准化、ToolProvider 白名单。"""

import unittest

from tools.base_tool import ToolRegistry
from tools.hotel_tool import HotelTool, HotelToolLive
from tools.tool_provider import ToolProvider


class TestHotelToolMock(unittest.TestCase):
    def test_search_returns_hotels(self) -> None:
        tool = HotelTool()
        result = tool._run(action="search", place="北京")
        self.assertIn("hotels", result)
        self.assertGreater(result["count"], 0)
        hotel = result["hotels"][0]
        self.assertIn("id", hotel)
        self.assertIn("name", hotel)
        self.assertIn("location", hotel)
        self.assertIn("price_per_night", hotel)

    def test_detail_returns_rooms(self) -> None:
        tool = HotelTool()
        result = tool._run(action="detail", hotelId="H001")
        self.assertIn("roomRatePlans", result)
        self.assertTrue(result["roomRatePlans"])

    def test_tags_returns_tags(self) -> None:
        tool = HotelTool()
        result = tool._run(action="tags")
        self.assertIn("tags", result)


class _FakeRollingGoClient:
    def __init__(self, result=None):
        self.result = result if result is not None else {}
        self.calls = []

    def call_tool(self, name: str, arguments=None):
        self.calls.append((name, arguments or {}))
        return self.result


class TestHotelToolLive(unittest.TestCase):
    def test_search_forwards_correct_mcp_arguments(self) -> None:
        client = _FakeRollingGoClient({"hotels": []})
        tool = HotelToolLive(client)
        tool._run(
            action="search",
            place="北京",
            placeType="城市",
            checkInDate="2026-08-23",
            stayNights=2,
            adultCount=2,
            starRatings=[4.0, 5.0],
            maxPricePerNight=1000,
            size=5,
        )
        self.assertEqual(client.calls[0][0], "searchHotels")
        args = client.calls[0][1]
        self.assertEqual(args["place"], "北京")
        self.assertEqual(args["placeType"], "城市")
        self.assertEqual(args["checkInParam"]["checkInDate"], "2026-08-23")
        self.assertEqual(args["checkInParam"]["stayNights"], 2)
        self.assertEqual(args["checkInParam"]["adultCount"], 2)
        self.assertEqual(args["filterOptions"]["starRatings"], [4.0, 5.0])
        self.assertEqual(args["hotelTags"]["maxPricePerNight"], 1000)
        self.assertEqual(args["size"], 5)

    def test_search_forwards_extended_filters(self) -> None:
        client = _FakeRollingGoClient({"hotels": []})
        tool = HotelToolLive(client)
        tool._run(
            action="search",
            place="北京",
            distanceInMeter=2000,
            preferredBrands=["华美达"],
            requiredTags=["免费取消"],
            maxPricePerNight=800,
        )
        args = client.calls[0][1]
        self.assertEqual(args["filterOptions"]["distanceInMeter"], 2000)
        self.assertEqual(args["hotelTags"]["preferredBrands"], ["华美达"])
        self.assertEqual(args["hotelTags"]["requiredTags"], ["免费取消"])
        self.assertEqual(args["hotelTags"]["maxPricePerNight"], 800)

    def test_search_accepts_city_alias(self) -> None:
        client = _FakeRollingGoClient({"hotels": []})
        tool = HotelToolLive(client)
        tool._run(action="search", city="北京")
        self.assertEqual(client.calls[0][0], "searchHotels")
        args = client.calls[0][1]
        self.assertEqual(args["place"], "北京")
        self.assertEqual(args["placeType"], "城市")

    def test_search_normalizes_hotel_fields(self) -> None:
        client = _FakeRollingGoClient({
            "hotels": [
                {
                    "hotelId": 1001,
                    "hotelName": "测试酒店",
                    "minPrice": 680,
                    "starRating": 5,
                    "rating": 4.9,
                    "address": "测试地址",
                    "tags": ["市中心"],
                    "location": {"lat": 39.9, "lng": 116.4},
                }
            ]
        })
        tool = HotelToolLive(client)
        result = tool._run(action="search", place="北京")
        self.assertEqual(result["count"], 1)
        hotel = result["hotels"][0]
        self.assertEqual(hotel["id"], 1001)
        self.assertEqual(hotel["name"], "测试酒店")
        self.assertEqual(hotel["price_per_night"], 680)
        self.assertEqual(hotel["star"], 5)
        self.assertEqual(hotel["location"]["lat"], 39.9)

    def test_search_normalizes_rollinggo_hotel_information_list(self) -> None:
        client = _FakeRollingGoClient({
            "success": True,
            "hotelInformationList": [
                {
                    "hotelId": 43586,
                    "name": "测试酒店",
                    "nameEn": "Test Hotel",
                    "brand": "TestBrand",
                    "address": "测试地址",
                    "latitude": 40.06,
                    "longitude": 116.58,
                    "starRating": 4.0,
                    "price": {"lowestPrice": 443.0, "currency": "CNY"},
                    "tags": ["市中心"],
                    "bookingUrl": "https://example.com/booking",
                    "imageUrl": "https://example.com/img.jpg",
                }
            ]
        })
        tool = HotelToolLive(client)
        result = tool._run(action="search", place="北京")
        self.assertEqual(result["count"], 1)
        hotel = result["hotels"][0]
        self.assertEqual(hotel["id"], 43586)
        self.assertEqual(hotel["name"], "测试酒店")
        self.assertEqual(hotel["price_per_night"], 443.0)
        self.assertEqual(hotel["star"], 4.0)
        self.assertEqual(hotel["location"]["lat"], 40.06)
        self.assertEqual(hotel["location"]["lng"], 116.58)
        self.assertEqual(hotel["booking_url"], "https://example.com/booking")

    def test_detail_forwards_hotel_id_and_dates(self) -> None:
        client = _FakeRollingGoClient({"rooms": []})
        tool = HotelToolLive(client)
        tool._run(
            action="detail",
            hotelId=123,
            checkInDate="2026-08-23",
            checkOutDate="2026-08-25",
            adultCount=2,
            roomCount=1,
        )
        self.assertEqual(client.calls[0][0], "getHotelDetail")
        args = client.calls[0][1]
        self.assertEqual(args["hotelId"], 123)
        self.assertEqual(args["dateParam"]["checkInDate"], "2026-08-23")
        self.assertEqual(args["dateParam"]["checkOutDate"], "2026-08-25")
        self.assertEqual(args["occupancyParam"]["adultCount"], 2)
        self.assertEqual(args["occupancyParam"]["roomCount"], 1)

    def test_detail_forwards_filter(self) -> None:
        client = _FakeRollingGoClient({"roomRatePlans": []})
        tool = HotelToolLive(client)
        tool._run(
            action="detail",
            hotelId=123,
            cancelPolicy="CANCELABLE",
            mealType="WITH_BREAKFAST",
        )
        args = client.calls[0][1]
        self.assertEqual(args["filter"]["cancelPolicy"], "CANCELABLE")
        self.assertEqual(args["filter"]["mealType"], "WITH_BREAKFAST")

    def test_detail_normalizes_room_rate_plans(self) -> None:
        client = _FakeRollingGoClient({
            "hotelId": 43586,
            "name": "测试酒店",
            "roomRatePlans": [
                {
                    "roomName": "高级间-双床",
                    "ratePlanId": "RP1",
                    "ratePlanName": "高级间-双床",
                    "averagePrice": 443,
                    "currency": "CNY",
                    "mealAmount": 0,
                    "mealTypeStr": "不含早餐",
                    "isOnRequest": False,
                    "cancelPolicy": "免费取消",
                    "cancelable": True,
                    "roomInfo": {
                        "hasWifi": True,
                        "hasWindow": True,
                        "maxOccupancy": 2,
                        "size": "31-35",
                        "floor": "1-4",
                        "smoking": "不可吸烟",
                        "images": "img.jpg",
                    },
                }
            ],
        })
        tool = HotelToolLive(client)
        result = tool._run(action="detail", hotelId=43586)
        self.assertEqual(result["hotelId"], 43586)
        self.assertEqual(len(result["rooms"]), 1)
        room = result["rooms"][0]
        self.assertEqual(room["room_name"], "高级间-双床")
        self.assertEqual(room["average_price"], 443)
        self.assertEqual(room["cancelable"], True)
        self.assertEqual(room["room_info"]["has_wifi"], True)

    def test_detail_requires_hotel_id_or_name(self) -> None:
        client = _FakeRollingGoClient({})
        tool = HotelToolLive(client)
        with self.assertRaises(ValueError):
            tool._run(action="detail")

    def test_tags_calls_getHotelSearchTags(self) -> None:
        client = _FakeRollingGoClient({"tags": []})
        tool = HotelToolLive(client)
        tool._run(action="tags")
        self.assertEqual(client.calls[0][0], "getHotelSearchTags")

    def test_tags_cached_within_ttl(self) -> None:
        client = _FakeRollingGoClient({"tags": ["市中心"]})
        tool = HotelToolLive(client)
        tool._run(action="tags")
        tool._run(action="tags")
        self.assertEqual(len(client.calls), 1)


class TestHotelToolProvider(unittest.TestCase):
    def test_hotel_is_in_tool_provider_whitelist(self) -> None:
        registry = ToolRegistry()
        registry.register(HotelTool())
        provider = ToolProvider(registry)
        names = [spec.name for spec in provider.list_tools()]
        self.assertIn("hotel", names)

    def test_hotel_call_through_provider(self) -> None:
        registry = ToolRegistry()
        registry.register(HotelTool())
        provider = ToolProvider(registry)
        result = provider.call_json("hotel", {"action": "search", "place": "北京"})
        self.assertEqual(result["tool"], "hotel")
        self.assertEqual(result["status"], "ok")
        self.assertIn("hotels", result["data"])


if __name__ == "__main__":
    unittest.main()
