"""空铁联运候选生成器（城际交通分阶段优化方案 §4.2，Day 1~2 最小切片）。

替代旧「58 边估算表 BFS」的候选发现机制（方案 §2.2/§4.9：不再在邻接展开
阶段调真源、不靠估算表圈死探索范围）：

- **类型 A 直达铁路**：``Train(O, D)`` 免费直查一次；
- **类型 C 飞机→火车**：``for C in AirOut(O): Train(C, D)``——航空拓扑正向
  邻居 + 免费铁路过滤；
- **类型 D 火车→飞机**：``for C in AirIn(D): Train(O, C)``——反向邻居同理
  （锦州→张掖走这条：AirIn(张掖) 含北京，Train(锦州, 北京) 有高铁）；
- 类型 B 直达航空与类型 E（铁→飞→铁）由调用方（``_resolve_intercity_route``
  的直达判断 / 后续 Day 3 Top-K）处理，本模块只产联运候选。

数据口径（方案 §3.x/§4.6）：航空边是**拓扑提示**（AirRouteHint，班期过滤后
仅有典型时长，无价格无时刻）→ 段级 ``estimated``；铁路边来自免费真源查询
（train_trip provider，有真实时长/价格）→ 段级 ``live``。候选整链即
``mixed``。付费航班验证（Day 3）不在本切片内。

铁路查询纪律（§3.2/§4.3）：同一 (o, d, date) 只查一次（缓存正负结果）；
每侧邻居最多 ``MAX_NEIGHBORS`` 个进入铁路查询（拓扑先按班期筛）；总量
延迟保护 ``MAX_TRAIN_CALLS``。

类型 C/D 的邻居枚举上限与铁路预算（8.31 贵港→北京 修复）：北京类枢纽的
``AirIn`` 邻居达 33 个，南宁这类「小城坐高铁 44min 即可达」的邻居排位靠后
（第 17 位）会被旧 ``MAX_NEIGHBORS=10`` 直接截断；且前序无车邻居会吃掉
``MAX_TRAIN_CALLS=12`` 预算，轮到南宁时已无查询额度 → 贵港→北京 候选
恒为空、退化自驾 33h。修复：邻居上限与铁路预算同步放宽到 24，且类型 C/D
改为**先收集铁路命中邻居、按铁路时长升序生成候选**（铁路可达优先：
贵港→南宁 44min 必然排在无车邻居之前出链，枢纽场景受益）。

Day 3 提前（8.30 拍板，用户要求真价提前）：``verify_flight_legs`` 对入围
候选的**航段城市对**调 juhe（flight_search，付费）拿真实价格/时刻——
真价覆盖拓扑提示（段升级为 live），无航班/失败保持 estimated 不阻断。
额度纪律（§4.4）：验证的城市对 ≤ ``MAX_FLIGHT_VERIFIES``（默认 4），
同对去重缓存；无价航班（ticketPrice=0）如实保持 0（juhe 部分航线无价，
预算口径注明低估）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from data_transmission.air_routes import AirRouteHint, AirRoutes, load_air_routes
from data_transmission.city_travel import (
    AIR_BUFFER_MIN,
    DEFAULT_MAX_TOTAL_MINUTES,
    CityTravelEdge,
    IntercityRoute,
)

logger = logging.getLogger("data_transmission.intercity_candidates")

# 每侧进入铁路查询的航空邻居上限（§4.3 每侧 10~20；8.31 贵港→北京 起
# 放宽到 24——北京类枢纽 AirIn 邻居 33 个，南宁排第 17，旧 10 直接截断）
MAX_NEIGHBORS = 24
# 单次候选生成的铁路查询总量保护（延迟口径，§4.3；12→24 见 docstring 修复说明）
MAX_TRAIN_CALLS = 24
# 班期/有效期外的邻居直接跳过（§4.3 先按班期筛）


def generate_intercity_candidates(
    origin: str,
    destination: str,
    date_str: str = "",
    train_provider: Optional[Callable[[str, str], Optional[CityTravelEdge]]] = None,
    air_routes: Optional[AirRoutes] = None,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
    priority: Optional[str] = None,
) -> List[IntercityRoute]:
    """生成空铁联运候选（类型 A/C/D），按总时长升序返回。

    ``train_provider``：``fn(a, b) -> Optional[CityTravelEdge]``——免费铁路
    真源（B 侧组合 provider 的 train 分支）；None → 跳过所有铁路查询
    （纯拓扑场景，仅类型 C/D 的拓扑骨架不产出——铁路段无法成立时联运链
    没有 live 依据，宁缺毋滥）。

    ``air_routes``：航空拓扑；缺省加载 ``air_routes.json``。

    返回的每条 ``IntercityRoute``：
    - 段级 source：铁路段 ``live``（真源）、航段 ``estimated``（拓扑提示）；
    - ``total_minutes`` 含 air 段值机缓冲（与既有 IntercityRoute 口径一致）；
    - 排序：``speed``/缺省按总时长；``cost`` 按总费用（航段无价格时排最后）。
    """
    if not origin or not destination or origin == destination:
        return []
    routes = air_routes if air_routes is not None else load_air_routes()
    candidates: List[IntercityRoute] = []
    train_cache: Dict[Tuple[str, str], Optional[CityTravelEdge]] = {}
    train_calls = 0

    def _train(a: str, b: str) -> Optional[CityTravelEdge]:
        """免费铁路查询：同对缓存（正负都缓存）+ 总量保护。"""
        nonlocal train_calls
        key = (a, b)
        if key in train_cache:
            return train_cache[key]
        if train_calls >= MAX_TRAIN_CALLS or train_provider is None:
            return None
        train_calls += 1
        try:
            edge = train_provider(a, b)
        except Exception as exc:  # noqa: BLE001  单段失败不炸整批候选
            logger.warning("铁路查询失败（%s→%s）：%s", a, b, exc)
            edge = None
        if edge is not None and edge.mode not in ("train", "rail"):
            # 组合 provider 的 train 分支在无车次时会落出 driving 估算边——
            # 那不是铁路结果（类型 A/C/D 都以「铁路段成立」为前提），按无车处理。
            edge = None
        train_cache[key] = edge
        return edge

    def _air_edge(hint: Any) -> CityTravelEdge:
        """拓扑航段 → estimated 边（典型时长 + 值机缓冲在外层计）。"""
        return CityTravelEdge(
            origin=hint.origin_city,
            destination=hint.destination_city,
            transport_minutes=int(hint.typical_duration_min or 0),
            mode="air",
            cost_per_person=0.0,  # 拓扑无价格（§3.1）；Top-K 验证后补
            from_station=hint.origin_airport,
            to_station=hint.destination_airport,
            source="estimated",
        )

    def _route_minutes(edges: Tuple[CityTravelEdge, ...]) -> int:
        total = 0
        for e in edges:
            total += e.transport_minutes
            if e.mode == "air":
                total += AIR_BUFFER_MIN
        return total

    def _add(edges: Tuple[CityTravelEdge, ...]) -> None:
        if not edges:
            return
        minutes = _route_minutes(edges)
        if minutes > max_total_minutes or minutes <= 0:
            return
        candidates.append(
            IntercityRoute(edges, minutes, sum(e.cost_per_person for e in edges))
        )

    # 类型 A：直达铁路（免费直查一次）
    direct_train = _train(origin, destination)
    if direct_train is not None:
        _add((direct_train,))

    # 类型 C：飞机→火车  AirOut(O) 的每个邻居 C 查 Train(C, D)。
    # 8.31 修复（铁路可达优先）：先把邻居查过铁路的命中（tail 非 None）收集
    # 起来，按铁路时长升序生成候选——「C 坐高铁多久能到 D」决定出链顺序，
    # 让可达优先的邻居尽早进入候选，枢纽长尾邻居不再靠数据文件顺序排位。
    rail_hits_c: List[Tuple[int, CityTravelEdge, AirRouteHint]] = []
    for city in routes.out_cities(origin, date_str)[:MAX_NEIGHBORS]:
        if city == destination:
            continue
        tail = _train(city, destination)
        if tail is None:
            continue  # 铁路过滤不通过 → 候选不成立（§4.2 类型 C 纪律）
        hint = routes.hint(origin, city, date_str)
        if hint is not None:
            rail_hits_c.append((tail.transport_minutes, tail, hint))
    for _mins, tail, hint in sorted(rail_hits_c, key=lambda x: x[0]):
        _add((_air_edge(hint), tail))

    # 类型 D：火车→飞机  AirIn(D) 的每个邻居 C 查 Train(O, C)。
    # 同样铁路可达优先（8.31 贵港→北京 修复核心）：贵港→南宁 高铁 44min
    # 这类「小城 → 就近枢纽」的邻居即使排位靠后（北京 AirIn 第 17 位）也
    # 会因铁路命中而排前出链；无车邻居不参与候选。
    rail_hits_d: List[Tuple[int, CityTravelEdge, AirRouteHint]] = []
    for city in routes.in_cities(destination, date_str)[:MAX_NEIGHBORS]:
        if city == origin or city == destination:
            continue
        head = _train(origin, city)
        if head is None:
            continue
        hint = routes.hint(city, destination, date_str)
        if hint is not None:
            rail_hits_d.append((head.transport_minutes, head, hint))
    for _mins, head, hint in sorted(rail_hits_d, key=lambda x: x[0]):
        _add((head, _air_edge(hint)))

    # 排序：cost 口径按总费用（无价格航段的候选排后），其余按总时长
    if priority == "cost":
        candidates.sort(key=lambda r: (r.total_cost if r.total_cost > 0 else 1e9, r.total_minutes))
    else:
        candidates.sort(key=lambda r: r.total_minutes)
    return candidates


# ---------------------------------------------------------------------------
# Day 3 提前（8.30）：Top-K 航段真价验证（juhe 付费，额度 ≤4 城市对/规划）
# ---------------------------------------------------------------------------

# 验证的城市对上限（§4.4 默认 ≤4；去重后计——同一城市对出现在多条候选只查一次）
MAX_FLIGHT_VERIFIES = 4


def verify_flight_legs(
    routes: List[IntercityRoute],
    flight_provider: Callable[[str, str], Optional[CityTravelEdge]],
    top_k: int = 2,
) -> List[IntercityRoute]:
    """对前 ``top_k`` 条候选的航段城市对做 juhe 真价验证，返回更新后的列表。

    - 城市对去重缓存 + 总量 ≤ ``MAX_FLIGHT_VERIFIES``（额度纪律 §4.4）；
    - 真源命中 → 航段替换为真价边（cost/时刻/duration/source=live，
      全量航班在 ``candidates`` 字段透传），``total_minutes/total_cost`` 重算；
    - 真源 None → 航段保持 estimated（**不淘汰**：None 语义过载——
      无航班 / provider 未实现 air 分支 / 额度尽，无法区分，误杀代价大于
      保留代价；明确证伪淘汰留给 Day 4 接续校验，届时以具体班次空列表
      为准）；
    - **回落边防护（8.31 贵港→北京 实测）**：B 侧组合 provider 的 air
      分支在 flight 查询无果时回落 map **估算边**（mode != "air"，如
      driving 1389m）——那不是航段真价，若替换会把 292m 联运链降级成
      1431m driving。非 air 的返回值一律视为「未命中」，保持 estimated
      不升级不淘汰；
    - 查询异常 → 保持 estimated 档（工具故障 ≠ 航段不存在，不误杀）；
    - 无航段的候选（纯铁路直达）原样返回（零 juhe 消耗）。

    ``flight_provider``：``fn(a, b) -> Optional[CityTravelEdge]``（B 侧组合
    provider 的 air 分支或 make_live_flight_provider 产物）。
    """
    air_cache: Dict[Tuple[str, str], Optional[CityTravelEdge]] = {}
    verified = 0

    def _air(a: str, b: str) -> Optional[CityTravelEdge]:
        nonlocal verified
        key = (a, b)
        if key in air_cache:
            return air_cache[key]
        if verified >= MAX_FLIGHT_VERIFIES:
            return None  # 额度尽：未验证航段保持 estimated（调用方不淘汰）
        verified += 1
        try:
            edge = flight_provider(a, b)
        except Exception as exc:  # noqa: BLE001  工具故障不误杀候选
            logger.warning("航段真价验证失败（%s→%s），保持 estimated：%s", a, b, exc)
            edge = None
            air_cache[key] = None
            return None  # 异常语义：不可判 → 不升级也不淘汰
        air_cache[key] = edge
        return edge

    result: List[IntercityRoute] = []
    for index, route in enumerate(routes):
        if index >= top_k or not any(e.mode == "air" for e in route.edges):
            result.append(route)  # top 之外 / 纯铁路：原样
            continue
        new_edges: List[CityTravelEdge] = []
        upgraded = False
        for edge in route.edges:
            if edge.mode != "air" or edge.source == "live":
                new_edges.append(edge)
                continue
            live_edge = _air(edge.origin, edge.destination)
            if live_edge is None or live_edge.mode != "air":
                # None 语义过载 + 回落边防护（8.31 贵港→北京）：B 侧组合
                # provider 的 air 分支在 flight 无果时会回落 map 估算边
                # （mode != "air"，driving 1389m）——不是航段真价，保持
                # estimated 不升级不淘汰（误杀/降级代价 > 保留代价）。
                new_edges.append(edge)
                continue
            new_edges.append(live_edge)
            upgraded = True
        if upgraded:
            minutes = 0
            for e in new_edges:
                minutes += e.transport_minutes
                if e.mode == "air":
                    minutes += AIR_BUFFER_MIN
            result.append(IntercityRoute(
                tuple(new_edges), minutes,
                sum(e.cost_per_person for e in new_edges),
            ))
        else:
            result.append(route)
    return result
