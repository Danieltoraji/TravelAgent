"""Must-visit spot assignment across one or multiple days."""

from __future__ import annotations

import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms._common import (
    Spot,
    _budget_context,
    _duration,
    _include_meal_time_in_daily_limit,
    _parse_time,
    _spot_key,
)
from algorithoms.route_ordering import _spot_transport_minutes
from algorithoms.timeline import _build_schedule_events, _scheduled_elapsed_minutes
from data_transmission.city_graph import DEFAULT_GRAPH_DIR
from data_transmission.meal import DEFAULT_MEAL_WINDOWS, MealWindow
from transport.providers import JsonTravelTimeProvider, TravelTimeProvider

# 远郊判定半径（km，8.30 demo1）：距池内全部 must 景点质心超过该值视为远郊
# （张掖实测：市区簇质心→七彩丹霞 ~40km、山丹马场 ~65km、高台纪念馆 ~70km）。
# 远郊景点彼此再按该半径聚簇（丹霞+高台同在西线 ~35km，可同天顺路）。
REMOTE_RADIUS_KM = 25.0


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两坐标 (lat, lng) 球面直线距离（km）。"""
    lat1, lng1 = a
    lat2, lng2 = b
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(h))


def _spot_coord(spot: Spot) -> Optional[Tuple[float, float]]:
    """景点坐标 (lat, lng)；缺坐标 → None。"""
    location = spot.get("location")
    if isinstance(location, dict):
        lat, lng = location.get("lat"), location.get("lng")
        if lat is not None and lng is not None:
            try:
                return (float(lat), float(lng))
            except (TypeError, ValueError):
                return None
    return None


def _remote_groups(
    all_must_options: Dict[str, Spot],
) -> Tuple[Dict[str, int], List[List[str]]]:
    """远郊识别 + 聚簇（8.30 demo1：远郊必去必须同天或独占一天）。

    判定（**密度锚点法**，质心法在「远郊占多数」时会把市区也判成远郊——
    3 个远郊 + 1 个市区的质心被拉到市区外 30km，8.30 实测暴露）：
    1. 找**最大本地簇**：彼此 < ``REMOTE_RADIUS_KM`` 的最大景点集合，
       视为「市区锚」（must 列表通常是市区为主，锚点即市区）；
    2. 距市区锚 > 半径的景点为**远郊**；远郊之间再按同半径贪心聚簇
       （西线丹霞+高台一簇、山丹独立一簇）。

    返回 ``(spot_key → 簇号, 簇列表)``；市区景点不在映射里（不受约束）。
    缺坐标（假池无 location 等）→ 不识别（行为退化为原逻辑，保守不炸）。
    """
    keyed = {
        key: coord
        for key, spot in all_must_options.items()
        if (coord := _spot_coord(spot)) is not None
    }
    if len(keyed) < 2:
        return {}, []

    # 最大本地簇（贪心：按「半径内邻居数」降序，从邻居最多的点生长）。
    keys = list(keyed)
    best_anchor: List[str] = []
    for seed in keys:
        cluster = [
            key for key in keys
            if key == seed or _haversine_km(keyed[seed], keyed[key]) < REMOTE_RADIUS_KM
        ]
        if len(cluster) > len(best_anchor):
            best_anchor = cluster
    if not best_anchor:
        best_anchor = keys[:1]
    anchor_center = (
        sum(keyed[key][0] for key in best_anchor) / len(best_anchor),
        sum(keyed[key][1] for key in best_anchor) / len(best_anchor),
    )

    remote_keys = [
        key for key in keys
        if key not in best_anchor
        and _haversine_km(anchor_center, keyed[key]) > REMOTE_RADIUS_KM
    ]
    if not remote_keys:
        return {}, []
    # 远郊之间聚簇（贪心：与簇内任一成员 < 半径即并入）
    clusters: List[List[str]] = []
    for key in remote_keys:
        for cluster in clusters:
            if any(
                _haversine_km(keyed[key], keyed[member]) < REMOTE_RADIUS_KM
                for member in cluster
            ):
                cluster.append(key)
                break
        else:
            clusters.append([key])
    spot_cluster: Dict[str, int] = {}
    for index, cluster in enumerate(clusters):
        for key in cluster:
            spot_cluster[key] = index
    return spot_cluster, clusters


def _must_visit_tasks(must_spots: Sequence[Spot]) -> List[List[Spot]]:
    """Convert mandatory spots into fixed tasks and alternative groups."""
    fixed = {}
    alternatives: Dict[str, Dict[str, Spot]] = {}
    for spot in must_spots:
        key = _spot_key(spot)
        if spot.get("dependency"):
            group = str(
                spot.get("dependency_group")
                or spot.get("must_visit_source")
                or "模糊必去景点"
            )
            alternatives.setdefault(group, {})[key] = spot
        else:
            fixed[key] = spot
    tasks = [[spot] for spot in fixed.values()]
    tasks.extend(list(group.values()) for group in alternatives.values())
    return tasks


def _default_must_spots(must_spots: Sequence[Spot]) -> List[Spot]:
    """Resolve each alternative task to one representative for one-day plans."""
    selected = []
    for task in _must_visit_tasks(must_spots):
        selected.append(
            max(
                task,
                key=lambda spot: (
                    float(spot.get("match_score", 0)),
                    -_duration(spot),
                    _spot_key(spot),
                ),
            )
        )
    return selected


def assign_must_spots_to_days(
    requirement: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    day_start_time: str = "09:00",
    travel_time_provider: Optional[TravelTimeProvider] = None,
    meal_windows: Sequence[MealWindow] = DEFAULT_MEAL_WINDOWS,
    restaurants=None,
    beam_width: int = 200,
) -> Dict[str, Any]:
    """Allocate every confirmed must-visit attraction across multiple days.

    A beam search keeps several partial allocations instead of committing to
    the first greedy choice. Each attraction is tested in every position of
    every day, and only schedules within ``daily_travel_time`` survive.
    """
    try:
        content = requirement["content"]
        destination = content["destination"]
        day_count = int(content["days"])
        daily_limit = int(content["constraints"]["daily_travel_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(
            "requirement 缺少有效的 destination、days 或 daily_travel_time"
        ) from exc
    if day_count <= 0 or daily_limit <= 0:
        raise ValueError("days 和 daily_travel_time 必须大于 0")
    if beam_width <= 0:
        raise ValueError("beam_width 必须大于 0")
    if len(candidate_spots) != 3:
        raise ValueError("candidate_spots 必须是 [must_spots, conflict_spots, scored_spots]")

    include_meal_time = _include_meal_time_in_daily_limit(requirement)
    budget_limit, visitor_number = _budget_context(requirement)

    must_spots, conflict_spots, _ = candidate_spots
    must_tasks = _must_visit_tasks(must_spots)
    all_must_options = {
        _spot_key(spot): spot for task in must_tasks for spot in task
    }
    provider = travel_time_provider or JsonTravelTimeProvider(destination, graph_dir)
    matrix = provider.get_matrix(all_must_options.keys())
    day_start_minutes = _parse_time(day_start_time)

    total_available_minutes = day_count * daily_limit
    minimum_required_visit_minutes = sum(
        min(_duration(spot) for spot in task) for task in must_tasks
    )

    def elapsed(route: Sequence[Spot]) -> int:
        return _scheduled_elapsed_minutes(
            route,
            None,
            matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time,
            restaurants,
        )

    # Long and remote mandatory attractions are placed first because they have
    # fewer feasible insertion options later.
    def spot_difficulty(spot: Spot):
        others = [
            item
            for item in all_must_options.values()
            if _spot_key(item) != _spot_key(spot)
        ]
        average_transport = (
            sum(_spot_transport_minutes(spot, other, matrix) for other in others)
            / len(others)
            if others
            else 0
        )
        return (_duration(spot) + average_transport, _spot_key(spot))

    def task_difficulty(task: Sequence[Spot]):
        # A task with fewer/longer alternatives is harder to place.
        easiest_option = min(spot_difficulty(spot)[0] for spot in task)
        return (easiest_option, -len(task), sorted(_spot_key(spot) for spot in task))

    ordered_tasks = sorted(must_tasks, key=task_difficulty, reverse=True)
    # 远郊识别 + 聚簇（8.30 demo1）：远郊必去（距质心 > 25km）只允许与**同簇**
    # 远郊同天或独占一天——否则会出现「开 1 小时车去丹霞、玩 2 小时、开 1 小时
    # 车回市区继续逛」的孤岛日。西线（丹霞+高台 ~35km）聚一簇同天顺路，
    # 山丹独立成天。缺坐标/无远郊 → 空映射，行为与原逻辑完全一致。
    remote_cluster_of, remote_clusters = _remote_groups(all_must_options)

    # Each state is a tuple of ordered spot tuples, one tuple per day.
    states = [tuple(tuple() for _ in range(day_count))]
    assigned_count = 0
    failed_task = None

    def state_rank(state):
        loads = [elapsed(route) for route in state]
        non_empty_loads = [value for value in loads if value > 0]
        return (
            max(loads, default=0),
            sum(loads),
            max(non_empty_loads, default=0) - min(non_empty_loads, default=0),
        )

    def _remote_violated(option_key: str, day_route: Sequence[Spot]) -> bool:
        """远郊同天约束（双向，分级）：

        - 严格档（``cross_remote=False``）：远郊与市区必去不得同天，
          **异簇远郊也不得同天**——每个远郊簇独占一天（时间预算留给通勤）。
        - 放宽档（``cross_remote=True``）：仅禁止「市区×远郊混排」，异簇远郊
          允许拼天（如 3 天行程 4 个互斥景点时，丹霞日顺带高台——49km 间隔
          同天可接受）。严格档不可行才降级到放宽档（constraint-degradation
          纪律：删除/失败从来不是首选），降级时打 warning。

        单向检查会在 beam 按难度降序分配时漏判（山丹先占空天、市区后插入
        时不被拦——8.30 测试暴露）。option 和已有景点任一方为远郊即校验。
        """

        def _violated(strict: bool) -> bool:
            option_cluster = remote_cluster_of.get(option_key)
            for other in day_route:
                other_key = _spot_key(other)
                other_cluster = remote_cluster_of.get(other_key)
                if option_cluster is None and other_cluster is None:
                    continue  # 双方都是市区：不受约束
                if option_cluster is None or other_cluster is None:
                    return True  # 市区×远郊混排：两档都禁止
                if strict and option_cluster != other_cluster:
                    return True  # 异簇远郊同天：仅严格档禁止
            return False

        return _violated(strict=not relaxed_remote)

    relaxed_remote = False  # 严格档先跑；beam 全灭时降级放宽档重跑
    remote_warning = ""

    def _run_beam(relaxed: bool):
        nonlocal relaxed_remote
        relaxed_remote = relaxed
        beam_states = [tuple(tuple() for _ in range(day_count))]
        beam_assigned = 0
        beam_failed = None
        for task in ordered_tasks:
            expanded = {}
            for state in beam_states:
                for option in task:
                    option_key = _spot_key(option)
                    for day_index, route_for_day in enumerate(state):
                        if _remote_violated(option_key, route_for_day):
                            continue
                        for position in range(len(route_for_day) + 1):
                            new_day_route = (
                                *route_for_day[:position],
                                option,
                                *route_for_day[position:],
                            )
                            if elapsed(new_day_route) > daily_limit:
                                continue
                            new_state = list(state)
                            new_state[day_index] = tuple(new_day_route)
                            new_state = tuple(new_state)
                            signature = tuple(
                                tuple(_spot_key(item) for item in route)
                                for route in new_state
                            )
                            expanded[signature] = new_state
            if not expanded:
                beam_failed = task
                break
            beam_states = sorted(expanded.values(), key=state_rank)[:beam_width]
            beam_assigned += 1
        return beam_states, beam_assigned, beam_failed

    states, assigned_count, failed_task = _run_beam(relaxed=False)
    if failed_task is not None and remote_cluster_of:
        # 严格档有远郊任务失败 → 降级放宽档（异簇远郊可拼天）重跑
        states, assigned_count, failed_task = _run_beam(relaxed=True)
        if failed_task is None:
            remote_warning = (
                "远郊必去数量超过可用天数，已放宽为允许不同方向远郊景点同天"
                "（市区与远郊仍不混排）"
            )

    best_state = min(states, key=state_rank) if states else tuple()
    unassigned_tasks = ordered_tasks[assigned_count:]
    if failed_task is not None and (
        not unassigned_tasks or unassigned_tasks[0] is not failed_task
    ):
        unassigned_tasks = [failed_task, *unassigned_tasks]

    days = []
    for day_index in range(day_count):
        route_for_day = list(best_state[day_index]) if best_state else []
        events, schedule_warnings = _build_schedule_events(
            route_for_day, matrix, day_start_minutes, meal_windows, restaurants
        )
        days.append(
            {
                "day": day_index + 1,
                "spots": [
                    {"id": spot.get("id"), "name": spot.get("name")}
                    for spot in route_for_day
                ],
                "counted_minutes": sum(
                    event["end_minutes"] - event["start_minutes"]
                    for event in events
                    if include_meal_time or event["type"] != "meal"
                ),
                "include_meal_time_in_daily_limit": include_meal_time,
                "warnings": schedule_warnings,
            }
        )

    warnings = []
    if conflict_spots:
        warnings.append("未确认保留的冲突必去景点没有参与分配")
    if unassigned_tasks:
        warnings.append("部分必去景点无法在现有天数和每日时长内完成分配")
    if minimum_required_visit_minutes > total_available_minutes:
        warnings.append("必去景点的最低游览时长超过整个旅程可用时长")
    if remote_warning:
        warnings.append(remote_warning)
    return {
        "feasible": not unassigned_tasks,
        "include_meal_time_in_daily_limit": include_meal_time,
        "days": days,
        "unassigned_must_spots": [
            {
                "dependency_group": task[0].get("dependency_group"),
                "options": [
                    {"id": spot.get("id"), "name": spot.get("name")}
                    for spot in task
                ],
            }
            for task in unassigned_tasks
        ],
        "minimum_required_visit_minutes": minimum_required_visit_minutes,
        "total_available_minutes": total_available_minutes,
        "overflow_minutes": max(
            minimum_required_visit_minutes - total_available_minutes, 0
        ),
        "travel_matrix_spot_count": len(matrix.spot_ids),
        "travel_matrix_pair_count": matrix.pair_count,
        "warnings": warnings,
        # Internal form for the next multi-day planning stage.
        "day_routes": [list(route) for route in best_state] if best_state else [],
    }
