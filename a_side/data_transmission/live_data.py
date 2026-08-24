"""真实数据接入（USE_LIVE_DATA）开关与 provider 适配层（A 侧，方案 §三 步骤 1-5）。

职责
----
1. **开关**：``use_live_data()`` 读环境变量 ``USE_LIVE_DATA``（"1"/"true"/"yes" → 开；
   默认 0 = 假数据离线，A 的 202 测试门禁与 CI 跑在该值上，不受影响）。
2. **适配层**：把 B 工具层（``tool_provider.call("scenic"/"map"/"hotel"/...)``）返回的
   真实数据规范化为 A 侧现有的数据形状：
   - 景点：``normalize_live_spot`` → 对齐 ``fake_spots/{city}/spots.json`` 的 spot dict；
   - 交通 ETA：``make_live_eta_fn`` → ``fn(起点名, 终点名) -> (distance_km, minutes)``；
   - 酒店：``make_live_hotel_provider`` → ``fn(city) -> List[Hotel]``（对齐 load_hotels 口径）；
   - 城际：``make_live_city_travel_provider`` → ``fn(origin, dest) -> Optional[CityTravelEdge]``。
3. **异常契约**：工具缺失 / 返回不可解析 / 网络失败一律抛 ``LiveDataError``，
   由调用方（如 ``BPlannerHook``）决定回退假源——适配层异常不穿透到规划层。

依赖 B 侧配套（方案 §四 B3/B4/B5）的工具在未就绪时，本层调用会得到
B 的 tool-not-found / 解析失败 → 转成 ``LiveDataError``，属预期行为。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from data_transmission.city_travel import CityTravelEdge
from data_transmission.hotel import Hotel


def use_live_data() -> bool:
    """真实数据开关（USE_LIVE_DATA=1/true/yes → True）。默认假数据。"""
    return os.environ.get("USE_LIVE_DATA", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


class LiveDataError(RuntimeError):
    """真实数据源接入失败（工具缺失 / 返回不可解析 / 网络错误）。

    调用方应捕获并回退假源（规划层兜底），不要让异常穿透到排程。
    """


# ---------------------------------------------------------------------------
# 通用解析小工具
# ---------------------------------------------------------------------------


def _as_str(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value)


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _str_list(value: Any) -> List[str]:
    """把 tags / alias 等字段展平成字符串列表（兼容 list / tuple / dict / 标量）。"""
    if value is None:
        return []
    if isinstance(value, dict):
        out: List[str] = []
        for group in value.values():
            if isinstance(group, (list, tuple)):
                out.extend(str(item) for item in group)
            elif group is not None:
                out.append(str(group))
        return out
    if isinstance(value, (list, tuple, set)):
        return [str(item) for item in value]
    return [str(value)]


def _tool_payload(result: Any) -> Any:
    """从 B 工具返回值里取业务 data（兼容 dict 与 ToolResult 风格对象）。

    - dict 且含 ``data``（list/dict）→ 返回 data；否则返回 dict 本身；
    - 对象（有 ``data`` 属性）→ 返回 data；**status 为 error → None**（降级语义）；
    - None / 其它空值 → LiveDataError。
    """
    if result is None:
        raise LiveDataError("工具返回空结果")
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, (list, dict)) and data:
            return data
        return result
    status = getattr(result, "status", None)
    if status is not None:
        status_value = getattr(status, "value", status)  # enum → 字面值
        if str(status_value).lower() != "ok":
            return None  # 工具层错误（网络/业务）→ 走 None 降级，不炸下游
    data = getattr(result, "data", None)
    if data is not None:
        return data
    return result


def _minutes_from_payload(payload: Any) -> Optional[int]:
    """从地图 route 返回里提取通勤分钟数（兼容多种字段口径）。

    - 明确分钟字段：``transport_minutes`` / ``minutes`` / ``duration_minutes``；
    - ``duration``：高德口径为秒（≥600 视为秒 ÷ 60），小值视为分钟；
    - 嵌套 dict / 列表：只取首个候选 dict 探一次（B3 批量 ETA 落地前不深解析）；
    - 非 dict（None / 错误结果）→ None。
    """
    if not isinstance(payload, dict):
        return None
    candidates = (
        payload.get("transport_minutes"),
        payload.get("minutes"),
        payload.get("duration_minutes"),
    )
    for value in candidates:
        if value is not None:
            try:
                return max(int(float(value)), 0)
            except (TypeError, ValueError):
                continue
    raw_duration = payload.get("duration")
    if raw_duration is not None:
        try:
            seconds = float(raw_duration)
            if seconds >= 600:
                return int(round(seconds / 60))
            return max(int(round(seconds)), 0)
        except (TypeError, ValueError):
            return None
    return None


def _distance_from_payload(payload: Any) -> float:
    """从地图 route 返回里提取公里数；缺省 0（矩阵只关心分钟，距离仅作展示）。"""
    for key in ("distance_km", "distance"):
        if key in payload:
            value = _as_float(payload.get(key))
            if value > 0:
                return value
    return 0.0


# ---------------------------------------------------------------------------
# 景点：live scenic/POI → A 的 spot dict
# ---------------------------------------------------------------------------


def normalize_live_spot(raw: Any, city: str, index: int = 0) -> Optional[Dict[str, Any]]:
    """B 的 scenic/POI 返回 → A 的 spot dict（对齐 ``fake_spots/{city}/spots.json``）。

    字段缺省兜底：无名 POI 丢弃（返回 None）；位置/价格/时长/营业时间缺失
    用默认值，保证下游 ``select_spots`` / 排程不炸。
    """
    if not isinstance(raw, dict):
        return None
    name = _as_str(raw.get("name") or raw.get("title") or raw.get("poi_name"))
    if not name:
        return None

    location = raw.get("location") or raw.get("geo") or raw.get("position")
    lat = lng = 0.0
    if isinstance(location, dict):
        lat, lng = _as_float(location.get("lat")), _as_float(location.get("lng"))
    elif isinstance(location, (list, tuple)) and len(location) >= 2:
        lat, lng = _as_float(location[1]), _as_float(location[0])
    elif isinstance(location, str) and "," in location:
        parts = [part.strip() for part in location.split(",")]
        if len(parts) >= 2:
            lat, lng = _as_float(parts[1]), _as_float(parts[0])  # "lng,lat" 高德口径

    tags = _str_list(raw.get("tags"))
    content_tags = _str_list(raw.get("content_tags")) or tags
    return {
        "id": _as_str(raw.get("id") or raw.get("poi_id") or f"live_{index}"),
        "name": name,
        "alias": _str_list(raw.get("alias") or raw.get("aliases")),
        "city": city,
        "location": {"lat": lat, "lng": lng},
        "price": _as_float(raw.get("price") or raw.get("ticket_price")),
        "guide_price": _as_float(raw.get("guide_price")),
        "duration": _as_int(
            raw.get("duration") or raw.get("suggest_duration") or raw.get("stay_minutes"),
            default=120,
        ),
        "opening_time": _as_str(
            raw.get("opening_time") or raw.get("open_time") or "09:00"
        ),
        "closing_time": _as_str(
            raw.get("closing_time") or raw.get("close_time") or "17:00"
        ),
        "content_tags": content_tags,
        "plan_tags": _str_list(raw.get("plan_tags")),
        "experience_tags": _str_list(raw.get("experience_tags")),
        "reservation_required": bool(
            raw.get("reservation_required")
            or raw.get("reservation")
            or raw.get("need_reservation")
        ),
    }


class LiveSpotsSource:
    """包裹 ``tool_provider`` 的景点候选提供器：``fn(city) -> List[spot dict]``。

    - 调用 ``tool_provider.call("scenic", place=city)`` 拉候选并逐条规范化；
    - 一个可用景点都没有 → ``LiveDataError``（由调用方回退假源）；
    - ``names`` 属性：{spot_id/name: spot_name}，供交通 provider 建 id→名称映射。
    """

    def __init__(self, tool_provider: Any):
        self.tool_provider = tool_provider
        self.names: Dict[str, str] = {}

    def __call__(self, city: str) -> List[Dict[str, Any]]:
        try:
            result = self.tool_provider.call(
                "scenic", action="search", place=city
            )
        except Exception as exc:  # noqa: BLE001  工具层网络/参数错误统一转 LiveDataError
            raise LiveDataError(f"scenic 工具调用失败：{exc}") from exc
        payload = _tool_payload(result)
        if not isinstance(payload, list):
            raise LiveDataError(f"scenic 工具未返回列表：{type(payload).__name__}")
        spots: List[Dict[str, Any]] = []
        for index, raw in enumerate(payload):
            spot = normalize_live_spot(raw, city=city, index=index)
            if spot is not None:
                spots.append(spot)
        if not spots:
            raise LiveDataError(f"scenic 工具未返回可用的景点（city={city}）")
        self.names = {
            str(spot.get("id") or spot["name"]): spot["name"] for spot in spots
        }
        return spots


def make_live_spots_provider(tool_provider: Any) -> LiveSpotsSource:
    """真源景点 provider 工厂（``USE_LIVE_DATA=1`` 时注入 ``select_spots``）。"""
    return LiveSpotsSource(tool_provider)


# ---------------------------------------------------------------------------
# 交通：live 地图 ETA → (distance_km, minutes)
# ---------------------------------------------------------------------------


def make_live_eta_fn(
    tool_provider: Any,
    city: Optional[str] = None,
) -> Callable[[str, str], Tuple[float, int]]:
    """返回 ``eta_fn(origin_name, destination_name) -> (distance_km, minutes)``。

    调用 ``tool_provider.call("map", action="route", mode="driving", ...)``；
    拿不到时长 → ``LiveDataError``。

    0825 修复（C 端真源联调暴露）：
    - ``mode="driving"``（驾车）：公交（transit）对相邻景点返回空（高德
      「公交路线规划返回为空」）、对怪名 POI 报 30001——任一对失败即整链回退
      假源；驾车对任意两坐标稳定有返回（与 B3 批量 ETA 同为驾车口径）；
    - 绑定 ``city``：B 端 map 工具地理编码默认限定北京，不带 city 时非北京
      行程（如上海）全按北京上下文查询 → 高德 30001。BPlannerHook 传 ``self.city``。
    """
    base_kwargs: Dict[str, Any] = {"action": "route", "mode": "driving"}
    if city:
        base_kwargs["city"] = city

    def eta_fn(origin: str, destination: str) -> Tuple[float, int]:
        try:
            result = tool_provider.call(
                "map",
                origin=origin,
                destination=destination,
                **base_kwargs,
            )
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(
                f"map.route 调用失败：{origin} → {destination}：{exc}"
            ) from exc
        payload = _tool_payload(result)
        minutes = _minutes_from_payload(payload)
        if minutes is None:
            raise LiveDataError(
                f"map.route 未返回有效时长：{origin} → {destination}（payload={payload}）"
            )
        return _distance_from_payload(payload), minutes

    return eta_fn


# ---------------------------------------------------------------------------
# 酒店：live HotelTool → A 的 Hotel（对齐 load_hotels 口径）
# ---------------------------------------------------------------------------


def _normalize_live_hotel(raw: Any) -> Optional[Hotel]:
    if not isinstance(raw, dict):
        return None
    hotel_id = _as_str(raw.get("id") or raw.get("hotel_id"))
    name = _as_str(raw.get("name") or hotel_id)
    if not name:
        return None
    location = raw.get("location") or raw.get("geo") or {}
    room_types = raw.get("room_types") or raw.get("rooms") or []
    prices = [
        _as_float(room.get("price"))
        for room in room_types
        if isinstance(room, dict) and room.get("price") is not None
    ]
    night_price = min(prices) if prices else _as_float(raw.get("price_per_night"))
    rating = _as_float(raw.get("rating"))
    star = _as_int(raw.get("star")) or (
        5 if rating >= 4.7 else 4 if rating >= 4.4 else 3
    )
    return Hotel(
        id=hotel_id or f"live_hotel_{id(raw)}",
        name=name,
        location=(
            _as_float(location.get("lat")),
            _as_float(location.get("lng")),
        ),
        price_per_night=night_price,
        rating=rating,
        star=star,
        tags=tuple(_str_list(raw.get("tags"))),
        nearby_spot_ids=tuple(_str_list(raw.get("nearby_spot_ids"))),
    )


def make_live_hotel_provider(
    tool_provider: Any,
) -> Callable[[str], List[Hotel]]:
    """返回 ``hotel_provider(city) -> List[Hotel]``（依赖 B4 HotelTool；未就绪 → LiveDataError）。"""

    def hotel_provider(city: str) -> List[Hotel]:
        try:
            result = tool_provider.call("hotel", city=city)
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(f"hotel 工具调用失败（city={city}）：{exc}") from exc
        payload = _tool_payload(result)
        items = payload.get("hotels", payload) if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            raise LiveDataError(f"hotel 工具未返回列表：{type(payload).__name__}")
        hotels = [
            hotel for hotel in (_normalize_live_hotel(item) for item in items) if hotel
        ]
        return hotels

    return hotel_provider


# ---------------------------------------------------------------------------
# 城际：live 地图线路 → CityTravelEdge（方案 §三.5，低优先）
# ---------------------------------------------------------------------------


def make_live_city_travel_provider(
    tool_provider: Any,
) -> Callable[[str, str], Optional[CityTravelEdge]]:
    """返回 ``provider(origin, dest) -> Optional[CityTravelEdge]``。

    无此线路（工具缺数据）返回 None，与假表 ``find_city_travel`` 缺边语义一致。
    """

    def city_travel_provider(origin: str, destination: str) -> Optional[CityTravelEdge]:
        try:
            result = tool_provider.call(
                "map",
                action="route",
                origin=origin,
                destination=destination,
                mode="train",
            )
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(
                f"城际线路查询失败：{origin} → {destination}：{exc}"
            ) from exc
        payload = _tool_payload(result)
        minutes = _minutes_from_payload(payload)
        if minutes is None:
            return None
        mode = _as_str(payload.get("mode") or payload.get("transit_mode") or "城际交通")
        return CityTravelEdge(
            origin=origin,
            destination=destination,
            transport_minutes=minutes,
            mode=mode,
        )

    return city_travel_provider