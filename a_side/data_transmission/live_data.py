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

import json
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from data_transmission.adapters import (  # noqa: F401  re-export（历史 import 路径兼容）
    _NON_RESTAURANT_TYPE_KEYWORDS,
    _as_float,
    _as_int,
    _as_str,
    _coord_from_text,
    _fake_spot_duration_map,
    _hhmm_to_minutes,
    _is_non_restaurant,
    _minutes_from_flight_row,
    _normalize_live_hotel,
    _normalize_live_restaurant,
    _pick,
    _pick_representative,
    _sanitize_time,
    _split_tags,
    _str_list,
    _train_candidates_from_payload,
    normalize_live_spot,
)
from data_transmission.city_travel import CityTravelEdge
from data_transmission.enums import Mode, Source
from data_transmission.hotel import Hotel
from data_transmission.live_errors import LiveDataError  # noqa: F401  re-export（历史 import 路径兼容）
from data_transmission.quota_manager import (
    QuotaExceeded,
    QuotaManager,
    make_quota_manager,
)
from data_transmission.restaurant import Restaurant


def use_live_data() -> bool:
    """真实数据开关（USE_LIVE_DATA=1/true/yes → True）。默认假数据。"""
    return os.environ.get("USE_LIVE_DATA", "0").strip().lower() in {
        "1",
        "true",
        "yes",
    }


# ---------------------------------------------------------------------------
# 通用解析小工具（已迁至 data_transmission/adapters.py，顶部 re-export）
# ---------------------------------------------------------------------------


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


# （`_pick` / `normalize_live_spot` / `_sanitize_time` / `_fake_spot_duration_map`
#  已迁至 adapters.py，顶部 re-export；此处不再重复定义）


class LiveSpotsSource:
    """包裹 ``tool_provider`` 的景点候选提供器：``fn(city, limit=None) -> List[spot dict]``。

    - 调用 ``tool_provider.call("scenic", place=city, limit=...)`` 拉候选并逐条规范化；
    - 一个可用景点都没有 → ``LiveDataError``（由调用方回退假源）；
    - ``names`` 属性：{spot_id/name: spot_name}，供交通 provider 建 id→名称映射。
    - 时长补全（8.31 demo2）：高德 POI 无「建议游玩时长」字段 → 按景点名取同城
      假池人工标注时长覆盖（``_fake_spot_duration_map``）；未命中保持工具默认。

    候选池宽度（8.30 扩容）：``limit`` 缺省 10（历史行为）；调用方可传
    ``max(10, days×5)`` 让池子随行程天数联动——B 侧 scenic 工具 / amap_client
    支持翻页（>25 自动分页，页间 0.3s 防 QPS）。
    """

    def __init__(self, tool_provider: Any):
        self.tool_provider = tool_provider
        self.names: Dict[str, str] = {}
        # B 档（8.25）：缓存最近一次拉取的候选池（含 location），供矩阵构建读坐标
        self.spots: List[Dict[str, Any]] = []

    def __call__(
        self,
        city: str,
        limit: Optional[int] = None,
        ensure_spots: Optional[List[str]] = None,
        search_plan: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        kwargs: Dict[str, Any] = {"action": "search", "place": city}
        if limit:
            kwargs["limit"] = int(limit)
        if ensure_spots:
            kwargs["ensure_spots"] = [
                str(name) for name in ensure_spots if str(name).strip()
            ]
        # 9.2 十二节 A：LLM 定制搜索计划（可选，None 时 B 侧走固定词表零回归）
        if search_plan:
            kwargs["search_plan"] = search_plan
        try:
            result = self.tool_provider.call("scenic", **kwargs)
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
        # 时长补全（8.31 demo2）：高德 POI 无建议游玩时长 → 取同城假池人工标注
        # duration 覆盖（如 故宫=360min）；未命中保持工具默认（120）。假池缺失/
        # 损坏返回空表 → 不覆盖不报错（真源链路不受影响）。
        fake_durations = _fake_spot_duration_map(city)
        if fake_durations:
            for spot in spots:
                known = fake_durations.get(spot.get("name") or "")
                if known:
                    spot["duration"] = int(known)
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
    base_kwargs: Dict[str, Any] = {"action": "route", "mode": Mode.DRIVING.value}
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
            "mode": Mode.DRIVING.value,
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
# （`_normalize_live_hotel` 已迁至 adapters.py，顶部 re-export）
# ---------------------------------------------------------------------------


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
# （`_coord_from_text` / `_split_tags` / `_NON_RESTAURANT_TYPE_KEYWORDS` /
#  `_is_non_restaurant` / `_normalize_live_restaurant` 已迁至 adapters.py，
#  顶部 re-export）
# ---------------------------------------------------------------------------


def make_live_restaurants_provider(
    tool_provider: Any,
) -> Callable[[str], List[Restaurant]]:
    """返回 ``restaurant_provider(city) -> List[Restaurant]``（对齐 load_restaurants 口径）。

    消费 B 端 ``FoodToolLive``（高德 POI 搜索真源餐厅，8.28 规划期接入）；
    工具调用失败 / 返回不可解析 → ``LiveDataError``（调用方决定是否回退假池）；
    餐厅无坐标会被 ``_normalize_live_restaurant`` 丢弃（无法参与通勤）。
    8.31 P0：池质量过滤——类型含「娱乐场所/宾馆/酒店/旅馆/住宿」的 POI 丢弃。
    """

    def restaurant_provider(city: str) -> List[Restaurant]:
        items = _fetch_food_items(tool_provider, city=city, limit=20)
        return [
            restaurant
            for restaurant in (_normalize_live_restaurant(item) for item in items)
            if restaurant
        ]

    return restaurant_provider


def make_live_nearby_restaurants_pool(
    tool_provider: Any,
    city: str = "",
    k: int = 10,
    radius: int = 2000,
) -> Callable[[Tuple[float, float], int], List[Restaurant]]:
    """返回 ``nearby_pool(anchor_coord, k) -> List[Restaurant]``（锚点附近搜索）。

    8.31 P0 附近搜索模式：以锚点坐标调 B 端 ``food`` 工具的 ``location``
    参数（坐标直连免 geocode，radius 默认 2km）→ 锚点附近真源餐厅。
    失败/无结果时**返回空列表**（不抛）——由 ``RestaurantResolver`` 降级用
    全池（make_live_restaurants_provider 已拉的城市级池），附近模式不阻断主链路。
    """
    # B 侧 page_size 上限 25；k 超出时钳到 25。
    max_k = min(int(k) or 10, 25)

    def nearby_pool(
        anchor_coord: Tuple[float, float], count: int = 10
    ) -> List[Restaurant]:
        location = f"{anchor_coord[1]},{anchor_coord[0]}"  # 高德 "lng,lat"
        try:
            items = _fetch_food_items(
                tool_provider,
                city=city,
                limit=min(int(count) or max_k, 25),
                location=location,
                radius=radius,
            )
        except Exception:  # noqa: BLE001
            return []
        return [
            restaurant
            for restaurant in (_normalize_live_restaurant(item) for item in items)
            if restaurant
        ]

    return nearby_pool


def _fetch_food_items(
    tool_provider: Any,
    city: str,
    limit: int,
    location: str = "",
    radius: int = 0,
) -> List[Any]:
    """调 B 端 food 工具并取业务列表；过滤非正餐 POI 后返回。

    location 非空 → 附近搜索（B 侧 FoodToolLive 的 location/radius 参数）；
    空 → 城市级搜索（8.28 原口径）。失败 → ``LiveDataError``。
    """
    kwargs: Dict[str, Any] = {"city": city, "limit": limit}
    if location:
        kwargs["location"] = location
        if radius:
            kwargs["radius"] = int(radius)
    try:
        result = tool_provider.call("food", **kwargs)
    except Exception as exc:  # noqa: BLE001
        raise LiveDataError(
            f"food 工具调用失败（city={city}, location={location or '-'}）：{exc}"
        ) from exc
    payload = _tool_payload(result)
    # ``{"data": []}``（空列表，falsy）会被 _tool_payload 回退成整个 dict——
    # 空列表是合法结果（该城市/锚点无餐厅），不能当形状错误（8.30 修复：
    # 全池空 + 附近有店的场景曾因此整链丢弃餐厅真源）。
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        items = payload["data"]
    else:
        items = (
            payload
            if isinstance(payload, list)
            else (payload.get("restaurants") if isinstance(payload, dict) else None)
        )
    if not isinstance(items, list):
        raise LiveDataError(f"food 工具未返回列表：{type(payload).__name__}")
    return [item for item in items if not _is_non_restaurant(item)]


# ---------------------------------------------------------------------------
# 城际：live 地图线路 → CityTravelEdge（方案 §三.5，低优先）
# ---------------------------------------------------------------------------


def make_live_city_travel_provider(
    tool_provider: Any,
    mode: str = Mode.TRAIN.value,
) -> Callable[..., Optional[CityTravelEdge]]:
    """返回 ``provider(origin, dest, *, mode=None) -> Optional[CityTravelEdge]``（真源城际查询）。

    构造 ``mode`` 为**缺省方式**；调用时可传 ``mode=`` 覆盖（偏好驱动选方式时，
    ``find_city_travel_preferred`` 先在本地估算表按 ``travel_priority`` 选定方式，
    再以该方式调 provider——避免 provider 短路导致偏好失效）。``mode`` 城际方式
    参数化（train/air/driving，默认 train；批次 2 A2 修复——不再硬编码死的
    ``"train"``）。B 侧 map 工具对 train/air 查估算表（``source=estimated``，
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
# （`_hhmm_to_minutes` / `_pick_representative` / `_minutes_from_flight_row`
#  已迁至 adapters.py，顶部 re-export）
# ---------------------------------------------------------------------------


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
        if mode not in (None, Mode.TRAIN.value):
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
            tickets, "duration", "price", Mode.TRAIN.value, origin, destination,
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
        if mode not in (None, Mode.AIR.value):
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
            flights, "duration_min", "price", Mode.AIR.value, origin, destination,
        )

    return flight_provider


class _CachingProvider:
    """主链城际真源查询的缓存包装（P3-D2a 组装入口）。

    把 ``.call(name, **kwargs)`` 路由到 ``QuotaManager.cached_call``——键
    ``(name, o, d, date)`` 从 kwargs 提取（from_city/from_station → o，
    to_city/to_station → d，date → date）。只包主链三真源
    （train_trip / train_ticket / flight_search）；取不到完整键要素
    （非城际查询）→ 退回普通 ``call``（不缓存、不改变行为）。
    """

    __slots__ = ("_quota",)

    def __init__(self, quota: Any):
        self._quota = quota

    def call(self, name: str, **kwargs: Any) -> Any:
        cached = getattr(self._quota, "cached_call", None)
        if callable(cached):
            # 键要素（from_city/from_station/to_city/to_station/date）由
            # QuotaManager.cached_call 从 kwargs 提取；这里原样透传，真实工具
            # 参数不受影响。键要素不全或未配置缓存 → cached_call 退化为 call。
            return cached(name, **kwargs)
        return self._quota.call(name, **kwargs)


def make_live_intercity_provider(
    tool_provider: Any,
    schedule: Dict[str, Any],
    origin: str = "",
    destination: str = "",
    mode_budget: Optional[Dict[str, int]] = None,
    stats: Optional[Dict[str, int]] = None,
    cache: Optional[Dict[Any, Any]] = None,
) -> Callable[..., Optional[CityTravelEdge]]:
    """返回组合城际真源 provider：train 真源 → flight 真源 → map 估算兜底。

    ``origin`` / ``destination`` 为行程主方向（去程 origin→destination、
    返程 destination→origin），用于按方向自动选查询日期：
    - 去程主方向对（o==origin 且 d==destination）用 ``travel_schedule.departure_date``；
    - 返程主方向对（d==origin 且 o==destination）用 ``return_date``；
    - **BFS 中转段（非主方向对）日期回落主链日期**（本批放宽）：按行程轴方向
      推断——奔向 destination 的链段用去程日期（o==origin 或 d==destination），
      向 origin 返回的链段用返程日期（d==origin 或 o==destination），完全中间段
      优先去程日期（车次/班期按日重复，代表性边跨日期稳定；查询失败回落估算）；
    - 对应方向缺日期 → 该模式该段不查真源（返回 None，走估算）。
    任何真源失败（工具缺 / 无直达 / 网络）都落回 map 估算 provider——
    ``find_city_travel_preferred`` 的真源-None-回落链已兜底，不炸规划。

    ``mode_budget`` / ``stats``（额度纪律落地，P3 起由 ``QuotaManager`` 承载）：
    一次规划内 train_trip / train_ticket / flight_search 各自调用上限（默认
    各 ≤6），超过上限的工具调用抛 ``QuotaExceeded`` → 该模式该段回落估算——
    与 BFS 的懒查询 + (城市对)缓存构成「per-mode 预算」一层，防额度失控
    （车次对多不会挤占航班预算；反之亦然）。

    ``cache``（P3-D2a）：可选外部 dict，传入后主链三真源（train_trip /
    train_ticket / flight_search）自动经 ``QuotaManager.cached_call`` 共享
    ``(name, o, d, date)`` 缓存——同对同日期只查一次真源（命中不计数、
    正负都缓存），BFS 重复展开同一无班次对不再反复查询；缺省 None 不缓存
    （行为零变化）。
    """

    if mode_budget is None:
        # P3-E：per-mode 预算默认值改由 ToolSpec 注册表单点定义（原内联
        # {"train_trip":6,"train_ticket":6,"flight_search":6}，见 tool_specs.py）。
        from data_transmission.tool_specs import intercity_mode_budget

        mode_budget = intercity_mode_budget()
    tool_provider = make_quota_manager(
        tool_provider, mode_budget, stats, cache=cache
    )
    # 城际真源查询的缓存包装：把 .call 路由到 cached_call（(name,o,d,date) 键）。
    # 只包主链三真源（train_trip/train_ticket/flight_search）——map 是估算兜底、
    # 矩阵缓存留在规划层，不入此缓存；unbudgeted 通道走 _UnbudgetedProxy 穿透。
    main_chain_provider = _CachingProvider(tool_provider)

    def _direction_date(o: str, d: str) -> str:
        departure = (schedule.get("departure_date") or "").strip()
        ret = (schedule.get("return_date") or "").strip()
        if o == origin and d == destination:
            return departure
        if d == origin and o == destination:
            return ret
        # BFS 中转段：按行程轴方向回落主链日期（去程侧用去程日期、返程侧用返程日期）
        if o == origin or d == destination:
            return departure
        if d == origin or o == destination:
            return ret
        return departure or ret

    map_provider = make_live_city_travel_provider(tool_provider, mode=Mode.TRAIN.value)

    # 候选生成通道：绕过 per-mode 预算（候选生成器有自己的总量纪律）。P3 起
    # 不再裸穿透 ``_inner``，改走 QuotaManager 的穿透代理（节律 + 无预算）。
    unbudgeted_provider = tool_provider.unbudgeted()

    def train_edge_unbudgeted(o: str, d: str, date_str: str = "") -> Optional[CityTravelEdge]:
        """预算外铁路查询（空铁候选生成器专用）。

        候选生成器有自己的总量纪律（MAX_TRAIN_CALLS=24 + 同对缓存，8.31
        由 12 放宽——北京类枢纽 AirIn 33 城、南宁排第 17 位，旧 12 会截断），
        不应与主链路（直达/BFS）共享 per-mode 预算——共享时去程邻居查询会
        吃光 train_trip/train_ticket 各 6 次预算，返程方向 train 级直接跳过、
        只剩 flight 0 条 → 退化 driving（8.30 demo1 返程实测双杀）。节律
        0.35s 由 QuotaManager 穿透通道承担（12306 限流风暴防护，8.31 实测）。
        """
        try:
            edge = make_live_train_trip_provider(unbudgeted_provider, date_str)(
                o, d, mode=Mode.TRAIN.value
            )
            if edge is None:
                edge = make_live_train_provider(unbudgeted_provider, date_str)(
                    o, d, mode=Mode.TRAIN.value
                )
            if edge is not None and edge.mode in (Mode.TRAIN.value,):
                return edge
        except Exception:  # noqa: BLE001
            return None
        return None

    def intercity_provider(
        o: str, d: str, *, mode: Optional[str] = None
    ) -> Optional[CityTravelEdge]:
        date_str = _direction_date(o, d)
        if date_str:
            if mode in (None, Mode.TRAIN.value):
                # P2b（PR#5）：train_trip 技能优先——站名解析更健壮（估算表城市对
                # → 站名）+ 二等座真票价；无班次/未收录城市对返回 None → 回落本地
                # train_ticket 候选版（多车次 + candidates 全量透传）。
                edge = None
                try:
                    edge = make_live_train_trip_provider(main_chain_provider, date_str)(
                        o, d, mode=Mode.TRAIN.value
                    )
                    if edge is None:
                        edge = make_live_train_provider(main_chain_provider, date_str)(
                            o, d, mode=Mode.TRAIN.value
                        )
                except QuotaExceeded:
                    pass  # 车次预算超限 → 该段不再查车次，转航班/估算（预算按模式独立）
                if edge is not None:
                    return edge
            if mode in (None, Mode.AIR.value):
                flight_provider = make_live_flight_provider(main_chain_provider, date_str)
                try:
                    edge = flight_provider(o, d, mode=Mode.AIR.value)
                except QuotaExceeded:
                    edge = None  # 航班预算超限 → 该段回落估算
                if edge is not None:
                    return edge
        return map_provider(o, d, mode=mode or Mode.TRAIN.value)

    # 候选生成器专用通道（属性挂载，调用方按需取用）：无 per-mode 预算的铁路
    # 查询。候选生成器内部有自己的总量纪律（MAX_TRAIN_CALLS=12 + 同对缓存），
    # 与主链路共享预算会互相饿死（8.30 demo1 返程实测）。
    intercity_provider.train_edge_unbudgeted = train_edge_unbudgeted
    return intercity_provider


# （`_train_candidates_from_payload` 已迁至 adapters.py，顶部 re-export）


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
        if mode not in (None, Mode.TRAIN.value):
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
            mode=Mode.TRAIN.value,
            cost_per_person=_as_float(payload.get("cost_per_person")),
            from_station=payload.get("from_station", ""),
            to_station=payload.get("to_station", ""),
            source=payload.get("source", Source.LIVE.value),
            candidates=_train_candidates_from_payload(payload),
        )

    return provider
