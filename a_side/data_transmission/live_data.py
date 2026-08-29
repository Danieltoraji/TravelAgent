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
import re
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from data_transmission.city_travel import CityTravelEdge
from data_transmission.hotel import Hotel
from data_transmission.restaurant import Restaurant


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


# -- 单对 ETA 容错：瞬时/配额类错误退避重试（A 档，8.25） ----------------------

import logging  # noqa: E402
import time  # noqa: E402

logger = logging.getLogger("data_transmission.live_data")

_ETA_RETRIES = 2          # 首次 + 1 次重试
_ETA_RETRY_SLEEP = 0.3    # 退避秒数（压 QPS 突刺）
_ETA_TRANSIENT_MARKERS = (
    "10021", "CUQPS", "QPS", "OVER_LIMIT", "EXCEEDED", "LIMIT",
    "429", "TIMEOUT", "超时", "TIME_OUT",
)


def _is_transient_eta_error(text: str) -> bool:
    """判断地图 ETA 错误是否为瞬时/配额类（可退避重试）。

    高德免费 key 的 QPS/日配额超限（如 ``10021 CUQPS_HAS_EXCEEDED_THE_LIMIT``）
    重试可恢复；``30001 ENGINE_RESPONSE_DATA_ERROR`` 等非瞬时错误不重试。
    """
    if not text:
        return False
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _ETA_TRANSIENT_MARKERS)


def _result_error_text(result: Any) -> str:
    """取 B 工具返回里的 error 文案（兼容 ToolResult 对象与 dict）。"""
    err = getattr(result, "error", None)
    if err is None and isinstance(result, dict):
        err = result.get("error")
    return str(err or "")


def _coord_str(location: Any) -> Optional[str]:
    """A 侧 spot 的 location → ``"lng,lat"`` 坐标字符串（坐标缺失/全 0 → None）。

    B 侧 map 工具把它当坐标直连（跳过地理编码）；与高德经纬度口径一致。
    """
    if not isinstance(location, dict):
        return None
    lat = _as_float(location.get("lat"))
    lng = _as_float(location.get("lng"))
    if not lat and not lng:
        return None
    return f"{lng},{lat}"


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


def _pick(raw: dict, primary: str, *aliases: str) -> Any:
    """A3：按主字段取值（真值语义与 ``or`` 链一致）；命中别名时打 debug。

    用途：收集"工具输出漂移到别名"的频率，为 output_schema 强契约（见
    docs/tool_encapsulation_design_20260828.md §2）的收敛提供依据。
    """
    value = raw.get(primary)
    if value:
        return value
    for alias in aliases:
        value = raw.get(alias)
        if value:
            logger.debug("live_data 别名命中: 主字段 %s 缺失，使用别名 %s", primary, alias)
            return value
    return None


def normalize_live_spot(raw: Any, city: str, index: int = 0) -> Optional[Dict[str, Any]]:
    """B 的 scenic/POI 返回 → A 的 spot dict（对齐 ``fake_spots/{city}/spots.json``）。

    字段缺省兜底：无名 POI 丢弃（返回 None）；位置/价格/时长/营业时间缺失
    用默认值，保证下游 ``select_spots`` / 排程不炸。

    主字段声明（A3，别名命中会打 debug）：
    name / location{"lat","lng"} / id / price / duration / opening_time / closing_time / tags
    """
    if not isinstance(raw, dict):
        return None
    name = _as_str(_pick(raw, "name", "title", "poi_name"))
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
        "id": _as_str(_pick(raw, "id", "poi_id") or f"live_{index}"),
        "name": name,
        "alias": _str_list(raw.get("alias") or raw.get("aliases")),
        "city": city,
        "location": {"lat": lat, "lng": lng},
        "price": _as_float(_pick(raw, "price", "ticket_price")),
        "guide_price": _as_float(raw.get("guide_price")),
        "duration": _as_int(
            _pick(raw, "duration", "suggest_duration", "stay_minutes"),
            default=120,
        ),
        "opening_time": _sanitize_time(
            raw.get("opening_time") or raw.get("open_time"), "09:00"
        ),
        "closing_time": _sanitize_time(
            raw.get("closing_time") or raw.get("close_time"), "17:00"
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


_TIME_TOKEN_RE = re.compile(r"\d{1,2}:\d{2}")


def _sanitize_time(value: Any, default: str) -> str:
    """真实营业时间可能多段/杂乱（"14:00 18:30-22:00"）→ 取首个合法 HH:MM，否则默认。

    下游 ``algorithoms._common._parse_time`` 只认严格 ``HH:MM``，杂串会整链回退
    （线上成都 8.26 教训：scenic 候选池带进一个乱格式营业时间 → live 规划抛
    ValueError → live_fallback）。清洗放在适配层，排程层零改动。
    """
    text = _as_str(value).strip()
    if not text:
        return default
    match = _TIME_TOKEN_RE.search(text)
    if not match:
        return default
    hh_text, mm_text = match.group(0).split(":", 1)
    try:
        hour, minute = int(hh_text), int(mm_text)
    except (TypeError, ValueError):
        return default
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return default
    return f"{hour:02d}:{mm_text}"


class LiveSpotsSource:
    """包裹 ``tool_provider`` 的景点候选提供器：``fn(city) -> List[spot dict]``。

    - 调用 ``tool_provider.call("scenic", place=city)`` 拉候选并逐条规范化；
    - 一个可用景点都没有 → ``LiveDataError``（由调用方回退假源）；
    - ``names`` 属性：{spot_id/name: spot_name}，供交通 provider 建 id→名称映射。
    """

    def __init__(self, tool_provider: Any):
        self.tool_provider = tool_provider
        self.names: Dict[str, str] = {}
        # B 档（8.25）：缓存最近一次拉取的候选池（含 location），供矩阵构建读坐标
        self.spots: List[Dict[str, Any]] = []

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
        self.spots = spots
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
        last_error = ""
        for attempt in range(_ETA_RETRIES):
            try:
                result = tool_provider.call(
                    "map",
                    origin=origin,
                    destination=destination,
                    **base_kwargs,
                )
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if _is_transient_eta_error(last_error):
                    time.sleep(_ETA_RETRY_SLEEP)
                    continue
                raise LiveDataError(
                    f"map.route 调用失败：{origin} → {destination}：{last_error}"
                ) from exc
            last_error = _result_error_text(result)
            if _is_transient_eta_error(last_error):
                time.sleep(_ETA_RETRY_SLEEP)
                continue
            payload = _tool_payload(result)
            minutes = _minutes_from_payload(payload)
            if minutes is None:
                raise LiveDataError(
                    f"map.route 未返回有效时长：{origin} → {destination}"
                    f"（payload={payload}，error={last_error or '无'}）"
                )
            return _distance_from_payload(payload), minutes
        raise LiveDataError(
            f"map.route 重试后仍失败：{origin} → {destination}：{last_error}"
        )

    return eta_fn


# ---------------------------------------------------------------------------
# 交通（B 档）：一次 batch_route 取整矩阵（坐标直连，跳过地理编码）
# ---------------------------------------------------------------------------


def make_live_matrix_fn(
    tool_provider: Any,
    city: Optional[str] = None,
) -> Callable[..., Dict[Tuple[str, str], Tuple[float, int]]]:
    """返回 ``matrix_fn(name_to_coord, origins=None, destinations=None)``。

    返回 ``{(coord, coord): (km, minutes)}``（键为 ``"lng,lat"`` 坐标对）。
    一次 ``map action="batch_route"``（/v3/distance，驾车近似）取矩阵：
    - 输入 ``{名称: "lng,lat"}``，B 侧坐标直连跳过地理编码 → 消灭 QPS 突刺
      （10021）与怪名 POI 编码失败（30001）——C 端真源联调暴露的两个结构性失败；
    - ``origins`` / ``destinations``：可选**名称列表**（各自取 ``name_to_coord``
      的坐标）；缺省 = 全部键 → 同集合整矩阵（与 8.25 起行为一致）。8.30 矩阵
      瘦身：第二阶段只取「计划内景点 → 真源餐厅」正交子矩阵（远小于全集合
      n²），餐厅失败/锚点缺失时该段整体跳过；
    - 整矩阵失败（调用异常 / 无 rows / 空矩阵）→ ``LiveDataError`` 交上层回退假源；
    - 个别行缺数据时由 ``LiveTravelTimeProvider`` 单边降级，不打断主链路。
    """

    def matrix_fn(
        name_to_coord: Dict[str, str],
        origins: Optional[Sequence[str]] = None,
        destinations: Optional[Sequence[str]] = None,
    ) -> Dict[Tuple[str, str], Tuple[float, int]]:
        origin_names: Sequence[str] = (
            list(origins) if origins is not None else list(name_to_coord)
        )
        dest_names: Sequence[str] = (
            list(destinations) if destinations is not None else list(name_to_coord)
        )
        origin_coords = [name_to_coord[name] for name in origin_names]
        dest_coords = [name_to_coord[name] for name in dest_names]
        kwargs: Dict[str, Any] = {
            "action": "batch_route",
            "origins": origin_coords,
            "destinations": dest_coords,
            "mode": "driving",
        }
        if city:
            kwargs["city"] = city
        result = None
        last_error_text = ""
        for attempt in range(_ETA_RETRIES):
            try:
                result = tool_provider.call("map", **kwargs)
            except Exception as exc:  # noqa: BLE001  免费 key 批量接口 QPS 低：瞬时错误重试
                last_error_text = str(exc)
                if attempt == 0 and _is_transient_eta_error(last_error_text):
                    time.sleep(_ETA_RETRY_SLEEP * 2)
                    continue
                raise LiveDataError(
                    f"map.batch_route 调用失败：{last_error_text}"
                ) from exc
            err_text = _result_error_text(result)
            if attempt == 0 and _is_transient_eta_error(err_text):
                time.sleep(_ETA_RETRY_SLEEP * 2)
                continue
            last_error_text = err_text
            break
        if result is None:
            raise LiveDataError(
                f"map.batch_route 重试后仍失败：{last_error_text}"
            )
        payload = _tool_payload(result)
        # B 侧 batch_route 的 data 直接是 rows 列表（8.26 实测：MapTool._batch_route
        # 返回 List[dict]，ToolResult.data = rows）；旧式 {"rows": [...]} 包裹也兼容。
        # 解析错形状会整链回退假源（线上三城 400/假池教训），两形状都必须在解析期接住。
        if isinstance(payload, list):
            rows = payload
        elif isinstance(payload, dict):
            rows = payload.get("rows")
        else:
            rows = None
        if not isinstance(rows, list):
            raise LiveDataError(f"map.batch_route 未返回 rows：{payload!r}")
        matrix: Dict[Tuple[str, str], Tuple[float, int]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            o, d = row.get("origin"), row.get("destination")
            if o is None or d is None:
                continue
            minutes = _minutes_from_payload(row)
            if minutes is None:
                continue
            matrix[(str(o), str(d))] = (
                _as_float(row.get("distance_km")),
                minutes,
            )
        if not matrix:
            raise LiveDataError("map.batch_route 返回空矩阵")
        return matrix

    return matrix_fn


# ---------------------------------------------------------------------------
# 酒店：live HotelTool → A 的 Hotel（对齐 load_hotels 口径）
# ---------------------------------------------------------------------------


def _normalize_live_hotel(raw: Any) -> Optional[Hotel]:
    """主字段声明（A3）：id / name / location{"lat","lng"} / rooms[].price / price_per_night / rating / star / tags。"""
    if not isinstance(raw, dict):
        return None
    hotel_id = _as_str(_pick(raw, "id", "hotel_id"))
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
    if not prices:
        # 别名兜底：rooms 无价 → 用顶层 price_per_night
        logger.debug("live_data hotel: rooms 缺价格，回退 price_per_night（id=%s）", hotel_id)
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
# 餐厅：live FoodToolLive → A 的 Restaurant（对齐 load_restaurants 口径）
# ---------------------------------------------------------------------------


def _coord_from_text(location: Any) -> Optional[Tuple[float, float]]:
    """B 侧返回的 location（高德 ``"lng,lat"`` 字符串或 ``{"lat":..,"lng":..}``）→ ``(lat, lng)``。

    无坐标 / 全 0 → None（餐厅无坐标无法计算通勤，由调用方丢弃）。
    """
    if isinstance(location, dict):
        lat, lng = _as_float(location.get("lat")), _as_float(location.get("lng"))
        if lat or lng:
            return (lat, lng)
        return None
    text = _as_str(location)
    parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) != 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    if not lng and not lat:
        return None
    return (lat, lng)


def _split_tags(text: Any) -> Tuple[str, ...]:
    """把 B 侧逗号/分号连接的标签串拆成元组（空 → ()）。"""
    tags = []
    for part in re.split(r"[,;，；]", _as_str(text)):
        part = part.strip()
        if part:
            tags.append(part)
    return tuple(tags)


def _normalize_live_restaurant(raw: Any) -> Optional[Restaurant]:
    """B 端 FoodToolLive 返回的餐厅 dict → A 的 ``Restaurant``；无坐标丢弃。

    坐标兼容三种形状（B 侧 ``_normalize_poi`` 真实输出为**顶层 lat/lng 两字段**，
    无 location 键；个别来源带 ``location`` 的 ``"lng,lat"`` 串或 dict）。

    主字段声明（A3）：name / 顶层 lat+lng（B 侧真实形状）/ id / cuisine /
    specialty / price_per_person；location 串与 average_cost 为别名兜底。
    """
    if not isinstance(raw, dict):
        return None
    name = _as_str(raw.get("name"))
    coord = _coord_from_text(raw.get("location"))
    if coord is None:
        coord = _coord_from_text({"lat": raw.get("lat"), "lng": raw.get("lng")})
    elif raw.get("lat") or raw.get("lng"):
        logger.debug("live_data restaurant: 命中 location 别名（真实输出应为顶层 lat/lng）")
    if not name or coord is None:
        return None
    rid = _as_str(_pick(raw, "id", "poiid")) or f"live_food_{id(raw)}"
    return Restaurant(
        id=rid,
        name=name,
        location=coord,
        cuisine_tags=_split_tags(_pick(raw, "cuisine", "cuisine_tags")),
        signature_tags=_split_tags(_pick(raw, "specialty", "signature_tags")),
        average_cost=_as_float(_pick(raw, "price_per_person", "average_cost")),
        nearby_spot_ids=tuple(_str_list(raw.get("nearby_spot_ids"))),
    )


def make_live_restaurants_provider(
    tool_provider: Any,
) -> Callable[[str], List[Restaurant]]:
    """返回 ``restaurant_provider(city) -> List[Restaurant]``（对齐 load_restaurants 口径）。

    消费 B 端 ``FoodToolLive``（高德 POI 搜索真源餐厅，8.28 规划期接入）；
    工具调用失败 / 返回不可解析 → ``LiveDataError``（调用方决定是否回退假池）；
    餐厅无坐标会被 ``_normalize_live_restaurant`` 丢弃（无法参与通勤）。
    """

    def restaurant_provider(city: str) -> List[Restaurant]:
        try:
            result = tool_provider.call("food", city=city, limit=20)
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(f"food 工具调用失败（city={city}）：{exc}") from exc
        payload = _tool_payload(result)
        items = (
            payload
            if isinstance(payload, list)
            else (payload.get("restaurants") if isinstance(payload, dict) else None)
        )
        if not isinstance(items, list):
            raise LiveDataError(f"food 工具未返回列表：{type(payload).__name__}")
        return [
            restaurant
            for restaurant in (_normalize_live_restaurant(item) for item in items)
            if restaurant
        ]

    return restaurant_provider


# ---------------------------------------------------------------------------
# 城际：live 地图线路 → CityTravelEdge（方案 §三.5，低优先）
# ---------------------------------------------------------------------------


def make_live_city_travel_provider(
    tool_provider: Any,
    mode: str = "train",
) -> Callable[..., Optional[CityTravelEdge]]:
    """返回 ``provider(origin, dest, *, mode=None) -> Optional[CityTravelEdge]``（真源城际查询）。

    构造 ``mode`` 为**缺省方式**；调用时可传 ``mode=`` 覆盖（偏好驱动选方式时，
    ``find_city_travel_preferred`` 先在本地估算表按 ``travel_priority`` 选定方式，
    再以该方式调 provider——避免 provider 短路导致偏好失效）。``mode`` 城际方式
    参数化（train/air/driving，默认 train；批次 2 A2 修复——不再硬编码死的
    ``"train"``）。B 侧 map 工具对 train/air 查估算表（``source=estimate``，
    含车站/机场对），表外自动回退 driving 真源——provider 单调用即完成
    「方式优先 → 兜底」降级，无需 A 侧重试链。

    解析新返回字段：``duration_min/transport_minutes`` → 分钟、
    ``cost_per_person / from_station / to_station / source`` → Edge 对应字段。

    无此线路（工具缺数据 / status=error / 分钟不可解析）返回 None，
    与假表 ``find_city_travel`` 缺边语义一致。
    """
    default_mode = mode

    def city_travel_provider(
        origin: str, destination: str, *, mode: Optional[str] = None
    ) -> Optional[CityTravelEdge]:
        use_mode = mode or default_mode
        try:
            result = tool_provider.call(
                "map",
                action="route",
                origin=origin,
                destination=destination,
                mode=use_mode,
            )
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(
                f"城际线路查询失败：{origin} → {destination}：{exc}"
            ) from exc
        payload = _tool_payload(result)
        minutes = _minutes_from_payload(payload)
        if minutes is None:
            return None
        edge_mode = _as_str(payload.get("mode") or payload.get("transit_mode") or use_mode)
        return CityTravelEdge(
            origin=origin,
            destination=destination,
            transport_minutes=minutes,
            mode=edge_mode,
            cost_per_person=_as_float(payload.get("cost_per_person")),
            from_station=_as_str(payload.get("from_station")),
            to_station=_as_str(payload.get("to_station")),
            source=_as_str(payload.get("source")),
        )

    return city_travel_provider


# ---------------------------------------------------------------------------
# 城际真源：火车（B 侧 train 工具）与航班（B 侧 flight 工具）→ CityTravelEdge
# ---------------------------------------------------------------------------


def _hhmm_to_minutes(text: Any) -> Optional[int]:
    """"05:24" / "5:24" → 分钟；无法解析返回 None。"""
    t = str(text or "").strip()
    if not t:
        return None
    parts = t.split(":")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (TypeError, ValueError):
        return None


def _pick_representative(
    rows: List[Dict[str, Any]],
    minutes_key: str,
    price_key: str,
    mode: str,
    origin: str,
    destination: str,
) -> Optional[CityTravelEdge]:
    """从真源候选行里选代表边 + 透传全量 candidates。

    代表策略（无 priority 上下文，取折中口径）：
    - 时长 = 候选里**最短**（BFS speed/earliest 决策口径）；
    - 价格 = 候选里**最低**（cost 决策与预算口径都想要最低价）；
    - candidates 全量透传（展示 + 未来班次级 earliest 优化用）。
    行字段缺失（时长不可解析 / 无航班号）逐条跳过。
    """
    if not rows:
        return None
    parsed: List[Tuple[float, float, Dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        minutes = _minutes_from_flight_row(row, minutes_key, price_key)
        if minutes is None:
            continue
        price = _as_float(row.get(price_key))
        parsed.append((minutes, price, row))
    if not parsed:
        return None
    shortest = min(parsed, key=lambda item: item[0])
    cheapest = min(parsed, key=lambda item: (item[1], item[0]))
    best_minutes = int(shortest[0])
    best_price = cheapest[1] if cheapest[1] > 0 else shortest[1]
    best_row = shortest[2]
    candidates: List[Dict[str, Any]] = []
    for minutes, price, row in parsed:
        candidate = dict(row)
        candidate.setdefault("transport_minutes", int(minutes))
        candidate.setdefault("cost_per_person", price)
        candidates.append(candidate)
    return CityTravelEdge(
        origin=origin,
        destination=destination,
        transport_minutes=best_minutes,
        mode=mode,
        cost_per_person=best_price or _as_float(best_row.get(price_key)),
        from_station=_as_str(best_row.get("from_station") or best_row.get("from_airport")),
        to_station=_as_str(best_row.get("to_station") or best_row.get("to_airport")),
        source="live",
        candidates=tuple(candidates),
    )


def _minutes_from_flight_row(row: Dict[str, Any], minutes_key: str, price_key: str) -> Optional[int]:
    """候选行 → 分钟（train 的 duration"05:24" 或 flight 的 duration_min 整数）。"""
    if minutes_key == "duration_min":
        minutes = _as_int(row.get("duration_min"))
        if minutes and minutes > 0:
            return minutes
        return None
    return _hhmm_to_minutes(row.get(minutes_key))


def make_live_train_provider(
    tool_provider: Any,
    date: str,
) -> Callable[..., Optional[CityTravelEdge]]:
    """返回 ``provider(origin, dest, *, mode=None) -> Optional[CityTravelEdge]``（12306 火车真源）。

    调 B 侧 ``train_ticket``（余票/时刻）+ ``train_price``（票价）双工具：
    - 车次候选全量 → ``Edge.candidates``（code/时刻/历时/座位/票价）；
    - 代表边：最短历时（BFS speed/earliest 口径），价格取全候选最低
      （cost 与预算口径；真源价缺失时由上层回落本地估算价兜底）；
    - 无车次（表外/全部停运）→ None（不假装，走本地估算/联运降级）；
    - 工具失败 → LiveDataError（由 BPlannerHook 捕获回退假源）。

    注意：城市对入参为 A 侧城市名；B 侧 train 工具要求站名/电报码，
    城市名→主站映射由工具层（stations）处理，入参直接传城市名即可。
    """

    def train_provider(
        origin: str, destination: str, *, mode: Optional[str] = None
    ) -> Optional[CityTravelEdge]:
        if mode not in (None, "train", "rail"):
            return None  # 只服务火车方式；其它 mode 交还给上游决策
        try:
            tickets = _tool_payload(tool_provider.call(
                "train_ticket", from_station=origin, to_station=destination,
                date=date,
            ))
        except LiveDataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(
                f"城际火车查询失败：{origin} → {destination}：{exc}"
            ) from exc
        if tickets is None:
            return None
        return _pick_representative(
            tickets, "duration", "price", "train", origin, destination,
        )

    return train_provider


def make_live_flight_provider(
    tool_provider: Any,
    date: str,
) -> Callable[..., Optional[CityTravelEdge]]:
    """返回 ``provider(origin, dest, *, mode=None) -> Optional[CityTravelEdge]``（航班真源）。

    调 B 侧 ``flight_search``（juhe 聚合数据-航班查询 1962）：
    - 航班候选全量 → ``Edge.candidates``（flight_no/航司/时刻/历时/票价）；
    - 代表边：最短历时航班（BFS speed/earliest 口径），价格取全候选最低；
    - ``flightInfo`` 为空（无直达）→ None（不假装，走本地估算/联运降级）；
    - 工具失败 / juhe 业务错误 → LiveDataError（由 BPlannerHook 捕获回退假源）。

    mode 契约为 ``"air"``（绝不返 ``rail``；见交接文档 §3.5 踩坑）。
    """

    def flight_provider(
        origin: str, destination: str, *, mode: Optional[str] = None
    ) -> Optional[CityTravelEdge]:
        if mode not in (None, "air"):
            return None
        try:
            flights = _tool_payload(tool_provider.call(
                "flight_search", from_city=origin, to_city=destination,
                date=date,
            ))
        except LiveDataError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise LiveDataError(
                f"城际航班查询失败：{origin} → {destination}：{exc}"
            ) from exc
        if flights is None:
            return None
        return _pick_representative(
            flights, "duration_min", "price", "air", origin, destination,
        )

    return flight_provider


def make_live_intercity_provider(
    tool_provider: Any,
    schedule: Dict[str, Any],
    origin: str = "",
    destination: str = "",
) -> Callable[..., Optional[CityTravelEdge]]:
    """返回组合城际真源 provider：train 真源 → flight 真源 → map 估算兜底。

    ``origin`` / ``destination`` 为行程主方向（去程 origin→destination、
    返程 destination→origin），用于按方向自动选查询日期：
    - 去程用 ``travel_schedule.departure_date``；
    - 返程用 ``return_date``；
    - 方向无法判断或缺日期 → 该模式不查真源（返回 None，走估算）。
    任何真源失败（工具缺 / 无直达 / 网络）都落回 map 估算 provider——
    ``find_city_travel_preferred`` 的真源-None-回落链已兜底，不炸规划。
    """

    def _direction_date(o: str, d: str) -> str:
        if o == origin and d == destination:
            return (schedule.get("departure_date") or "").strip()
        if d == origin and o == destination:
            return (schedule.get("return_date") or "").strip()
        return ""

    map_provider = make_live_city_travel_provider(tool_provider, mode="train")

    def intercity_provider(
        o: str, d: str, *, mode: Optional[str] = None
    ) -> Optional[CityTravelEdge]:
        date_str = _direction_date(o, d)
        if date_str:
            if mode in (None, "train", "rail"):
                # P2b（PR#5）：train_trip 技能优先——站名解析更健壮（估算表城市对
                # → 站名）+ 二等座真票价；无班次/未收录城市对返回 None → 回落本地
                # train_ticket 候选版（多车次 + candidates 全量透传）。
                edge = make_live_train_trip_provider(tool_provider, date_str)(
                    o, d, mode="train"
                )
                if edge is None:
                    edge = make_live_train_provider(tool_provider, date_str)(
                        o, d, mode="train"
                    )
                if edge is not None:
                    return edge
            if mode in (None, "air"):
                flight_provider = make_live_flight_provider(tool_provider, date_str)
                edge = flight_provider(o, d, mode="air")
                if edge is not None:
                    return edge
        return map_provider(o, d, mode=mode or "train")

    return intercity_provider


def make_live_train_trip_provider(tool_provider: Any, date: str = ""):
    """train_trip 技能 → CityTravelEdge provider（P2b 城际真源化，0829）。

    契约与 ``make_live_city_travel_provider`` 相同：f(origin, destination,
    mode=None) → Optional[CityTravelEdge]。差异：
    - ``date`` 为出行日期（12306 查询必填，来自 requirement.travel_schedule，
      由 b_planner_hook 传入）；
    - 仅支持 train 方式（mode 非 train/None → None，A 侧回退本地估算表）；
    - 工具失败 / 无班次 / 未收录城市对 → None（回退估算），不再整链抛错。
    """

    def provider(origin, destination, mode=None):
        if mode not in (None, "train"):
            return None
        try:
            result = tool_provider.call(
                "train_trip", from_city=origin, to_city=destination, date=date,
            )
        except Exception as exc:  # noqa: BLE001  工具缺失/参数错 → 回退估算
            logger.warning("train_trip 调用失败（%s→%s）：%s", origin, destination, exc)
            return None
        payload = _tool_payload(result)
        if not isinstance(payload, dict):
            return None
        try:
            minutes = int(payload.get("transport_minutes"))
        except (TypeError, ValueError):
            return None
        if minutes <= 0:
            return None
        return CityTravelEdge(
            origin=origin,
            destination=destination,
            transport_minutes=minutes,
            mode="train",
            cost_per_person=_as_float(payload.get("cost_per_person")),
            from_station=payload.get("from_station", ""),
            to_station=payload.get("to_station", ""),
            source=payload.get("source", "live"),
        )

    return provider
