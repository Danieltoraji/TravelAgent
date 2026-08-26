"""酒店 Tool：酒店信息查询（搜索 / 房型价格明细 / 搜索标签）。

Mock 版（HotelTool）：返回固定酒店列表，Demo 用。
Live 版（HotelToolLive）：通过 RollingGo MCP 查询真实酒店数据。

切换方式：build_registry() 按 settings.use_real_hotel_api 自动选择。
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from tools.base_tool import BaseTool

_MOCK_HOTELS = [
    {
        "id": "H001",
        "name": "北京王府井酒店",
        "name_en": "Beijing Wangfujing Hotel",
        "brand": "示例品牌",
        "location": {"lat": 39.914, "lng": 116.410},
        "star": 5,
        "rating": 4.8,
        "price_per_night": 880,
        "address": "北京市东城区王府井大街",
        "tags": ["市中心", "近地铁"],
        "booking_url": "https://example.com/booking/H001",
        "image_url": "https://example.com/img/H001.jpg",
        "open": True,
    },
    {
        "id": "H002",
        "name": "北京前门大酒店",
        "name_en": "Beijing Qianmen Hotel",
        "brand": "示例品牌",
        "location": {"lat": 39.899, "lng": 116.397},
        "star": 4,
        "rating": 4.5,
        "price_per_night": 520,
        "address": "北京市东城区前门大街",
        "tags": ["近景点", "交通便利"],
        "booking_url": "https://example.com/booking/H002",
        "image_url": "https://example.com/img/H002.jpg",
        "open": True,
    },
]

_DEFAULT_TAGS_TTL = 3600  # tags 缓存 1 小时


class HotelTool(BaseTool):
    """酒店查询工具（Mock）。"""

    name = "hotel"
    description = "酒店信息查询：按城市/日期/人数/星级/价格搜索酒店，或查询单个酒店房型与价格明细。"
    source = "mock"
    readonly = True
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["search", "detail", "tags"],
                "description": "search 搜索酒店；detail 查询单个酒店房型价格；tags 获取搜索标签",
            },
            "place": {"type": "string", "description": "城市/机场/景点/火车站/地铁站/酒店/区县/详细地址"},
            "city": {"type": "string", "description": "城市名（兼容 A 侧现有调用，等价于 place 为城市）"},
            "placeType": {
                "type": "string",
                "enum": ["城市", "机场", "景点", "火车站", "地铁站", "酒店", "区/县", "详细地址"],
                "description": "place 的类型",
            },
            "originQuery": {"type": "string", "description": "综合住宿意图描述"},
            "checkInDate": {"type": "string", "description": "入住日期 YYYY-MM-DD"},
            "checkOutDate": {"type": "string", "description": "离店日期 YYYY-MM-DD"},
            "stayNights": {"type": "integer", "description": "入住晚数"},
            "adultCount": {"type": "integer", "description": "每间房成人数"},
            "roomCount": {"type": "integer", "description": "房间数"},
            "childCount": {"type": "integer", "description": "每间房儿童数"},
            "childAgeDetails": {"type": "array", "items": {"type": "integer"}, "description": "儿童年龄数组"},
            "starRatings": {"type": "array", "items": {"type": "number"}, "description": "星级范围，如 [4.5, 5.0]"},
            "maxPricePerNight": {"type": "number", "description": "每晚价格上限"},
            "distanceInMeter": {"type": "integer", "description": "按 POI 位置的直线距离过滤（米）"},
            "preferredBrands": {"type": "array", "items": {"type": "string"}, "description": "偏好品牌列表"},
            "requiredTags": {"type": "array", "items": {"type": "string"}, "description": "必须命中的酒店标签（硬约束）"},
            "cancelPolicy": {
                "type": "string",
                "enum": ["CANCELABLE", "NON_CANCELABLE"],
                "description": "取消政策筛选（detail 时使用）",
            },
            "mealType": {
                "type": "string",
                "enum": ["WITH_BREAKFAST", "SINGLE_BREAKFAST", "DOUBLE_BREAKFAST", "NO_MEAL"],
                "description": "餐食类别筛选（detail 时使用）",
            },
            "hotelId": {"type": "integer", "description": "酒店 ID（detail 时使用）"},
            "name": {"type": "string", "description": "酒店名称（detail 时使用）"},
            "size": {"type": "integer", "description": "返回数量，默认 5，最大 20"},
        },
        "required": ["action"],
    }

    def _run(self, action: str = "search", **kwargs: Any) -> Any:
        if action == "tags":
            return {
                "tags": ["市中心", "近地铁", "近景点", "含早餐", "免费取消", "机场酒店", "商务型"],
            }
        if action == "detail":
            hotel_id = kwargs.get("hotelId")
            name = kwargs.get("name", "")
            for hotel in _MOCK_HOTELS:
                if (hotel_id is not None and str(hotel["id"]) == str(hotel_id)) or (
                    name and name in hotel["name"]
                ):
                    return {
                        "hotelId": hotel["id"],
                        "name": hotel["name"],
                        "starRating": hotel["star"],
                        "checkIn": kwargs.get("checkInDate", ""),
                        "checkOut": kwargs.get("checkOutDate", ""),
                        "bookingUrl": hotel["booking_url"],
                        "roomRatePlans": [
                            {
                                "roomName": "标准间",
                                "ratePlanId": "R001",
                                "ratePlanName": "标准间",
                                "averagePrice": hotel["price_per_night"],
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
                                    "size": "25-30",
                                    "floor": "1-5",
                                    "smoking": "不可吸烟",
                                    "images": "",
                                },
                            }
                        ],
                    }
            return {"hotelId": hotel_id, "name": name, "roomRatePlans": []}
        # search
        return {"hotels": _MOCK_HOTELS, "count": len(_MOCK_HOTELS)}


class HotelToolLive(HotelTool):
    """RollingGo MCP 实现版。"""

    source = "live"

    def __init__(self, client: Any, tags_ttl: int = _DEFAULT_TAGS_TTL) -> None:
        super().__init__()
        self._client = client
        self._tags_cache: Optional[Dict[str, Any]] = None
        self._tags_cache_at: float = 0.0
        self._tags_ttl = tags_ttl

    def _run(self, action: str = "search", **kwargs: Any) -> Any:
        if action == "tags":
            return self._get_tags()
        if action == "detail":
            return self._call_detail(kwargs)
        return self._call_search(kwargs)

    # -- tags 缓存 ---------------------------------------------------------

    def _get_tags(self) -> Dict[str, Any]:
        now = time.time()
        if self._tags_cache is not None and now - self._tags_cache_at < self._tags_ttl:
            return self._tags_cache
        result = self._client.call_tool("getHotelSearchTags", {})
        self._tags_cache = result if isinstance(result, dict) else {"data": result}
        self._tags_cache_at = now
        return self._tags_cache

    # -- RollingGo 参数构造 ------------------------------------------------

    def _call_search(self, kwargs: Dict[str, Any]) -> Any:
        # 兼容 A 侧现有调用：make_live_hotel_provider 用 city=city 调 hotel_tool
        place = kwargs.get("place") or kwargs.get("city") or ""
        if not place:
            raise ValueError("hotel search requires 'place' or 'city'")
        place_type = kwargs.get("placeType", "城市")
        origin_query = kwargs.get("originQuery") or f"在{place}住宿"

        arguments: Dict[str, Any] = {
            "originQuery": origin_query,
            "place": place,
            "placeType": place_type,
        }

        check_in_param: Dict[str, Any] = {}
        if kwargs.get("checkInDate"):
            check_in_param["checkInDate"] = kwargs["checkInDate"]
        if kwargs.get("stayNights") is not None:
            check_in_param["stayNights"] = int(kwargs["stayNights"])
        if kwargs.get("adultCount") is not None:
            check_in_param["adultCount"] = int(kwargs["adultCount"])
        if check_in_param:
            arguments["checkInParam"] = check_in_param

        filter_options: Dict[str, Any] = {}
        if kwargs.get("starRatings"):
            filter_options["starRatings"] = list(kwargs["starRatings"])
        if kwargs.get("distanceInMeter") is not None:
            filter_options["distanceInMeter"] = int(kwargs["distanceInMeter"])
        if filter_options:
            arguments["filterOptions"] = filter_options

        hotel_tags: Dict[str, Any] = {}
        if kwargs.get("maxPricePerNight") is not None:
            hotel_tags["maxPricePerNight"] = float(kwargs["maxPricePerNight"])
        if kwargs.get("preferredBrands"):
            hotel_tags["preferredBrands"] = list(kwargs["preferredBrands"])
        if kwargs.get("requiredTags"):
            hotel_tags["requiredTags"] = list(kwargs["requiredTags"])
        if hotel_tags:
            arguments["hotelTags"] = hotel_tags

        if kwargs.get("size") is not None:
            arguments["size"] = int(kwargs["size"])

        result = self._client.call_tool("searchHotels", arguments)
        return self._normalize_search_result(result)

    def _call_detail(self, kwargs: Dict[str, Any]) -> Any:
        arguments: Dict[str, Any] = {}
        if kwargs.get("hotelId") is not None:
            arguments["hotelId"] = int(kwargs["hotelId"])
        if kwargs.get("name"):
            arguments["name"] = str(kwargs["name"])

        if not kwargs.get("hotelId") and not kwargs.get("name"):
            raise ValueError("hotel detail requires 'hotelId' or 'name'")

        if kwargs.get("checkInDate") or kwargs.get("checkOutDate"):
            date_param: Dict[str, Any] = {}
            if kwargs.get("checkInDate"):
                date_param["checkInDate"] = str(kwargs["checkInDate"])
            if kwargs.get("checkOutDate"):
                date_param["checkOutDate"] = str(kwargs["checkOutDate"])
            arguments["dateParam"] = date_param

        occupancy: Dict[str, Any] = {}
        if kwargs.get("adultCount") is not None:
            occupancy["adultCount"] = int(kwargs["adultCount"])
        if kwargs.get("roomCount") is not None:
            occupancy["roomCount"] = int(kwargs["roomCount"])
        if kwargs.get("childCount") is not None:
            occupancy["childCount"] = int(kwargs["childCount"])
        if kwargs.get("childAgeDetails"):
            occupancy["childAgeDetails"] = list(kwargs["childAgeDetails"])
        if occupancy:
            arguments["occupancyParam"] = occupancy

        filter_options: Dict[str, Any] = {}
        if kwargs.get("cancelPolicy"):
            filter_options["cancelPolicy"] = str(kwargs["cancelPolicy"])
        if kwargs.get("mealType"):
            filter_options["mealType"] = str(kwargs["mealType"])
        if filter_options:
            arguments["filter"] = filter_options

        result = self._client.call_tool("getHotelDetail", arguments)
        return self._normalize_detail_result(result)

    # -- 结果标准化 --------------------------------------------------------

    @staticmethod
    def _normalize_search_result(result: Any) -> Dict[str, Any]:
        if not isinstance(result, dict):
            return {"hotels": [], "raw": result}

        candidates = None
        for key in ("hotels", "hotelList", "hotelInformationList", "data", "list", "results", "items"):
            if isinstance(result.get(key), list):
                candidates = result[key]
                break
        if candidates is None:
            # 不确定结构时保留原始返回，避免丢数据
            return result

        normalized = [HotelToolLive._normalize_hotel_item(item) for item in candidates]
        return {
            "hotels": normalized,
            "count": len(normalized),
            "raw": result,
        }

    @staticmethod
    def _normalize_hotel_item(item: Any) -> Dict[str, Any]:
        if not isinstance(item, dict):
            return {"raw": item}

        location = item.get("location")
        if not isinstance(location, dict):
            if item.get("latitude") is not None and item.get("longitude") is not None:
                location = {"lat": item.get("latitude"), "lng": item.get("longitude")}
            elif item.get("lat") is not None and item.get("lng") is not None:
                location = {"lat": item.get("lat"), "lng": item.get("lng")}

        price = item.get("price")
        if isinstance(price, dict):
            price_per_night = price.get("lowestPrice") or price.get("price")
        else:
            price_per_night = price

        price_per_night = (
            price_per_night
            or item.get("minPrice")
            or item.get("lowestPrice")
            or item.get("pricePerNight")
        )

        return {
            "id": item.get("hotelId") or item.get("hotel_id") or item.get("id") or "",
            "name": item.get("name") or item.get("hotelName") or "",
            "name_en": item.get("nameEn") or "",
            "brand": item.get("brand") or "",
            "location": location,
            "star": item.get("starRating") or item.get("star") or item.get("starLevel"),
            "rating": item.get("rating"),
            "price_per_night": price_per_night,
            "address": item.get("address") or "",
            "tags": item.get("tags") or item.get("labels") or item.get("hotelTags") or [],
            "booking_url": item.get("bookingUrl") or "",
            "image_url": item.get("imageUrl") or "",
            "open": item.get("open", True),
        }

    @staticmethod
    def _normalize_detail_result(result: Any) -> Dict[str, Any]:
        """把 RollingGo getHotelDetail 原始返回整理成 C 更友好的结构。"""
        if not isinstance(result, dict):
            return {"raw": result}

        room_rate_plans = result.get("roomRatePlans") or []
        normalized_rooms = []
        for plan in room_rate_plans:
            if not isinstance(plan, dict):
                normalized_rooms.append({"raw": plan})
                continue
            room_info = plan.get("roomInfo") or {}
            normalized_rooms.append({
                "room_name": plan.get("roomName") or "",
                "rate_plan_id": plan.get("ratePlanId") or "",
                "rate_plan_name": plan.get("ratePlanName") or "",
                "average_price": plan.get("averagePrice"),
                "currency": plan.get("currency") or "",
                "meal_amount": plan.get("mealAmount") or 0,
                "meal_type": plan.get("mealTypeStr") or "",
                "on_request": plan.get("isOnRequest", False),
                "cancel_policy": plan.get("cancelPolicy") or "",
                "cancelable": plan.get("cancelable", False),
                "room_info": {
                    "has_wifi": room_info.get("hasWifi", False),
                    "has_window": room_info.get("hasWindow", False),
                    "max_occupancy": room_info.get("maxOccupancy"),
                    "size": room_info.get("size") or "",
                    "floor": room_info.get("floor") or "",
                    "smoking": room_info.get("smoking") or "",
                    "images": room_info.get("images") or "",
                },
            })

        return {
            "hotelId": result.get("hotelId"),
            "name": result.get("name") or "",
            "starRating": result.get("starRating"),
            "checkIn": result.get("checkIn") or "",
            "checkOut": result.get("checkOut") or "",
            "bookingUrl": result.get("bookingUrl") or "",
            "rooms": normalized_rooms,
            "raw": result,
        }
