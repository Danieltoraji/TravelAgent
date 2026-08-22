"""Must-visit spot assignment across one or multiple days."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

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

    for task in ordered_tasks:
        expanded = {}
        for state in states:
            for option in task:
                for day_index, route_for_day in enumerate(state):
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
            failed_task = task
            break
        states = sorted(expanded.values(), key=state_rank)[:beam_width]
        assigned_count += 1

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
