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
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

from data_transmission.air_routes import AirRoutes, load_air_routes
from data_transmission.city_travel import (
    AIR_BUFFER_MIN,
    DEFAULT_MAX_TOTAL_MINUTES,
    CityTravelEdge,
    IntercityRoute,
)

logger = logging.getLogger("data_transmission.intercity_candidates")

# 每侧进入铁路查询的航空邻居上限（§4.3：每侧 10~20，取下限保守）
MAX_NEIGHBORS = 10
# 单次候选生成的铁路查询总量保护（延迟口径，§4.3）
MAX_TRAIN_CALLS = 12
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

    # 类型 C：飞机→火车  AirOut(O) 的每个邻居 C 查 Train(C, D)
    for city in routes.out_cities(origin, date_str)[:MAX_NEIGHBORS]:
        if city == destination:
            continue
        tail = _train(city, destination)
        if tail is None:
            continue  # 铁路过滤不通过 → 候选不成立（§4.2 类型 C 纪律）
        hint = routes.hint(origin, city, date_str)
        if hint is None:
            continue
        _add((_air_edge(hint), tail))

    # 类型 D：火车→飞机  AirIn(D) 的每个邻居 C 查 Train(O, C)
    for city in routes.in_cities(destination, date_str)[:MAX_NEIGHBORS]:
        if city == origin or city == destination:
            continue
        head = _train(origin, city)
        if head is None:
            continue
        hint = routes.hint(city, destination, date_str)
        if hint is None:
            continue
        _add((head, _air_edge(hint)))

    # 排序：cost 口径按总费用（无价格航段的候选排后），其余按总时长
    if priority == "cost":
        candidates.sort(key=lambda r: (r.total_cost if r.total_cost > 0 else 1e9, r.total_minutes))
    else:
        candidates.sort(key=lambda r: r.total_minutes)
    return candidates
