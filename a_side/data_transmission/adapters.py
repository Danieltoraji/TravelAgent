"""工具输出适配器层（架构整理方案 P3：B ToolResult → A 内部类型，翻译方向倒置）。

**定位**：以前是 A 产出翻成 B 方言（b_contract.py 方向），本模块反着来——
B 工具返回的**自由形状**（高德 POI / juhe 航班 / FoodToolLive 等）翻成 A 方言
（spot dict / ``Hotel`` / ``Restaurant`` / ``CityTravelEdge`` / 班次候选元组）。

与 ``live_data.py`` 的边界（P3 拆三块之一）：
- 本模块：**纯转换函数**——输入 raw 输出 A 类型，无 tool_provider 调用、
  无缓存、无节律、无预算（可单测、无副作用）；
- ``live_data.py``：**组装入口**——工厂（``make_live_*``）+ tool_provider
  调用 + 额度管家（``QuotaManager``）+ 缓存；
- ``quota_manager.py``：额度管家（per-mode 预算 + 节律 + 计数）。

``live_data.py`` 顶部 ``from data_transmission.adapters import ...`` 引入
（历史 ``from data_transmission.live_data import normalize_live_spot`` 等
import 路径因 re-export 保持可用）。
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from data_transmission.city_travel import CityTravelEdge
from data_transmission.enums import Source
from data_transmission.hotel import Hotel
from data_transmission.restaurant import Restaurant

logger = logging.getLogger("data_transmission.adapters")


# ---------------------------------------------------------------------------
# 通用小工具（纯函数，供各转换函数复用）
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


def _pick(raw: dict, primary: str, *aliases: str) -> Any:
    """A3：按主字段取值（真值语义与 ``or`` 链一致）；命中别名时打 debug。

    用途：收集"工具输出漂移到别名"的频率，为 output_schema 强契约的收敛
    提供依据。
    """
    value = raw.get(primary)
    if value:
        return value
    for alias in aliases:
        value = raw.get(alias)
        if value:
            logger.debug("adapters 别名命中: 主字段 %s 缺失，使用别名 %s", primary, alias)
            return value
    return None


# ---------------------------------------------------------------------------
# 景点：live scenic/POI → A 的 spot dict
# ---------------------------------------------------------------------------

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
        # 9.2 十一节层二：透传高德 rating（B 侧 scenic 工具返回，select_spots
        # 据此质量评分；假池 spots.json 无此字段 → 缺省 0 → 假数据零回归）
        "rating": _as_float(raw.get("rating")),
    }


def _fake_spot_duration_map(city: str) -> Dict[str, int]:
    """真源候选的人工标注时长表：``{景点名 → duration}``（供 true-source 候选补全）。

    高德 POI 不提供「建议游玩时长」（8.31 demo2 结论）——B 侧 scenic 工具给每个
    候选固定 ``suggest_duration=120``，故宫也按 2h 排。独立时长表
    ``data_transmission/spot_durations.json``（A 侧 repo / B 侧 a_side 各一份）
    按城市别名索引（``city_graph.CITY_DIRECTORY_ALIASES``：北京→beijing），存放
    人工标好的差异化时长（故宫=360min）——真源候选按名字覆盖，命中用表值、
    未命中保持工具默认（120）；表缺失/损坏/城市未收录 → 空表（不覆盖不报错）。

    用**独立表而非假池 spots.json**：假池是 A 侧 400+ 测试的共享输入（replanner
    等按 BJ_001 故宫 180 断言），直接改会波及其他分支——时长补全只影响真源链。
    """
    from data_transmission.city_graph import (
        CITY_DIRECTORY_ALIASES,
        normalize_city_name,
    )

    alias = CITY_DIRECTORY_ALIASES.get(city) or CITY_DIRECTORY_ALIASES.get(
        normalize_city_name(city)
    )
    durations_json = Path(__file__).resolve().parent / "spot_durations.json"
    if not durations_json.is_file():
        return {}
    try:
        with open(durations_json, "r", encoding="utf-8") as fh:
            table = json.load(fh)
    except (OSError, ValueError) as exc:
        logger.warning("spot_durations.json 读取失败（%s）：%s", durations_json, exc)
        return {}
    if not isinstance(table, dict):
        return {}
    rows = table.get(alias or city)
    if not isinstance(rows, dict):
        return {}
    durations: Dict[str, int] = {}
    for name, raw_duration in rows.items():
        duration = _as_int(raw_duration)
        if name and duration and duration > 0:
            durations[str(name)] = int(duration)
    return durations


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
        logger.debug("adapters hotel: rooms 缺价格，回退 price_per_night（id=%s）", hotel_id)
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


# ---------------------------------------------------------------------------
# 餐厅：B 端 FoodToolLive → A 的 Restaurant
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


# 池质量过滤（8.31 P0）：高德 POI 类型里非「正餐」的类别——实测城市级搜索会把
# 酒吧（The Captain，type=娱乐场所）、主食铺（宫门口馒头铺）混进餐厅池，被
# 时间轴选中后观感极差（「午餐=酒吧」）。
_NON_RESTAURANT_TYPE_KEYWORDS = ("娱乐场所", "宾馆", "酒店", "旅馆", "住宿")


def _is_non_restaurant(item: Dict[str, Any]) -> bool:
    """POI 类型含非正餐类别（娱乐场所/住宿等）→ True（从池中过滤）。"""
    type_text = _as_str(item.get("type") or item.get("cuisine"))
    return any(keyword in type_text for keyword in _NON_RESTAURANT_TYPE_KEYWORDS)


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
        logger.debug("adapters restaurant: 命中 location 别名（真实输出应为顶层 lat/lng）")
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


# ---------------------------------------------------------------------------
# 城际：live train/flight 候选行 → CityTravelEdge（代表边 + candidates 透传）
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


def _minutes_from_flight_row(row: Dict[str, Any], minutes_key: str, price_key: str) -> Optional[int]:
    """候选行 → 分钟（train 的 duration"05:24" 或 flight 的 duration_min 整数）。"""
    if minutes_key == "duration_min":
        minutes = _as_int(row.get("duration_min"))
        if minutes and minutes > 0:
            return minutes
        return None
    return _hhmm_to_minutes(row.get(minutes_key))


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
    - **站对聚类（十二节缺陷1，2026-09-05）**：12306 子票含全部中途站对
      （北京→天津 292 张票拆出 北京南→天津 78 条 + 亦庄→武清 16min 碎片段
      等），代表边若全局取「最短行」会命中碎片段，站对元数据与 local 腿全被
      带偏——先按 (from_station, to_station) 聚类，选**车次最多**的站对
      （频次 = 主干度；有站对优先于缺站对），站对内再取折中；
    - 时长 = 站对内**最短**（BFS speed/earliest 决策口径）；
    - 价格 = 站对内**最低**（cost 决策与预算口径都想要最低价）；
    - candidates 全量透传（展示 + 班次级精排 `_realize_outbound_with_schedule`
      从全量候选里按时刻选班，不受站对聚类限制）。
    行字段缺失（时长不可解析 / 无航班号）逐条跳过。
    """
    if not rows:
        return None
    parsed: List[Tuple[float, float, str, str, Dict[str, Any]]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        minutes = _minutes_from_flight_row(row, minutes_key, price_key)
        if minutes is None:
            continue
        price = _as_float(row.get(price_key))
        st_from = _as_str(row.get("from_station") or row.get("from_airport"))
        st_to = _as_str(row.get("to_station") or row.get("to_airport"))
        parsed.append((minutes, price, st_from, st_to, row))
    if not parsed:
        return None
    groups: Dict[Tuple[str, str], List[Tuple[float, float, str, str, Dict[str, Any]]]] = {}
    for item in parsed:
        groups.setdefault((item[2], item[3]), []).append(item)
    (st_from, st_to), pool = min(
        groups.items(),
        key=lambda kv: (
            0 if kv[0] != ("", "") else 1,  # 有站对的组优先于缺站对
            -len(kv[1]),  # 车次最多 = 主干站对
            min(item[0] for item in kv[1]),  # 并列取最短
            min((item[1] for item in kv[1] if item[1] > 0), default=0.0),  # 再取最低价
        ),
    )
    shortest = min(pool, key=lambda item: item[0])
    best_minutes = int(shortest[0])
    best_row = shortest[4]
    priced = [item[1] for item in pool if item[1] > 0]
    best_price = min(priced) if priced else _as_float(best_row.get(price_key))
    candidates: List[Dict[str, Any]] = []
    for minutes, price, _sf, _st, row in parsed:
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
        from_station=st_from,
        to_station=st_to,
        source=Source.LIVE.value,
        candidates=tuple(candidates),
    )


def _train_candidates_from_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    """train_trip payload → 班次候选（city_travel.py 候选结构约定）。

    优先 ``payload["trains"]``（9.2 b：train_trip 全量可预订班次，已含
    code/时刻/历时/票价/站点）；旧 payload 无 ``trains`` 时兜底单条最佳
    （code/depart_time/arrive_time 组装 1 条）——保证「按到家时刻选最晚
    班次」（_select_return_combination）在真源铁路候选总有米可择。
    """

    def _minutes_int(value: Any) -> int:
        text = str(value or "").strip()
        return int(text) if text.isdigit() else 0

    rows = payload.get("trains")
    if isinstance(rows, list) and rows:
        out: List[Dict[str, Any]] = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            dep, arr = r.get("depart_time"), r.get("arrive_time")
            if not dep or not arr:
                continue
            price = r.get("price")
            out.append({
                "code": r.get("code", ""), "train_no": r.get("train_no", ""),
                "depart_time": dep, "arrive_time": arr,
                "duration": r.get("duration", ""),
                "price": price, "cost_per_person": price,
                "seats": r.get("seats", ""),
                "from_station": r.get("from_station", ""),
                "to_station": r.get("to_station", ""),
                "transport_minutes": _minutes_int(r.get("transport_minutes")),
            })
        if out:
            return tuple(out)
    dep, arr = payload.get("depart_time"), payload.get("arrive_time")
    if dep and arr:
        price = payload.get("cost_per_person")
        return ({
            "code": payload.get("code", ""), "train_no": payload.get("train_no", ""),
            "depart_time": dep, "arrive_time": arr,
            "duration": payload.get("duration", ""),
            "price": price, "cost_per_person": price,
            "seats": payload.get("seats", ""),
            "from_station": payload.get("from_station", ""),
            "to_station": payload.get("to_station", ""),
            "transport_minutes": _minutes_int(payload.get("transport_minutes")),
        },)
    return ()


# ---------------------------------------------------------------------------
# 适配器编排（供 live_data.py 组装层使用的便捷入口）
# ---------------------------------------------------------------------------


def normalize_hotel(raw: Any) -> Optional[Hotel]:
    """酒店 raw → Hotel（公开名；下划线私有名保留给兼容 re-export）。"""
    return _normalize_live_hotel(raw)


def normalize_restaurant(raw: Any) -> Optional[Restaurant]:
    """餐厅 raw → Restaurant（公开名；下划线私有名保留给兼容 re-export）。"""
    return _normalize_live_restaurant(raw)


def pick_representative_edge(
    rows: List[Dict[str, Any]],
    minutes_key: str,
    price_key: str,
    mode: str,
    origin: str,
    destination: str,
) -> Optional[CityTravelEdge]:
    """候选行 → 代表 CityTravelEdge（公开名）。"""
    return _pick_representative(
        rows, minutes_key, price_key, mode, origin, destination
    )


def train_candidates_from_payload(payload: Dict[str, Any]) -> Tuple[Dict[str, Any], ...]:
    """train_trip payload → 班次候选（公开名）。"""
    return _train_candidates_from_payload(payload)