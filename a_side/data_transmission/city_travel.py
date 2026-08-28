"""城际交通表：出发地 → 目的地 的时长与方式（模拟数据，跨城市查表）。

与 `spots_graph.json`（市内景点/餐厅图）互补：市内交通查图，跨城市查本表。

批次 2（8.28）：估算表升级车站粒度（stations 表 + options 站点对）——
- ``CityTravelEdge`` 扩展 ``cost_per_person / from_station / to_station / source``
  （source: "" 假表旧边 / "estimate" 估算表条目 / "live" 真源）；
- ``load_city_travel_options`` 返回 ``{(o,d): {mode: Edge}}`` 全 options；
  ``load_city_travel_edges`` 取 options 默认边（train 优先；旧表无 options 回退
  顶层字段）保持旧读法兼容；
- ``find_city_travel_preferred`` 按偏好链（高铁→飞机→自驾）逐个方式查——对应
  用户 8.28 建议「先定城际方式」，再规划市内衔接（阶段三 legs）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_CITY_TRAVEL_PATH = (
    Path(__file__).resolve().parent.parent / "fake_spots" / "city_travel.json"
)

# 城际方式偏好（来去程段生成时的选择顺序：高铁优先，逐项回退到命中为止）
MODE_PREFERENCE: Tuple[str, ...] = ("train", "air", "driving")

# mode → 中文方式名（段展示用）
_MODE_TEXT: Dict[str, str] = {
    "train": "高铁",
    "air": "飞机",
    "driving": "自驾",
}


def mode_text(mode: str) -> str:
    """城际方式中文名（train/air/driving；其它原样返回）。"""
    return _MODE_TEXT.get(mode, mode)


@dataclass(frozen=True)
class CityTravelEdge:
    origin: str
    destination: str
    transport_minutes: int
    mode: str = "城际交通"
    cost_per_person: float = 0.0     # 人均费用（元；估算/真源票价口径）
    from_station: str = ""           # 出发站（train/air；driving 无站点为空）
    to_station: str = ""             # 到达站（train/air）
    source: str = ""                 # "estimate"（估算表）| "live"（真源）| ""（假表旧边）


def _load_raw_edges(path: Optional[Any] = None) -> list:
    """读表原始 ``edges`` 列表；文件缺失 / 损坏 → []（不报错）。"""
    travel_path = Path(path) if path is not None else DEFAULT_CITY_TRAVEL_PATH
    if not travel_path.exists():
        return []
    try:
        data = json.loads(travel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("edges", []) or []


def _edge_from_raw(raw: Dict[str, Any]) -> CityTravelEdge:
    """旧表顶层字段 → Edge（mode 为顶层方式名，如「高铁」）。"""
    return CityTravelEdge(
        origin=raw["origin"],
        destination=raw["destination"],
        transport_minutes=int(raw["transport_minutes"]),
        mode=raw.get("mode", "城际交通"),
    )


def _edge_from_option(
    opt: Dict[str, Any],
    origin: str,
    destination: str,
    mode: str,
    default_edge: CityTravelEdge,
) -> CityTravelEdge:
    """options 条目 → Edge（缺字段回退默认边，保持旧读法兼容）。"""
    return CityTravelEdge(
        origin=origin,
        destination=destination,
        transport_minutes=int(
            opt.get("transport_minutes") or default_edge.transport_minutes
        ),
        mode=mode,
        cost_per_person=float(opt.get("cost_per_person") or 0.0),
        from_station=str(opt.get("from_station") or ""),
        to_station=str(opt.get("to_station") or ""),
        source=str(opt.get("source") or "estimate"),
    )


def load_city_travel_options(
    path: Optional[Any] = None,
) -> Dict[Tuple[str, str], Dict[str, CityTravelEdge]]:
    """完整 options 表：``{(origin, dest): {mode: CityTravelEdge}}``。

    旧表（无 ``options``）合成单元素 dict（顶层字段）；文件缺失返回空表。
    """
    out: Dict[Tuple[str, str], Dict[str, CityTravelEdge]] = {}
    for raw in _load_raw_edges(path):
        origin, destination = raw.get("origin", ""), raw.get("destination", "")
        if not origin or not destination:
            continue
        options = raw.get("options") or []
        by_mode: Dict[str, CityTravelEdge] = {}
        default_edge = _edge_from_raw(raw)
        if options:
            for opt in options:
                mode = str(opt.get("mode") or "").strip()
                if not mode:
                    continue
                by_mode[mode] = _edge_from_option(
                    opt, origin, destination, mode, default_edge
                )
        else:
            by_mode[default_edge.mode] = default_edge
        out[(origin, destination)] = by_mode
    return out


def load_city_travel_edges(
    path: Optional[Any] = None,
) -> Dict[Tuple[str, str], CityTravelEdge]:
    """默认边（options 的 train 条目优先，其次任意首条目；旧表用顶层字段）。

    返回 ``{(origin, destination): CityTravelEdge}``——兼容旧调用方
    （``find_city_travel`` 等）；文件缺失返回空表。
    """
    edges: Dict[Tuple[str, str], CityTravelEdge] = {}
    for key, by_mode in load_city_travel_options(path).items():
        first = by_mode.get("train") or next(iter(by_mode.values()), None)
        if first is not None:
            edges[key] = first
    return edges


def find_city_travel(
    origin: str,
    destination: str,
    edges: Optional[Dict[Tuple[str, str], CityTravelEdge]] = None,
    provider: Optional[Callable[[str, str], Optional[CityTravelEdge]]] = None,
) -> Optional[CityTravelEdge]:
    """查 (origin, destination) 的默认城际边；无此边返回 None。

    ``provider``：``fn(origin, dest) -> Optional[CityTravelEdge]``——真实数据接入时由
    ``data_transmission.live_data.make_live_city_travel_provider`` 注入，给定时优先于
    本地表（查不到再回落本地表，保持缺边语义一致）。
    """
    if provider is not None:
        edge = provider(origin, destination)
        if edge is not None:
            return edge
    edges = edges if edges is not None else load_city_travel_edges()
    return edges.get((origin, destination))


def find_city_travel_for_mode(
    origin: str,
    destination: str,
    mode: str,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
) -> Optional[CityTravelEdge]:
    """按指定方式查本地表 options 条目；无此边 / 无该方式 → None。"""
    options = options if options is not None else load_city_travel_options()
    by_mode = options.get((origin, destination)) or {}
    return by_mode.get(mode)


def find_city_travel_preferred(
    origin: str,
    destination: str,
    modes: Tuple[str, ...] = MODE_PREFERENCE,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
    provider: Optional[Callable[[str, str], Optional[CityTravelEdge]]] = None,
) -> Optional[CityTravelEdge]:
    """按偏好链逐个方式查，返回第一个命中（先定城际方式）。

    - ``provider`` 非空：直接单调用（live map 工具内部自带
      train→表外→driving 真源的降级，无需 A 侧重试链）；
    - 否则本地 options：高铁 → 飞机 → 自驾 逐个方式查。
    """
    if provider is not None:
        return provider(origin, destination)
    options = options if options is not None else load_city_travel_options()
    by_mode = options.get((origin, destination)) or {}
    for mode in modes:
        if mode in by_mode:
            return by_mode[mode]
    return None


# ---------------------------------------------------------------------------
# 区域模板（多式联运，批次 2.5）：分区 → 多枢纽 → 天津→张掖 空铁联运
# ---------------------------------------------------------------------------

AIR_BUFFER_MIN = 60              # 每段 air 的值机缓冲（计入联运总时长）
DEFAULT_MAX_TOTAL_MINUTES = 720  # 单程 12h 硬约束（uncompleted_list 一-1）


@dataclass(frozen=True)
class IntercityRoute:
    """多段联运链（本地直达 或 区域模板候选拼接）。

    ``total_minutes`` = Σ段净时长 + Σ air 段缓冲（值机），即真实行程总时长；
    ``total_cost`` = Σ 各段人均费用。
    """
    edges: Tuple[CityTravelEdge, ...]
    total_minutes: int
    total_cost: float

    @property
    def is_chain(self) -> bool:
        """是否多段联运（>1 段）。"""
        return len(self.edges) > 1


def load_regions(path: Optional[Any] = None) -> list:
    """区域表 ``[{name, hubs, members}]``；文件缺失 / 无 regions → []。"""
    travel_path = Path(path) if path is not None else DEFAULT_CITY_TRAVEL_PATH
    if not travel_path.exists():
        return []
    try:
        data = json.loads(travel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("regions", []) or []


def find_region_of(
    city: str, regions: Optional[list] = None
) -> Optional[dict]:
    """城市所在区域（hub 或 member 命中）；不在任何区域 → None。

    ``regions=None`` → 读默认表：便于调用方一次加载复用。
    """
    regions = regions if regions is not None else load_regions()
    for region in regions:
        if city in (region.get("hubs") or []) or city in (region.get("members") or []):
            return region
    return None


def _pick_segment(
    a: str,
    b: str,
    options: Dict[Tuple[str, str], Dict[str, CityTravelEdge]],
    prefer: str = "rail",
) -> Optional[CityTravelEdge]:
    """a→b 偏好段：在 ``prefer`` 允许的交通方式内取**时长最短**（踩中最优）。

    - ``prefer="air"``（区域间干线）：候选 air + train 取短；
    - ``prefer="rail"``（区域内接驳）：候选 train + air 取短；
    两者都无 → driving 兜底；仍无 → None。
    """
    if a == b:
        return None
    modes = ("air", "train") if prefer == "air" else ("train", "air")
    by_mode = options.get((a, b)) or {}
    candidates = [by_mode[m] for m in modes if m in by_mode]
    if not candidates:
        driving = by_mode.get("driving")
        return driving if driving is not None else None
    return min(candidates, key=lambda e: e.transport_minutes)


def _route_minutes(edges: Tuple[CityTravelEdge, ...]) -> int:
    """段净时长 + Σ air 值机缓冲。"""
    return sum(e.transport_minutes for e in edges) + sum(
        AIR_BUFFER_MIN for e in edges if e.mode == "air"
    )


def find_intercity_route_template(
    origin: str,
    destination: str,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
) -> Optional[IntercityRoute]:
    """区域模板候选枚举（不查直达，供直达失败/超时后调用）：

    出发区枢纽 × 目标区枢纽（含「本区枢纽 → 目标区成员直飞」特例），枚举全部
    ≤2 跳链，取总时长（含 air 缓冲）最短且 ≤ ``max_total_minutes``；无候选 → None。

    例如 天津→张掖（华北[北京] → 西北[西安/兰州/乌鲁木齐]）：
    - 北京→张掖 直飞（大兴→甘州）→ 链 [天津→北京, 北京→张掖] 约 240min ★
    - 经兰州 → [天津→北京, 北京→兰州, 兰州→张掖] 约 385min
    取最短 → 踩中「天津→大兴→直飞张掖」的最优策略（多枢纽价值所在）。
    """
    if not origin or not destination or origin == destination:
        return None
    options = options if options is not None else load_city_travel_options()
    origin_region = find_region_of(origin)
    dest_region = find_region_of(destination)
    if origin_region is None or dest_region is None:
        return None

    candidates: List[IntercityRoute] = []
    seen = set()
    hubs1 = origin_region.get("hubs") or []
    hubs2 = dest_region.get("hubs") or []

    def add(chain: Tuple[CityTravelEdge, ...]) -> None:
        if not chain:
            return
        key = tuple((e.origin, e.destination, e.mode) for e in chain)
        if key in seen:
            return
        seen.add(key)
        minutes = _route_minutes(chain)
        if minutes <= max_total_minutes:
            candidates.append(
                IntercityRoute(chain, minutes, sum(e.cost_per_person for e in chain))
            )

    for h1 in hubs1:
        seg1 = None if origin == h1 else _pick_segment(origin, h1, options, "rail")
        if origin != h1 and seg1 is None:
            continue  # 到不了本区枢纽 → 该 h1 无候选
        head = (seg1,) if seg1 is not None else ()
        # 特例：本区枢纽 → 目标区成员直飞（如 去程 天津→张掖：北京→张掖 大兴→甘州）
        if destination != h1:
            seg_direct = _pick_segment(h1, destination, options, "air")
            if seg_direct is not None:
                add(head + (seg_direct,))
        # 经目标区枢纽：h1 → h2 → destination
        for h2 in hubs2:
            if h2 == h1 or h2 == destination:
                continue
            mid = _pick_segment(h1, h2, options, "air")
            tail = _pick_segment(h2, destination, options, "rail")
            if mid is None or tail is None:
                continue
            add(head + (mid, tail))
    # 特例（对称）：成员直连目标区枢纽（如 返程 张掖→天津：张掖→北京 直飞 145m
    # 优于 张掖→兰州→北京 385m——h1 循环只枚举「本区枢纽出发」，此处补 origin 直连）
    if origin not in hubs1 and destination not in hubs2:
        for h2 in hubs2:
            seg0 = _pick_segment(origin, h2, options, "air")
            tail0 = _pick_segment(h2, destination, options, "rail")
            if seg0 is None or tail0 is None:
                continue
            add((seg0, tail0))

    if not candidates:
        return None
    return min(candidates, key=lambda r: r.total_minutes)


def find_intercity_route(
    origin: str,
    destination: str,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
) -> Optional[IntercityRoute]:
    """多式联运总入口：本地直达（≤12h）优先 → 区域模板 → None。

    注意：provider（live）场景直达由调用方先行权衡（表外 driving 兜底超 12h 时
    才回落本函数），本函数只消费本地表。
    """
    options = options if options is not None else load_city_travel_options()
    direct = find_city_travel_preferred(origin, destination, options=options)
    if direct is not None and direct.transport_minutes <= max_total_minutes:
        return IntercityRoute((direct,), direct.transport_minutes, direct.cost_per_person)
    template = find_intercity_route_template(
        origin, destination, max_total_minutes=max_total_minutes, options=options
    )
    if template is not None:
        return template
    if direct is not None:
        # 直达超 12h（罕见）：仍如实给出（source 标注后由外层兜底语义处理）
        return IntercityRoute((direct,), direct.transport_minutes, direct.cost_per_person)
    return None