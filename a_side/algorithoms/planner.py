"""Top-level planners: one-day, multi-day and candidate-route generation."""

from __future__ import annotations

import sys
from copy import deepcopy
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms._common import (
    TARGET_DAY_UTILIZATION,
    Location,
    Spot,
    _budget_context,
    _duration,
    _food_preferences,
    _guide_price,
    _include_meal_time_in_daily_limit,
    _parse_time,
    _price,
    _route_rank,
    _spot_key,
    _ticket_cost,
    _visit_cost,
)
from algorithoms.repair import (
    _fine_tune_route,
    _fit_route_to_budget,
    _fit_route_to_daily_limit,
    _knapsack,
    _knapsack_alternatives,
    _refill_route_with_feasible_spots,
)
from algorithoms.route_ordering import _order_spots, _route_distance
from algorithoms.spot_assignment import _default_must_spots, assign_must_spots_to_days
from algorithoms.timeline import _build_schedule_events, _scheduled_elapsed_minutes
from data_transmission.city_graph import DEFAULT_GRAPH_DIR
from data_transmission.itinerary import (
    build_itinerary_node,
    node_time_period,
    node_to_readable,
)
from data_transmission.meal import DEFAULT_MEAL_WINDOWS, MealWindow
from transport.providers import JsonTravelTimeProvider, TravelTimeMatrix, TravelTimeProvider
from transport.restaurants import RestaurantResolver


def _resolve_restaurants(requirement, travel_time_provider, graph_dir):
    """Build a RestaurantResolver for the destination, or None when unavailable."""
    try:
        destination = requirement["content"]["destination"]
    except (KeyError, TypeError):
        return None
    try:
        resolver = RestaurantResolver(
            destination,
            food_preferences=_food_preferences(requirement),
            travel_time_provider=travel_time_provider,
            data_dir=graph_dir,
        )
    except (OSError, ValueError):
        return None
    return resolver if resolver.restaurants else None


def _planned_restaurant_ids(daily_result: Dict[str, Any]) -> List[str]:
    """单日结果里最终选定的餐厅 id（route_details 的 meal 段去重保序）。"""
    ids: List[str] = []
    for node in daily_result.get("route_details", []) or []:
        if node.get("type") != "meal":
            continue
        restaurant_id = str(node.get("details", {}).get("restaurant_id", "") or "")
        if restaurant_id and restaurant_id not in ids:
            ids.append(restaurant_id)
    return ids


def _repair_day_variant(
    selected: Sequence[Spot],
    must_keys: set,
    optional_pool: Sequence[Spot],
    daily_limit: int,
    start_location: Optional[Location],
    travel_matrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    budget_limit: Optional[float],
    visitor_number: int,
    fine_tune_max_pool: Optional[int],
    restaurants=None,
    min_spots: int = 0,
) -> Optional[Dict[str, Any]]:
    """Repair and fine-tune one selected set into a feasible day variant.

    Returns a dict carrying the ordered route and its repair artifacts, or
    ``None`` when mandatory spots alone overflow the daily limit.
    """
    ordered, removed_for_time, scheduled_elapsed = _fit_route_to_daily_limit(
        selected,
        must_keys,
        daily_limit,
        start_location,
        travel_matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time,
        restaurants,
    )
    if scheduled_elapsed > daily_limit:
        return None
    ordered, removed_for_budget, _ = _fit_route_to_budget(
        ordered,
        must_keys,
        budget_limit,
        visitor_number,
    )
    ordered, refilled_spots, scheduled_elapsed = _refill_route_with_feasible_spots(
        ordered,
        optional_pool,
        daily_limit,
        start_location,
        travel_matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time,
        budget_limit,
        visitor_number,
        restaurants=restaurants,
    )
    ordered, scheduled_elapsed = _fine_tune_route(
        ordered,
        optional_pool,
        must_keys,
        daily_limit,
        start_location,
        travel_matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time,
        budget_limit,
        visitor_number,
        restaurants=restaurants,
        max_pool=fine_tune_max_pool,
        min_spots=min_spots,
    )
    return {
        "ordered": ordered,
        "removed_for_time": removed_for_time,
        "removed_for_budget": removed_for_budget,
        "refilled_spots": refilled_spots,
        "scheduled_elapsed": scheduled_elapsed,
    }


def _generate_one_day_candidates(
    requirement: Dict[str, Any],
    must_spots: Sequence[Spot],
    scored_spots: Sequence[Spot],
    score_band: float,
    fine_tune_max_pool: Optional[int],
    graph_dir: Path,
    day_start_time: str,
    travel_time_provider: Optional[TravelTimeProvider],
    meal_windows: Sequence[MealWindow],
    restaurants=None,
    max_candidates: Optional[int] = None,
    min_spots: int = 0,
) -> List[Dict[str, Any]]:
    """Seed, repair and fine-tune several one-day routes, best-ranked first.

    The score-band knapsack proposes near-optimal subsets; each is repaired
    against time and budget using the *full* optional pool, then fine-tuned.
    The returned dicts carry ``route_details`` and ranking metadata ready for
    concise output.
    """
    try:
        content = requirement["content"]
        destination = content["destination"]
        daily_limit = int(content["constraints"]["daily_travel_time"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("requirement 缺少有效的 destination 或 daily_travel_time") from exc
    if daily_limit <= 0:
        raise ValueError("daily_travel_time 必须大于 0")

    include_meal_time = _include_meal_time_in_daily_limit(requirement)
    budget_limit, visitor_number = _budget_context(requirement)
    day_start_minutes = _parse_time(day_start_time)

    resolved_must = _default_must_spots(must_spots)
    must_by_key = {_spot_key(spot): spot for spot in resolved_must}
    must_duration = sum(_duration(spot) for spot in must_by_key.values())
    optional_by_key = {
        _spot_key(spot): spot
        for spot in scored_spots
        if _spot_key(spot) not in must_by_key
    }
    all_candidate_spots = {**optional_by_key, **must_by_key}
    provider = travel_time_provider or JsonTravelTimeProvider(destination, graph_dir)
    travel_matrix = provider.get_matrix(all_candidate_spots.keys())
    remaining_capacity = daily_limit - must_duration
    optional_pool = list(optional_by_key.values())

    alternatives = _knapsack_alternatives(
        optional_pool,
        remaining_capacity,
        score_band=score_band,
        max_routes=max_candidates,
    )
    candidates = []
    seen = set()
    for _, optional_subset in alternatives:
        selected = [*must_by_key.values(), *optional_subset]
        variant = _repair_day_variant(
            selected,
            set(must_by_key),
            optional_pool,
            daily_limit,
            None,
            travel_matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time,
            budget_limit,
            visitor_number,
            fine_tune_max_pool,
            restaurants=restaurants,
            min_spots=min_spots,
        )
        if variant is None:
            continue
        ordered = variant["ordered"]
        signature = tuple(_spot_key(spot) for spot in ordered)
        if signature in seen:
            continue
        seen.add(signature)

        raw_events, _ = _build_schedule_events(
            ordered, travel_matrix, day_start_minutes, meal_windows, restaurants
        )
        route_details = []
        for event in raw_events:
            details = dict(event["details"])
            if event["type"] == "spot":
                details["is_must_visit"] = details["spot_id"] in must_by_key
            if event["type"] == "transport":
                details["source"] = provider.source_name
            route_details.append(
                build_itinerary_node(
                    event["type"],
                    event["name"],
                    event["start_minutes"],
                    event["end_minutes"],
                    details,
                )
            )
        elapsed = variant["scheduled_elapsed"]
        total_match_score = sum(
            float(spot.get("match_score", 0)) for spot in ordered
        )
        candidates.append(
            {
                "route_details": route_details,
                "total_match_score": total_match_score,
                "utilization_rate": round(elapsed / daily_limit, 4),
                "rank": _route_rank(
                    elapsed, total_match_score, daily_limit, len(ordered), min_spots
                ),
            }
        )

    candidates.sort(key=lambda item: item["rank"], reverse=True)
    if max_candidates is not None:
        candidates = candidates[:max_candidates]
    return candidates


def _allocate_optional_spots(
    mandatory_routes: Sequence[Sequence[Spot]],
    optional_spots: Sequence[Spot],
    daily_limit: int,
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    restaurants=None,
    strategy: str = "balanced",
    perturbation: int = 0,
) -> List[List[Spot]]:
    """把可选景点预分配到各天（只决定「每天可选子池」，不排程）。

    ``strategy``：
      - ``balanced``（默认）：按 (-match_score, duration, key) 排序，逐个塞给
        「当前最空且塞得下」的天 → 各天景点数量 / 负载均匀，避免前两天抢光
        短平快景点、最后一天只剩又远又长的。
      - ``greedy``：逐个塞给「最早（天数最小）能塞下」的天 → Day1 优先拿高分
        短景点（近似早期的逐日贪心效果）。
    ``perturbation``：可行天里取第 k 个（0=最优，1=次优……）→ 制造变体种子。

    8.30 远郊日保护：含远郊必去（距质心 > 25km，spot_assignment 识别）的
    天不接收市区可选景点——远郊日的时间预算应留给往返通勤，市区景点硬塞
    会挤爆当日限额或制造「远郊+市区折返跑」。
    """
    day_count = len(mandatory_routes)
    prealloc: List[List[Spot]] = [[] for _ in range(day_count)]

    # 远郊日识别：当天任一必去景点被 _remote_groups 判为远郊 → 整天保护。
    from algorithoms.spot_assignment import _remote_groups

    all_must = {
        _spot_key(spot): spot
        for route in mandatory_routes
        for spot in route
    }
    remote_cluster_of, _ = _remote_groups(all_must)
    remote_day_indexes = {
        index
        for index, route in enumerate(mandatory_routes)
        if any(_spot_key(spot) in remote_cluster_of for spot in route)
    }

    def day_elapsed(index: int, extra: Optional[Spot] = None) -> Optional[int]:
        """排程后的计入时长；若插入后出现超闭馆景点则返回 None（不可塞入）。"""
        route = [*mandatory_routes[index], *prealloc[index]]
        if extra is not None:
            route.append(extra)
        ordered = _order_spots(route, None, matrix)  # 真实排程后再算耗时
        raw_events, _ = _build_schedule_events(
            ordered, matrix, day_start_minutes, meal_windows, restaurants
        )
        for event in raw_events:
            if event["type"] != "spot":
                continue
            spot = next(
                (s for s in ordered if _spot_key(s) == event["details"]["spot_id"]),
                None,
            )
            if spot is None:
                continue
            if event["end_minutes"] > _parse_time(
                spot.get("closing_time", "24:00")
            ):
                return None
        return sum(
            event["end_minutes"] - event["start_minutes"]
            for event in raw_events
            if include_meal_time or event["type"] != "meal"
        )

    # 长景点先分配（选择少、先占位），短景点后补——避免短景点散落空天、
    # 长景点塞不进任何天导致「必去天饿死 / 单景点天」。
    optional_sorted = sorted(
        optional_spots,
        key=lambda spot: (
            -_duration(spot),
            -float(spot.get("match_score", 0)),
            _spot_key(spot),
        ),
    )
    for spot in optional_sorted:
        feasible = [
            index
            for index in range(day_count)
            if index not in remote_day_indexes  # 远郊日不塞市区可选
            and (elapsed := day_elapsed(index, spot)) is not None
            and elapsed <= daily_limit
        ]
        if not feasible:
            continue
        if strategy == "greedy":
            feasible.sort(key=lambda index: index)  # Day1 优先
        else:
            feasible.sort(key=lambda index: day_elapsed(index) or 0)  # 最空优先
        chosen = feasible[min(perturbation, len(feasible) - 1)]
        prealloc[chosen].append(spot)
    return prealloc


def _plan_multi_day_with_prealloc(
    requirement: Dict[str, Any],
    mandatory_routes: Sequence[Sequence[Spot]],
    prealloc: Sequence[Sequence[Spot]],
    conflict_spots: Sequence[Spot],
    optional_spots: Sequence[Spot],
    graph_dir: Path,
    day_start_time: str,
    travel_time_provider: Optional[TravelTimeProvider],
    meal_windows: Sequence[MealWindow],
    restaurants,
    repair_hard_constraints: bool,
    min_spots: int,
) -> Dict[str, Any]:
    """给定必去分天 + 可选预分配池，逐天走完整 plan_one_day 管线并汇总。

    所有多日种子的「执行」都经过这里：每天的可选景点只能从当天的预分配子池里
    knapsack / refill / fine-tune，质量（repair / min_spots / 闭馆检查）统一
    由单日管线承担——平衡分配的「均匀」收益保留、质量保证不缺失。
    """
    content = requirement["content"]
    daily_limit = int(content["constraints"]["daily_travel_time"])
    day_count = len(mandatory_routes)
    include_meal_time = _include_meal_time_in_daily_limit(requirement)
    budget_limit, visitor_number = _budget_context(requirement)

    provider = travel_time_provider or JsonTravelTimeProvider(
        content["destination"], graph_dir
    )

    # 跨天去重（8.31 P0）：每个多日种子开始时重置「之前各天最终选定」记录，
    # 保证结果与种子/试算顺序无关；每天完成后只记当天最终选定的餐厅。
    reset_planned = getattr(restaurants, "reset_planned", None)
    if callable(reset_planned):
        reset_planned()
    note_planned = getattr(restaurants, "note_planned", None)

    planned_days = []
    for day_index, (mandatory_route, optional_prealloc) in enumerate(
        zip(mandatory_routes, prealloc), start=1
    ):
        daily_candidates = [
            list(mandatory_route),
            conflict_spots if day_index == 1 else [],
            list(optional_prealloc),
        ]
        daily_requirement = deepcopy(requirement)
        # 预算为全程约束，每天不单独扣减（汇总时整体检查）。
        daily_requirement["content"]["constraints"]["budget"] = None
        daily_result = plan_one_day(
            daily_requirement,
            daily_candidates,
            graph_dir=graph_dir,
            day_start_time=day_start_time,
            travel_time_provider=provider,
            meal_windows=meal_windows,
            restaurants=restaurants,
            repair_hard_constraints=repair_hard_constraints,
            min_spots=min_spots,
        )
        if not daily_result["feasible"] and repair_hard_constraints:
            return {
                "feasible": False,
                "reason": f"第{day_index}天规划失败：{daily_result.get('reason')}",
                "days": planned_days,
                "failed_day": day_index,
                "daily_result": daily_result,
            }
        planned_days.append({"day": day_index, **daily_result})
        # 跨天去重：记当天**最终**选定的餐厅（候选试算里的选择不进排除集）。
        if callable(note_planned):
            note_planned(_planned_restaurant_ids(daily_result))

    estimated_ticket_cost = sum(day["estimated_ticket_cost"] for day in planned_days)
    estimated_guide_cost = sum(day["estimated_guide_cost"] for day in planned_days)
    estimated_visit_cost = estimated_ticket_cost + estimated_guide_cost
    budget_remaining = (
        budget_limit - estimated_visit_cost if budget_limit is not None else None
    )
    budget_exceeded = budget_remaining is not None and budget_remaining < 0
    overtime_days = [
        {
            "day": day["day"],
            "overflow_minutes": day.get("time_overflow_minutes", 0),
        }
        for day in planned_days
        if day.get("is_overtime")
    ]
    hard_constraint_violations = []
    if overtime_days:
        hard_constraint_violations.append(
            {"constraint": "daily_travel_time", "days": overtime_days}
        )
    if budget_exceeded:
        hard_constraint_violations.append(
            {"constraint": "budget", "overflow": -budget_remaining}
        )

    used_spot_keys = {
        node["details"]["spot_id"]
        for day in planned_days
        for node in day.get("route_details", [])
        if node.get("type") == "spot"
    }
    # 未选景点 = 全部可选景点中未被任何一天选用的（含未预分配的部分）。
    unassigned_optional = [
        spot.get("name")
        for spot in optional_spots
        if _spot_key(spot) not in used_spot_keys
    ]

    return {
        "feasible": not overtime_days and not budget_exceeded,
        "days_requested": day_count,
        "daily_travel_time": daily_limit,
        "include_meal_time_in_daily_limit": include_meal_time,
        "total_available_minutes": day_count * daily_limit,
        "days": planned_days,
        "visitor_number": visitor_number,
        "budget": budget_limit,
        "estimated_ticket_cost": estimated_ticket_cost,
        "estimated_guide_cost": estimated_guide_cost,
        "budget_remaining": budget_remaining,
        "budget_overflow": max(-budget_remaining, 0) if budget_remaining is not None else 0,
        "budget_exceeded": budget_exceeded,
        "budget_scope": "景点门票+讲解",
        "is_overtime": bool(overtime_days),
        "overtime_days": overtime_days,
        "hard_constraint_violations": hard_constraint_violations,
        "total_match_score": sum(day["total_match_score"] for day in planned_days),
        "unassigned_must_spots": [],
        "unassigned_optional_spots": unassigned_optional,
        "warnings": [],
    }


def plan_one_day(
    requirement: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    start_location: Optional[Location] = None,
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    day_start_time: str = "09:00",
    travel_time_provider: Optional[TravelTimeProvider] = None,
    meal_windows: Sequence[MealWindow] = DEFAULT_MEAL_WINDOWS,
    restaurants=None,
    repair_hard_constraints: bool = True,
    score_band: float = 5.0,
    max_candidates: Optional[int] = 5,
    fine_tune_max_pool: Optional[int] = 10,
    min_spots: int = 0,
) -> Dict[str, Any]:
    """Create a one-day attraction plan.

    ``candidate_spots`` follows ``select_spots``'s return contract:
    ``[must_spots, conflict_spots, scored_spots]``.
    """
    try:
        content = requirement["content"]
        destination = content["destination"]
        daily_limit = content["constraints"]["daily_travel_time"]
    except (KeyError, TypeError) as exc:
        raise ValueError("requirement 缺少 constraints.daily_travel_time") from exc
    if isinstance(daily_limit, bool) or not isinstance(daily_limit, (int, float)):
        raise ValueError("daily_travel_time 必须是分钟数")
    daily_limit = int(daily_limit)
    if daily_limit <= 0:
        raise ValueError("daily_travel_time 必须大于 0")
    if len(candidate_spots) != 3:
        raise ValueError("candidate_spots 必须是 [must_spots, conflict_spots, scored_spots]")
    if start_location is not None:
        raise ValueError("当前交通时间来自 spots_graph，暂不支持图外的 start_location")

    include_meal_time = _include_meal_time_in_daily_limit(requirement)
    budget_limit, visitor_number = _budget_context(requirement)

    day_start_minutes = _parse_time(day_start_time)
    must_spots, conflict_spots, scored_spots = candidate_spots
    resolved_must_spots = _default_must_spots(must_spots)
    must_by_key = {_spot_key(spot): spot for spot in resolved_must_spots}
    must_duration = sum(_duration(spot) for spot in must_by_key.values())
    must_ticket_cost = _ticket_cost(list(must_by_key.values()), visitor_number)
    # 8.30 预算口径：门票 + 讲解（选景点即确定的费用）纳入排程预算约束；
    # 酒店/餐饮不进排程硬约束（选后计，见 _plan_cost_summary / select_hotels_for_plan）。
    must_visit_cost = _visit_cost(list(must_by_key.values()), visitor_number)
    warnings = []
    if conflict_spots:
        warnings.append("部分必去景点命中了 dismissed_tags，未加入路线")
    if must_duration > daily_limit and repair_hard_constraints:
        return {
            "feasible": False,
            "reason": "必去景点总游览时间超过每日出游时长",
            "daily_travel_time": daily_limit,
            "include_meal_time_in_daily_limit": include_meal_time,
            "required_visit_minutes": must_duration,
            "overflow_minutes": must_duration - daily_limit,
            "route": [],
            "conflict_spots": [spot.get("name") for spot in conflict_spots],
            "warnings": warnings,
        }
    if (
        budget_limit is not None
        and must_visit_cost > budget_limit
        and repair_hard_constraints
    ):
        return {
            "feasible": False,
            "reason": "必去景点门票+讲解费用超过当前可用预算",
            "budget": budget_limit,
            "visitor_number": visitor_number,
            "estimated_ticket_cost": must_ticket_cost,
            "estimated_guide_cost": (
                sum(_guide_price(spot) for spot in must_by_key.values())
                * visitor_number
            ),
            "budget_remaining": budget_limit - must_visit_cost,
            "budget_overflow": must_visit_cost - budget_limit,
            "budget_exceeded": True,
            "budget_scope": "景点门票+讲解",
            "daily_travel_time": daily_limit,
            "include_meal_time_in_daily_limit": include_meal_time,
            "route": [],
            "conflict_spots": [spot.get("name") for spot in conflict_spots],
            "warnings": [*warnings, "预算暂未包含城际交通费用"],
        }

    optional_by_key = {
        _spot_key(spot): spot
        for spot in scored_spots
        if _spot_key(spot) not in must_by_key
    }
    all_candidate_spots = {**optional_by_key, **must_by_key}
    provider = travel_time_provider or JsonTravelTimeProvider(destination, graph_dir)
    travel_matrix = provider.get_matrix(all_candidate_spots.keys())
    remaining_capacity = daily_limit - must_duration
    optional_pool = list(optional_by_key.values())
    if not repair_hard_constraints:
        # Legacy path: a single knapsack pick without repair or fine-tuning.
        optional_selected = _knapsack(optional_pool, remaining_capacity)
        selected = [*must_by_key.values(), *optional_selected]
        ordered = _order_spots(selected, start_location, travel_matrix)
        removed_for_time = []
        removed_for_budget = []
        refilled_spots = []
        scheduled_elapsed = _scheduled_elapsed_minutes(
            ordered,
            start_location,
            travel_matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time,
            restaurants,
        )
    else:
        # Score-band knapsack seeds several near-optimal routes; each is then
        # repaired and fine-tuned before the best one is kept.
        alternatives = _knapsack_alternatives(
            optional_pool,
            remaining_capacity,
            score_band=score_band,
            max_routes=max_candidates,
        )
        best = None
        for _, optional_subset in alternatives:
            selected = [*must_by_key.values(), *optional_subset]
            variant = _repair_day_variant(
                selected,
                set(must_by_key),
                optional_pool,
                daily_limit,
                start_location,
                travel_matrix,
                day_start_minutes,
                meal_windows,
                include_meal_time,
                budget_limit,
                visitor_number,
                fine_tune_max_pool,
                restaurants=restaurants,
                min_spots=min_spots,
            )
            if variant is None:
                # Mandatory spots alone overflow; no optional subset can help.
                continue
            ordered = variant["ordered"]
            rank = _route_rank(
                variant["scheduled_elapsed"],
                sum(float(spot.get("match_score", 0)) for spot in ordered),
                daily_limit,
                len(ordered),
            )
            if best is None or rank > best["rank"]:
                best = {**variant, "rank": rank}

        if best is None:
            mandatory_route = _order_spots(
                list(must_by_key.values()), start_location, travel_matrix
            )
            required_elapsed = _scheduled_elapsed_minutes(
                mandatory_route,
                start_location,
                travel_matrix,
                day_start_minutes,
                meal_windows,
                include_meal_time,
                restaurants,
            )
            return {
                "feasible": False,
                "reason": (
                    "必去景点的游览、交通、等待及按用户选择计入的用餐时间"
                    "超过每日出游时长"
                ),
                "daily_travel_time": daily_limit,
                "include_meal_time_in_daily_limit": include_meal_time,
                "required_elapsed_minutes": required_elapsed,
                "overflow_minutes": required_elapsed - daily_limit,
                "route": [],
                "conflict_spots": [spot.get("name") for spot in conflict_spots],
                "warnings": warnings,
            }

        ordered = best["ordered"]
        removed_for_time = best["removed_for_time"]
        removed_for_budget = best["removed_for_budget"]
        refilled_spots = best["refilled_spots"]
        scheduled_elapsed = best["scheduled_elapsed"]

    if removed_for_time:
        warnings.append("加入交通和等待时间后，已移除部分低收益非必去景点")
    if removed_for_budget:
        warnings.append("加入门票费用后，已移除部分低收益非必去景点以满足预算")
    if refilled_spots:
        warnings.append("已使用其他可行景点重新填补删除后释放的时间")
    selected = list(ordered)
    selected_keys = {_spot_key(spot) for spot in selected}

    raw_events, schedule_warnings = _build_schedule_events(
        ordered, travel_matrix, day_start_minutes, meal_windows, restaurants,
        complete_day=True,
    )
    warnings.extend(schedule_warnings)
    route_details = []
    for event in raw_events:
        details = dict(event["details"])
        if event["type"] == "spot":
            details["is_must_visit"] = details["spot_id"] in must_by_key
        if event["type"] == "transport":
            details["source"] = provider.source_name
        route_details.append(
            build_itinerary_node(
                event["type"],
                event["name"],
                event["start_minutes"],
                event["end_minutes"],
                details,
            )
        )
    readable_route = [node_to_readable(node) for node in route_details]

    total_visit_minutes = sum(
        item["duration_minutes"] for item in route_details if item["type"] == "spot"
    )
    total_transport_minutes = sum(
        item["duration_minutes"]
        for item in route_details
        if item["type"] == "transport"
    )
    total_waiting_minutes = sum(
        item["duration_minutes"]
        for item in route_details
        if item["type"] == "waiting"
    )
    total_meal_minutes = sum(
        item["duration_minutes"]
        for item in route_details
        if item["type"] == "meal"
    )
    total_elapsed_minutes = sum(item["duration_minutes"] for item in route_details)
    total_counted_minutes = total_elapsed_minutes
    if not include_meal_time:
        total_counted_minutes -= total_meal_minutes
    time_overflow_minutes = max(total_counted_minutes - daily_limit, 0)
    is_overtime = time_overflow_minutes > 0
    estimated_ticket_cost = _ticket_cost(selected, visitor_number)
    estimated_guide_cost = (
        sum(_guide_price(spot) for spot in selected) * visitor_number
    )
    estimated_visit_cost = estimated_ticket_cost + estimated_guide_cost
    budget_exceeded = (
        budget_limit is not None and estimated_visit_cost > budget_limit
    )
    budget_remaining = (
        budget_limit - estimated_visit_cost if budget_limit is not None else None
    )
    hard_constraint_violations = []
    if is_overtime:
        hard_constraint_violations.append(
            {"constraint": "daily_travel_time", "overflow": time_overflow_minutes}
        )
    if budget_exceeded:
        hard_constraint_violations.append(
            {"constraint": "budget", "overflow": estimated_visit_cost - budget_limit}
        )
    return {
        "feasible": not is_overtime and not budget_exceeded,
        "daily_travel_time": daily_limit,
        "include_meal_time_in_daily_limit": include_meal_time,
        "total_visit_minutes": total_visit_minutes,
        "total_transport_minutes": total_transport_minutes,
        "total_waiting_minutes": total_waiting_minutes,
        "total_meal_minutes": total_meal_minutes,
        "total_elapsed_minutes": total_elapsed_minutes,
        "total_counted_minutes": total_counted_minutes,
        "remaining_minutes": max(daily_limit - total_counted_minutes, 0),
        "is_overtime": is_overtime,
        "time_overflow_minutes": time_overflow_minutes,
        "utilization_rate": round(total_counted_minutes / daily_limit, 4),
        "target_utilization_rate": TARGET_DAY_UTILIZATION,
        "total_route_distance_km": round(
            _route_distance(ordered, start_location, travel_matrix), 2
        ),
        "travel_matrix_spot_count": len(travel_matrix.spot_ids),
        "travel_matrix_pair_count": travel_matrix.pair_count,
        "refilled_spots": [spot.get("name") for spot in refilled_spots],
        "visitor_number": visitor_number,
        "budget": budget_limit,
        "estimated_ticket_cost": estimated_ticket_cost,
        "estimated_guide_cost": estimated_guide_cost,
        "budget_remaining": budget_remaining,
        "budget_overflow": max(-budget_remaining, 0) if budget_remaining is not None else 0,
        "budget_exceeded": budget_exceeded,
        "budget_scope": "景点门票+讲解",
        "hard_constraint_violations": hard_constraint_violations,
        "total_match_score": sum(
            float(spot.get("match_score", 0)) for spot in selected
        ),
        "route": readable_route,
        "route_details": route_details,
        "unselected_spots": [
            spot.get("name")
            for key, spot in optional_by_key.items()
            if key not in selected_keys
        ],
        "conflict_spots": [spot.get("name") for spot in conflict_spots],
        "warnings": [
            *warnings,
            "预算口径为目的地内费用（门票+讲解+酒店+餐饮），不含城际/市内交通",
            "交通时间来自 spots_graph.json 的模拟数据，并非实时地图数据",
        ],
    }


def plan_multi_day(
    requirement: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    day_start_time: str = "09:00",
    travel_time_provider: Optional[TravelTimeProvider] = None,
    meal_windows: Sequence[MealWindow] = DEFAULT_MEAL_WINDOWS,
    restaurants=None,
    beam_width: int = 200,
    repair_hard_constraints: bool = True,
    allocator: str = "balanced",
    perturbation: int = 0,
    min_spots: int = 0,
) -> Dict[str, Any]:
    """Plan all requested days: beam must-assignment + optional allocator + full
    per-day pipeline.

    ``allocator``：可选景点的跨天分配策略（``balanced`` 均匀 / ``greedy`` 逐日优先）。
    ``perturbation``：分配时在可行天里取第 k 个（0=最优）→ 与其它种子配合生成多路线候选。
    ``min_spots``：完整的一天至少排多少个景点（达标路由优先，硬规则透传）。
    """
    try:
        day_count = int(requirement["content"]["days"])
        daily_limit = int(
            requirement["content"]["constraints"]["daily_travel_time"]
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("requirement 缺少有效的 days 或 daily_travel_time") from exc
    if day_count <= 0:
        raise ValueError("days 必须大于 0")
    if allocator not in {"balanced", "greedy"}:
        raise ValueError(f"未知 allocator：{allocator!r}（可选 balanced / greedy）")

    allocation = assign_must_spots_to_days(
        requirement,
        candidate_spots,
        graph_dir=graph_dir,
        day_start_time=day_start_time,
        travel_time_provider=travel_time_provider,
        meal_windows=meal_windows,
        restaurants=restaurants,
        beam_width=beam_width,
    )
    if not allocation["feasible"]:
        return {
            **allocation,
            "daily_travel_time": daily_limit,
            "days_requested": day_count,
        }

    mandatory_routes = allocation["day_routes"]
    _, conflict_spots, scored_spots = candidate_spots
    # Every concrete option belonging to a must-visit task is reserved. Only
    # the option chosen by allocation may appear, and only as a mandatory spot.
    must_option_keys = {_spot_key(spot) for spot in candidate_spots[0]}
    optional_spots = [
        spot for spot in scored_spots if _spot_key(spot) not in must_option_keys
    ]

    provider = travel_time_provider or JsonTravelTimeProvider(
        requirement["content"]["destination"], graph_dir
    )
    matrix = provider.get_matrix(
        {*must_option_keys, *(_spot_key(spot) for spot in optional_spots)}
    )

    day_start_minutes = _parse_time(day_start_time)
    include_meal_time = _include_meal_time_in_daily_limit(requirement)

    prealloc = _allocate_optional_spots(
        mandatory_routes,
        optional_spots,
        daily_limit,
        matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time,
        restaurants,
        strategy=allocator,
        perturbation=perturbation,
    )
    plan = _plan_multi_day_with_prealloc(
        requirement,
        mandatory_routes,
        prealloc,
        conflict_spots,
        optional_spots,
        graph_dir,
        day_start_time,
        travel_time_provider,
        meal_windows,
        restaurants,
        repair_hard_constraints,
        min_spots,
    )
    if plan.get("feasible"):
        plan["minimum_required_visit_minutes"] = allocation[
            "minimum_required_visit_minutes"
        ]
        plan["warnings"] = allocation["warnings"]
    return plan


def generate_route_candidates(
    requirement: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    max_routes: int = 3,
    max_evaluations: int = 40,
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    day_start_time: str = "09:00",
    travel_time_provider: Optional[TravelTimeProvider] = None,
    meal_windows: Sequence[MealWindow] = DEFAULT_MEAL_WINDOWS,
    restaurants=None,
    beam_width: int = 200,
    score_band: float = 5.0,
    fine_tune_max_pool: Optional[int] = 10,
    min_spots: int = 2,
) -> Dict[str, Any]:
    """Return concise feasible alternatives after repair and fine-tuning.

    Both one-day and multi-day requests are seeded by the score-band knapsack;
    every alternative is repaired against time and budget and then fine-tuned
    toward a fuller, higher-scoring schedule. ``max_evaluations`` is kept for
    backward compatibility and no longer bounds multi-day enumeration.

    ``min_spots``：完整的一天至少排多少个景点（硬规则，达标路由优先），
    已透传到单日候选与多日执行（plan_multi_day）。
    """
    if max_routes <= 0 or max_evaluations <= 0:
        raise ValueError("max_routes 和 max_evaluations 必须大于 0")
    if len(candidate_spots) != 3:
        raise ValueError("candidate_spots 必须是 [must_spots, conflict_spots, scored_spots]")

    must_spots, conflict_spots, scored_spots = candidate_spots
    day_count = int(requirement.get("content", {}).get("days") or 1)

    if restaurants is None:
        restaurants = _resolve_restaurants(
            requirement,
            travel_time_provider
            or (
                JsonTravelTimeProvider(requirement.get("content", {}).get("destination"), graph_dir)
                if requirement.get("content", {}).get("destination")
                else None
            ),
            graph_dir,
        )

    def concise_spots(route_details: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
        return [
            {"name": node["name"], "time_period": node_time_period(node)}
            for node in route_details
            if node.get("type") == "spot"
        ]

    def concise_meals(route_details: Sequence[Dict[str, Any]]) -> List[Dict[str, str]]:
        return [
            {
                "name": node["name"],
                "restaurant": node.get("details", {}).get("restaurant_name") or "",
                "time_period": node_time_period(node),
            }
            for node in route_details
            if node.get("type") == "meal"
            and node.get("details", {}).get("restaurant_name")
        ]

    def concise_route(result: Dict[str, Any]) -> Dict[str, Any]:
        if day_count == 1:
            details = result.get("route_details", [])
            return {
                "days": [
                    {
                        "day": 1,
                        "spots": concise_spots(details),
                        "meals": concise_meals(details),
                    }
                ]
            }
        return {
            "days": [
                {
                    "day": day["day"],
                    "spots": concise_spots(day.get("route_details", [])),
                    "meals": concise_meals(day.get("route_details", [])),
                }
                for day in result.get("days", [])
            ]
        }

    if day_count == 1:
        candidates = _generate_one_day_candidates(
            requirement,
            must_spots,
            scored_spots,
            score_band=score_band,
            fine_tune_max_pool=fine_tune_max_pool,
            graph_dir=graph_dir,
            day_start_time=day_start_time,
            travel_time_provider=travel_time_provider,
            meal_windows=meal_windows,
            restaurants=restaurants,
            min_spots=min_spots,
        )
        top = candidates[:max_routes]
        return {
            "routes": [concise_route(candidate) for candidate in top],
            "plans": top,
        }

    # Multi-day: several seeds (allocator + perturbation) all run through the
    # same full per-day pipeline (plan_multi_day); de-duplicate, rank, keep the
    # best as the executable plan and the top N as candidates.
    seeds: List[Dict[str, Any]] = []
    for strategy, perturbation in (
        ("balanced", 0),
        ("balanced", 1),
        ("balanced", 2),
        ("greedy", 0),
    ):
        plan = plan_multi_day(
            requirement,
            candidate_spots,
            graph_dir=graph_dir,
            day_start_time=day_start_time,
            travel_time_provider=travel_time_provider,
            meal_windows=meal_windows,
            restaurants=restaurants,
            beam_width=beam_width,
            allocator=strategy,
            perturbation=perturbation,
            min_spots=min_spots,
        )
        if plan["feasible"]:
            seeds.append(plan)

    def plan_signature(plan: Dict[str, Any]) -> tuple:
        return tuple(
            tuple(
                node.get("details", {}).get("spot_id")
                for node in day.get("route_details", [])
                if node.get("type") == "spot"
            )
            for day in plan.get("days", [])
        )

    unique: Dict[Any, Dict[str, Any]] = {}
    for plan in seeds:
        unique.setdefault(plan_signature(plan), plan)

    # min_spots 作为排序优先级（首选路线保证达标），不硬过滤：
    # 数据允许时「每天至少 min_spots 个景点」的种子排最前，变体种子保留在后
    # （扰动天生分布不均，可能某天 <min_spots——这正是变体差异）。
    def meets_min(plan: Dict[str, Any]) -> bool:
        if min_spots <= 0:
            return True
        return all(
            len(
                [
                    node
                    for node in day.get("route_details", [])
                    if node.get("type") == "spot"
                ]
            )
            >= min_spots
            for day in plan.get("days", [])
        )

    ranked = sorted(
        unique.values(),
        key=lambda plan: (
            meets_min(plan),
            plan.get("total_match_score", 0),
            sum(
                day.get("utilization_rate", 0)
                for day in plan.get("days", [])
                if day.get("utilization_rate") is not None
            ),
        ),
        reverse=True,
    )[:max_routes]
    return {
        "routes": [concise_route(plan) for plan in ranked],
        "plans": ranked,
    }
