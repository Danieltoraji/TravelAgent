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
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

DEFAULT_CITY_TRAVEL_PATH = (
    Path(__file__).resolve().parent.parent / "fake_spots" / "city_travel.json"
)

# 城际方式偏好（来去程段生成时的选择顺序：高铁优先，逐项回退到命中为止）
MODE_PREFERENCE: Tuple[str, ...] = ("train", "air", "driving")
# C 端四维偏好（travel_priority）对应的模式优先链（「优先」= 链式命中，非取最短）：
MODE_PRIORITY_RAIL: Tuple[str, ...] = ("train", "air", "driving")   # 高铁优先（= 默认链）
MODE_PRIORITY_AIR: Tuple[str, ...] = ("air", "train", "driving")    # 飞机优先

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
    candidates: Tuple[Dict[str, Any], ...] = ()  # 真源候选列表（多条车次/航班 × 时刻 × 票价）；估算边恒为空


# 候选列表单条结构约定（真源 provider 填充，供展示/未来班次级优化用）：
#   train: {"code", "depart_time", "arrive_time", "duration", "price",
#           "seats": {...}, "from_station", "to_station"}
#   air:   {"flight_no", "airline", "depart_time", "arrive_time",
#           "duration_min", "price", "from_airport", "to_airport", "status"}


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
    provider: Optional[Callable[..., Optional[CityTravelEdge]]] = None,
    priority: Optional[str] = None,
) -> Optional[CityTravelEdge]:
    """按偏好选择城际单边方式（先定城际方式）。

    - ``provider`` 非空：先在本地估算表按 ``priority`` 选定方式（``_pick_local_edge``），
      再**以该方式调 provider**（``provider(origin, dest, mode=...)``，B 侧拿该模式
      估算/真源；表外自动降级 driving 真源）。本地表该城市对全无解 → 直接单调用
      provider 缺省 mode 兜底。注意：provider 不短路偏好——线上 cost/air/speed
      等均按偏好选定方式后再查；
    - 否则纯本地 options，按 ``priority``（C 端四维 + 省钱）决策，见 ``_pick_local_edge``。
    """
    options = options if options is not None else load_city_travel_options()
    if provider is not None:
        local = _pick_local_edge(origin, destination, modes, options, priority)
        if local is not None and local.mode:
            edge = provider(origin, destination, mode=local.mode)
            if edge is None:
                return local  # 真源查询失败 → 本地估算照常（不假装、不空段）
            if edge.transport_minutes and edge.transport_minutes > 0:
                # 真源有有效时长：价格缺失时用本地估算价兜底（预算五项口径要价格）
                if (
                    (not edge.cost_per_person or edge.cost_per_person <= 0)
                    and local.cost_per_person
                    and local.cost_per_person > 0
                ):
                    return replace(edge, cost_per_person=local.cost_per_person)
                return edge
            # 真源时长缺失/为 0（如 driving 真源对短途城际返回空）
            # → 回落本地估算条目（时长+价格都补齐）
            return local
        return provider(origin, destination)
    return _pick_local_edge(origin, destination, modes, options, priority)


def _pick_local_edge(
    origin: str,
    destination: str,
    modes: Tuple[str, ...],
    options: Dict[Tuple[str, str], Dict[str, CityTravelEdge]],
    priority: Optional[str],
) -> Optional[CityTravelEdge]:
    """本地估算表按 ``priority`` 决策单边方式（返回 Edge，不调 provider）：
    - ``"rail"`` 高铁优先：按 ``MODE_PRIORITY_RAIL``（train→air→driving）链式命中；
    - ``"air"`` 飞机优先：按 ``MODE_PRIORITY_AIR``（air→train→driving）链式命中；
    - ``"speed"`` 速度最快：所有可用方式里取**总耗时最短**
      （air 净时长 + 值机缓冲 60min 一并比较，公平起见）；
    - ``"earliest"`` 最早到达：估算表无班次，当前与 speed 同值（总耗时最短），
      真源班次化后（阶段三）按到达时刻选最早的班次组合；
    - ``"cost"`` 越省钱越好：有价格条目（``cost_per_person > 0``）里取人均费用最低，
      全部无价 → 回落 ``modes`` 偏好链；
    - ``None`` / 其它（含已移除的 comfort 兜底）：按 ``modes`` 偏好链逐个命中。
    """
    by_mode = options.get((origin, destination)) or {}
    if priority in ("rail", "air"):
        chain = MODE_PRIORITY_RAIL if priority == "rail" else MODE_PRIORITY_AIR
        for mode in chain:
            if mode in by_mode:
                return by_mode[mode]
        return None
    if priority in ("speed", "earliest"):
        accessible = list(by_mode.values())
        if not accessible:
            return None
        return min(
            accessible,
            key=lambda e: e.transport_minutes
            + (AIR_BUFFER_MIN if e.mode == "air" else 0),
        )
    if priority == "cost":
        priced = [
            e for e in by_mode.values() if e.cost_per_person and e.cost_per_person > 0
        ]
        if priced:
            return min(priced, key=lambda e: e.cost_per_person)
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
    priority: Optional[str] = None,
) -> Optional[CityTravelEdge]:
    """a→b 偏好段：默认（None）在 ``prefer`` 允许的方式内取**时长最短**（踩中最优）。

    - ``prefer="air"``（区域间干线）：候选 air + train 取短；
    - ``prefer="rail"``（区域内接驳）：候选 train + air 取短；
    - ``priority`` 覆盖（C 端四维偏好）：
      - ``"rail"`` 高铁优先 / ``"air"`` 飞机优先：按对应链**链式命中**
        （有该模式就用，即使更慢/更贵——「优先」语义）；
      - ``"speed"`` / ``"earliest"``：候选里取总耗时最短（air 含值机缓冲）；
      - ``"cost"``：候选里取**人均费用最低**（0 价/无价条目视为不可比，回落时长最短）；
    - 候选为空 → driving 兜底；仍无 → None。
    """
    if a == b:
        return None
    if priority == "rail":
        chain = MODE_PRIORITY_RAIL
    elif priority == "air":
        chain = MODE_PRIORITY_AIR
    else:
        chain = ("air", "train") if prefer == "air" else ("train", "air")
    by_mode = options.get((a, b)) or {}
    candidates = [by_mode[m] for m in chain if m in by_mode]
    if not candidates:
        driving = by_mode.get("driving")
        return driving if driving is not None else None
    if priority in ("rail", "air"):
        # 模式优先 = 链式命中（train 或 air 存在即用，不做最短比较）
        return candidates[0]
    if priority == "cost":
        priced = [e for e in candidates if e.cost_per_person and e.cost_per_person > 0]
        if priced:
            return min(priced, key=lambda e: e.cost_per_person)
    if priority in ("speed", "earliest"):
        return min(
            candidates,
            key=lambda e: e.transport_minutes
            + (AIR_BUFFER_MIN if e.mode == "air" else 0),
        )
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
    priority: Optional[str] = None,
) -> Optional[IntercityRoute]:
    """区域模板候选枚举（批次 2.5 实现；**8.29 起主入口已改 BFS 全图**，
    本函数与 ``load_regions``/``find_region_of`` 一并保留兼容，不再被调用）。

    只枚举「出发区枢纽 × 目标区枢纽（含直飞特例）」≤2 跳链——依赖 regions 表、
    非枢纽成员中转/跨区小众链会漏，故被 BFS+剪枝 取代。行为见旧文档记录：

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
        seg1 = None if origin == h1 else _pick_segment(origin, h1, options, "rail", priority)
        if origin != h1 and seg1 is None:
            continue  # 到不了本区枢纽 → 该 h1 无候选
        head = (seg1,) if seg1 is not None else ()
        # 特例：本区枢纽 → 目标区成员直飞（如 去程 天津→张掖：北京→张掖 大兴→甘州）
        if destination != h1:
            seg_direct = _pick_segment(h1, destination, options, "air", priority)
            if seg_direct is not None:
                add(head + (seg_direct,))
        # 经目标区枢纽：h1 → h2 → destination
        for h2 in hubs2:
            if h2 == h1 or h2 == destination:
                continue
            mid = _pick_segment(h1, h2, options, "air", priority)
            tail = _pick_segment(h2, destination, options, "rail", priority)
            if mid is None or tail is None:
                continue
            add(head + (mid, tail))
    # 特例（对称）：成员直连目标区枢纽（如 返程 张掖→天津：张掖→北京 直飞 145m
    # 优于 张掖→兰州→北京 385m——h1 循环只枚举「本区枢纽出发」，此处补 origin 直连）
    if origin not in hubs1 and destination not in hubs2:
        for h2 in hubs2:
            seg0 = _pick_segment(origin, h2, options, "air", priority)
            tail0 = _pick_segment(h2, destination, options, "rail", priority)
            if seg0 is None or tail0 is None:
                continue
            add((seg0, tail0))

    if not candidates:
        return None
    return min(candidates, key=lambda r: r.total_minutes)


# ---------------------------------------------------------------------------
# BFS + 剪枝 全图搜索（8.29 起，替代区域模板候选枚举；template 函数保留兼容）
# ---------------------------------------------------------------------------

DEFAULT_MAX_HOPS = 3  # 中转链最大跳数：3 段覆盖空铁联运全组合；4 段会明显绕路且被剪

INF = 10**9


def _segment_candidates(
    a: str,
    b: str,
    options: Dict[Tuple[str, str], Dict[str, CityTravelEdge]],
    priority: Optional[str],
) -> List[CityTravelEdge]:
    """a→b 在 ``priority`` 语义下的候选边集（BFS 扩展用，可多条）。

    段内决策**复用旧模板 ``_pick_segment`` 语义**（保证 2.5 金标准不变）：
    - ``rail`` / ``air``：链式命中（对应优先链首个可用模式）；
    - ``None``：**偏好链内取最短**（如 昆明→成都 air 80m 而非 train 300m）；
    - ``cost`` / ``speed`` / ``earliest``：**全展开** —— BFS 升级点：不止段内贪心，
      更在**整条链上求全局最优**（全局最低价 / 含 air 缓冲的最短总时长）。
    """
    if priority in ("speed", "earliest"):
        return list((options.get((a, b)) or {}).values())
    if priority == "cost":
        by_mode = options.get((a, b)) or {}
        priced = [
            e for e in by_mode.values() if e.cost_per_person and e.cost_per_person > 0
        ]
        if priced:
            return priced  # 全局最低价链（多边竞争）
        # 全无价 → 偏好链兜底（与 _pick_segment cost 回落一致）
        edge = _pick_segment(a, b, options, "rail", priority)
        return [edge] if edge is not None else []
    edge = _pick_segment(a, b, options, "rail", priority)
    return [edge] if edge is not None else []


def _edge_weight(edge: CityTravelEdge) -> int:
    """单段的「真实到达时长」：净时长 + air 值机缓冲（与 ``_route_minutes`` 口径一致）。"""
    return edge.transport_minutes + (AIR_BUFFER_MIN if edge.mode == "air" else 0)


def find_intercity_route_bfs(
    origin: str,
    destination: str,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
    priority: Optional[str] = None,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> Optional[IntercityRoute]:
    """多式联运全图 BFS + 剪枝（软直达优先）。

    邻接按 ``priority`` 决定每边的候选集（``_segment_candidates``：rail/air 链式 1 条、
    speed/cost 全候选展开），uniform-cost 搜索（heapq 按累计目标值升序）＝广度优先传播
    ＋ Dijkstra 最优性：**首达 destination 的路径即全局最优**，不会像区域模板那样
    只看「本区枢纽 × 目标区枢纽」漏掉小众中转链。

    剪枝（防搜索爆炸，17 城 58 边规模下状态量极小）：
    1. 跳数 > ``max_hops``（默认 3）→ 剪；
    2. 累计目标值 ≥ 当前最优（**分支限界**）→ 剪；
    3. 累计真实到达时长（含 air 缓冲）> ``max_total_minutes``（720 硬约束）→ 剪；
    4. 路径内城市已访问（**无环**）→ 剪；
    5. 到达某城市的目标值劣于已记录（**Dijkstra 节点去重**）→ 剪（爆炸的根剪枝）。

    软直达优先（8.29 决策）：直达（≤12h）存在 → 作为初始最优；BFS 只在发现
    「目标值更优」的多跳链时替换——有直达不绕路，但真正更优的小众链不遗漏。

    ``priority``：目标值口径——``cost`` 为人均费用累计，其余（rail/air/speed/earliest/
    None）为总到达时长（含 air 缓冲）；两种口径下 720 时长硬约束都照常生效。
    """
    if not origin or not destination or origin == destination:
        return None
    options = options if options is not None else load_city_travel_options()
    from heapq import heappop, heappush

    goal_cost = priority == "cost"

    # 软直达优先：直达存在且 ≤ max_total → 初始最优（多跳必须更优才替换）
    best_edges: Optional[Tuple[CityTravelEdge, ...]] = None
    best_value = None  # 目标值（费用 / 含缓冲时长）
    direct = find_city_travel_preferred(
        origin, destination, options=options, priority=priority
    )
    if direct is not None and _edge_weight(direct) <= max_total_minutes:
        best_value = (
            direct.cost_per_person if goal_cost else _edge_weight(direct)
        )
        best_edges = (direct,)

    # 邻接：每个 (a, b) 按 priority 展开候选边（邻接表，只构建一次）
    out: Dict[str, List[Tuple[str, CityTravelEdge]]] = {}
    for (a, b), by_mode in options.items():
        if not by_mode:
            continue
        for edge in _segment_candidates(a, b, options, priority):
            out.setdefault(a, []).append((b, edge))

    # heap: (目标值, 跳数, 当前城市, 路径边, 累计真实到达时长)
    heap: List[Tuple[int, int, str, Tuple[CityTravelEdge, ...], int]] = []
    best_to_city: Dict[str, int] = {}
    heappush(heap, (0, 0, origin, (), 0))

    while heap:
        value, hops, city, path, full_minutes = heappop(heap)
        if best_value is not None and value >= best_value:
            break  # 分支限界：堆顶已不优于当前最优 → 后面只会更差
        if city == destination and path:
            best_value, best_edges = value, path
            continue  # 可能还有同值/更优分支；堆序保证不会再差
        if hops >= max_hops:
            continue
        visited = {e.origin for e in path} | {city}
        for nxt, edge in out.get(city, ()):
            if nxt == destination and city == origin:
                continue  # 起点→终点 直达已由基准（软直达优先）决策，BFS 只搜中转链
            if nxt in visited:
                continue  # 无环
            n_full = full_minutes + _edge_weight(edge)
            if n_full > max_total_minutes:
                continue  # 720 硬约束（含 air 缓冲）
            n_value = value + (edge.cost_per_person if goal_cost else _edge_weight(edge))
            if best_value is not None and n_value >= best_value:
                continue  # 分支限界（到终点只会更差）
            new_path = path + (edge,)
            if nxt == destination:
                best_value, best_edges = n_value, new_path
                continue
            if n_value >= best_to_city.get(nxt, INF):
                continue  # Dijkstra 节点去重：已存在更优路径到该城
            best_to_city[nxt] = n_value
            heappush(heap, (n_value, hops + 1, nxt, new_path, n_full))

    if best_edges is None:
        return None
    return IntercityRoute(best_edges, _route_minutes(best_edges),
                          sum(e.cost_per_person for e in best_edges))


def find_intercity_route(
    origin: str,
    destination: str,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
    priority: Optional[str] = None,
    max_hops: int = DEFAULT_MAX_HOPS,
) -> Optional[IntercityRoute]:
    """多式联运总入口：全图 BFS + 剪枝（8.29 起，替代区域模板候选枚举）。

    ``priority``（rail/air/speed/earliest/cost/None）：直达与各段的模式/目标偏好，
    见 ``find_city_travel_preferred`` / ``find_intercity_route_bfs``。

    软直达优先：直达（≤12h）→ 初始最优，BFS 只在发现更优链时替换——有直达不绕路，
    更优的小众中转链不遗漏（解决方案：BFS 全图，不再依赖 regions 表）。

    注意：provider（live）场景直达由调用方先行权衡（表外 driving 兜底超 12h 时
    才回落本函数），本函数只消费本地表。
    """
    if not origin or not destination or origin == destination:
        return None
    options = options if options is not None else load_city_travel_options()
    route = find_intercity_route_bfs(
        origin, destination, max_total_minutes=max_total_minutes,
        options=options, priority=priority, max_hops=max_hops,
    )
    if route is not None:
        return route
    # 兜底（与旧行为一致）：直达存在但超 12h（罕见）→ 仍如实给出
    direct = find_city_travel_preferred(
        origin, destination, options=options, priority=priority
    )
    if direct is not None:
        return IntercityRoute(
            (direct,), direct.transport_minutes, direct.cost_per_person
        )
    return None