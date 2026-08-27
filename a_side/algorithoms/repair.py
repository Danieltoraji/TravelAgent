"""Validation and repair: capacity fitting, budget fitting and refill."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms._common import (
    TARGET_DAY_UTILIZATION,
    Location,
    Spot,
    _duration,
    _parse_time,
    _price,
    _route_rank,
    _spot_key,
    _visit_cost,
)
from algorithoms.route_ordering import _order_spots
from algorithoms.timeline import _build_schedule_events, _scheduled_elapsed_minutes
from data_transmission.meal import MealWindow
from transport.providers import TravelTimeMatrix


def _knapsack(spots: Sequence[Spot], capacity: int) -> List[Spot]:
    """Select attractions while balancing score and capacity utilization."""
    # capacity -> (score, selected spots); keeping sparse states makes this
    # usable even if the daily limit is expressed in minutes.
    states = {0: (0.0, [])}
    for spot in spots:
        minutes = _duration(spot)
        score = float(spot.get("match_score", 0))
        if minutes > capacity:
            continue
        next_states = dict(states)
        for used, (total_score, selected) in states.items():
            new_used = used + minutes
            if new_used > capacity:
                continue
            candidate = (total_score + score, [*selected, spot])
            existing = next_states.get(new_used)
            if existing is None or candidate[0] > existing[0]:
                next_states[new_used] = candidate
        states = next_states

    def final_rank(item):
        used, (score, selected) = item
        return _route_rank(used, score, capacity, len(selected))

    _, (_, selected) = max(
        states.items(),
        key=final_rank,
    )
    return selected


def _knapsack_alternatives(
    spots: Sequence[Spot],
    capacity: int,
    score_band: float = 5.0,
    max_routes: Optional[int] = None,
) -> List[Tuple[float, List[Spot]]]:
    """Return subsets within ``score_band`` of the best total score.

    Unlike :func:`_knapsack`, which returns one best-ranked route, this keeps
    every near-optimal route so downstream fine-tuning can pick the fullest,
    highest-scoring one. Each used-minutes bucket retains only solutions whose
    score is within ``score_band`` of that bucket's current best; because a
    bucket's best never decreases while spots are added, this pruning can
    never drop a solution that later re-enters the final band.

    Results are de-duplicated by spot set and ordered by :func:`_route_rank`.
    """
    if score_band < 0:
        raise ValueError("score_band 必须 >= 0")
    if max_routes is not None and max_routes <= 0:
        raise ValueError("max_routes 必须 > 0")

    # used -> [(score, selected spots), ...] retained within the band.
    states = {0: [(0.0, [])]}
    for spot in spots:
        minutes = _duration(spot)
        score = float(spot.get("match_score", 0))
        if minutes > capacity:
            continue
        next_states = {used: list(items) for used, items in states.items()}
        for used, items in states.items():
            for total_score, selected in items:
                new_used = used + minutes
                if new_used > capacity:
                    continue
                next_states.setdefault(new_used, []).append(
                    (total_score + score, [*selected, spot])
                )
        for used, items in list(next_states.items()):
            bucket_best = max(total_score for total_score, _ in items)
            next_states[used] = [
                (total_score, selected)
                for total_score, selected in items
                if total_score >= bucket_best - score_band
            ]
        states = next_states

    global_max = max(
        total_score
        for items in states.values()
        for total_score, _ in items
    )
    seen = set()
    ranked = []
    for used, items in states.items():
        for total_score, selected in items:
            if total_score < global_max - score_band:
                continue
            signature = frozenset(_spot_key(spot) for spot in selected)
            if signature in seen:
                continue
            seen.add(signature)
            ranked.append(
                (
                    _route_rank(used, total_score, capacity, len(selected)),
                    total_score,
                    selected,
                )
            )
    ranked.sort(key=lambda item: item[0], reverse=True)
    if max_routes is not None:
        ranked = ranked[:max_routes]
    return [(total_score, selected) for _, total_score, selected in ranked]


def _fit_route_to_daily_limit(
    selected: Sequence[Spot],
    must_keys: set,
    daily_limit: int,
    start_location: Optional[Location],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    restaurants=None,
) -> Tuple[List[Spot], List[Spot], int]:
    """Remove low-value optional spots until visits, transport and closing fit."""
    kept = list(selected)
    removed: List[Spot] = []
    while True:
        ordered = _order_spots(kept, start_location, matrix)
        elapsed = _scheduled_elapsed_minutes(
            ordered,
            start_location,
            matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time, restaurants,
        )

        # 闭馆违规（轻量版判据）：当前序列里「计划结束时间超过闭馆时间」的景点。
        events, _ = _build_schedule_events(
            ordered, matrix, day_start_minutes, meal_windows, restaurants
        )
        closing_violators = set()
        for event in events:
            if event["type"] != "spot":
                continue
            spot_key = event["details"]["spot_id"]
            spot = next((s for s in kept if _spot_key(s) == spot_key), None)
            if spot is None:
                continue
            closing = _parse_time(spot.get("closing_time", "24:00"))
            if event["end_minutes"] > closing:
                closing_violators.add(spot_key)

        overtime = elapsed > daily_limit
        # 轻量版：超时，或「不超时但存在超闭馆可选景点且删后仍至少剩一个景点」
        # → 进入删除循环（超闭馆景点优先删）；单景点天超闭馆保留并提示。
        removable_violators = [key for key in closing_violators if key not in must_keys]
        if not overtime and not (removable_violators and len(kept) > 1):
            return ordered, removed, elapsed

        removable = [spot for spot in kept if _spot_key(spot) not in must_keys]
        if not removable:
            return ordered, removed, elapsed

        # Prefer removing the attraction with the smallest score loss per
        # minute saved, recalculating the route after each possible removal.
        choices = []
        for spot in removable:
            candidate = [item for item in kept if _spot_key(item) != _spot_key(spot)]
            candidate_order = _order_spots(candidate, start_location, matrix)
            candidate_elapsed = _scheduled_elapsed_minutes(
                candidate_order,
                start_location,
                matrix,
                day_start_minutes,
                meal_windows,
                include_meal_time, restaurants,
            )
            saved = max(elapsed - candidate_elapsed, 1)
            score_loss = float(spot.get("match_score", 0))
            spot_key = _spot_key(spot)
            choices.append(
                (
                    0 if spot_key in closing_violators else 1,
                    score_loss / saved,
                    score_loss,
                    spot_key,
                    spot,
                )
            )
        to_remove = min(choices, key=lambda item: item[:4])[4]
        kept = [spot for spot in kept if _spot_key(spot) != _spot_key(to_remove)]
        removed.append(to_remove)


def _fit_route_to_budget(
    ordered: Sequence[Spot],
    must_keys: set,
    budget_limit: Optional[float],
    visitor_number: int,
) -> Tuple[List[Spot], List[Spot], float]:
    """Remove low-value optional attractions until visit cost fits the budget.

    8.30 口径：预算约束按「门票 + 讲解」（_visit_cost）；删除排序仍按门票
    性价比（讲解为附加项，不改变「先删门票贵/分低的景点」的次序）。
    """
    kept = list(ordered)
    removed: List[Spot] = []
    while budget_limit is not None and _visit_cost(kept, visitor_number) > budget_limit:
        removable = [spot for spot in kept if _spot_key(spot) not in must_keys]
        if not removable:
            break
        paid = [spot for spot in removable if _price(spot) > 0]
        if not paid:
            break
        to_remove = min(
            paid,
            key=lambda spot: (
                float(spot.get("match_score", 0))
                / (_price(spot) * visitor_number),
                float(spot.get("match_score", 0)),
                _spot_key(spot),
            ),
        )
        kept = [spot for spot in kept if _spot_key(spot) != _spot_key(to_remove)]
        removed.append(to_remove)
    return kept, removed, _visit_cost(kept, visitor_number)


def _refill_route_with_feasible_spots(
    ordered: Sequence[Spot],
    optional_spots: Sequence[Spot],
    daily_limit: int,
    start_location: Optional[Location],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    budget_limit: Optional[float],
    visitor_number: int,
    restaurants=None,
) -> Tuple[List[Spot], List[Spot], int]:
    """Fill time released by removals with the best feasible attractions.

    Every remaining attraction is tested at every insertion position. The
    schedule calculation includes visits, graph transport time and opening
    waits, so an accepted insertion is guaranteed to fit the daily limit and
    (轻量版) to finish each spot before its closing time.
    """
    current_route = list(ordered)
    current_keys = {_spot_key(spot) for spot in current_route}
    remaining = [
        spot for spot in optional_spots if _spot_key(spot) not in current_keys
    ]
    inserted: List[Spot] = []

    def closing_violations(route: Sequence[Spot]) -> bool:
        """插入后是否有景点计划结束超过闭馆时间（轻量版判据）。"""
        raw_events, _ = _build_schedule_events(
            route, matrix, day_start_minutes, meal_windows, restaurants
        )
        for event in raw_events:
            if event["type"] != "spot":
                continue
            spot = next(
                (s for s in route if _spot_key(s) == event["details"]["spot_id"]),
                None,
            )
            if spot is None:
                continue
            if event["end_minutes"] > _parse_time(
                spot.get("closing_time", "24:00")
            ):
                return True
        return False

    while remaining:
        current_elapsed = _scheduled_elapsed_minutes(
            current_route,
            start_location,
            matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time, restaurants,
        )
        feasible_choices = []
        for spot in remaining:
            for position in range(len(current_route) + 1):
                candidate_route = [
                    *current_route[:position],
                    spot,
                    *current_route[position:],
                ]
                candidate_elapsed = _scheduled_elapsed_minutes(
                    candidate_route,
                    start_location,
                    matrix,
                    day_start_minutes,
                    meal_windows,
                    include_meal_time, restaurants,
                )
                if candidate_elapsed > daily_limit:
                    continue
                if closing_violations(candidate_route):
                    continue  # 轻量版：插入后不得有景点超闭馆
                if (
                    budget_limit is not None
                    and _visit_cost(candidate_route, visitor_number) > budget_limit
                ):
                    continue
                added_minutes = candidate_elapsed - current_elapsed
                score = float(spot.get("match_score", 0))
                utilization = candidate_elapsed / daily_limit
                reaches_target = utilization >= TARGET_DAY_UTILIZATION
                if reaches_target:
                    ranking = (1, score, candidate_elapsed, -added_minutes)
                else:
                    ranking = (0, candidate_elapsed, score, -added_minutes)
                feasible_choices.append(
                    (ranking, _spot_key(spot), position, spot, candidate_route)
                )

        if not feasible_choices:
            break
        _, selected_key, _, selected_spot, best_route = max(
            feasible_choices,
            key=lambda item: (item[0], item[1]),
        )
        current_route = best_route
        inserted.append(selected_spot)
        remaining = [spot for spot in remaining if _spot_key(spot) != selected_key]

    elapsed = _scheduled_elapsed_minutes(
        current_route,
        start_location,
        matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time, restaurants,
    )
    return current_route, inserted, elapsed


def _fine_tune_route(
    ordered: Sequence[Spot],
    optional_spots: Sequence[Spot],
    must_keys: set,
    daily_limit: int,
    start_location: Optional[Location],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    budget_limit: Optional[float],
    visitor_number: int,
    max_iterations: Optional[int] = None,
    max_pool: Optional[int] = None,
    restaurants=None,
    min_spots: int = 0,
) -> Tuple[List[Spot], int]:
    """Improve a feasible route toward a fuller, higher-scoring schedule.

    Applies single-insert, 1-for-1 swap and 1-for-2 swap moves, accepting the
    best move of each iteration that strictly raises :func:`_route_rank` while
    elapsed time and ticket cost stay within their hard limits. Must-visit
    spots are never removed and every candidate is re-ordered so transport
    time stays minimal.

    ``max_pool`` limits the insertion pool to the highest-scoring spots, which
    keeps the quadratic 1-for-2 move cheap when many candidates are available.
    """
    def total_score(route):
        return sum(float(spot.get("match_score", 0)) for spot in route)

    def closing_violations(route):
        """路线里是否有景点计划结束超过闭馆时间（轻量版判据）。"""
        raw_events, _ = _build_schedule_events(
            route, matrix, day_start_minutes, meal_windows, restaurants
        )
        for event in raw_events:
            if event["type"] != "spot":
                continue
            spot = next(
                (s for s in route if _spot_key(s) == event["details"]["spot_id"]),
                None,
            )
            if spot is None:
                continue
            if event["end_minutes"] > _parse_time(
                spot.get("closing_time", "24:00")
            ):
                return True
        return False

    def evaluate(candidate_set):
        candidate = _order_spots(candidate_set, start_location, matrix)
        elapsed = _scheduled_elapsed_minutes(
            candidate,
            start_location,
            matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time, restaurants,
        )
        if elapsed > daily_limit:
            return None
        if closing_violations(candidate):
            return None  # 轻量版：不接受超闭馆组合
        if (
            budget_limit is not None
            and _visit_cost(candidate, visitor_number) > budget_limit
        ):
            return None
        rank = _route_rank(
            elapsed, total_score(candidate), daily_limit, len(candidate), min_spots
        )
        return candidate, elapsed, rank

    input_route = list(ordered)

    def feasible(route):
        if (
            _scheduled_elapsed_minutes(
                route,
                start_location,
                matrix,
                day_start_minutes,
                meal_windows,
                include_meal_time, restaurants,
            )
            > daily_limit
        ):
            return False
        if budget_limit is not None and _visit_cost(route, visitor_number) > budget_limit:
            return False
        return True

    # A transport-minimal reorder can shift meal windows and overflow even
    # when the caller's order is feasible, so keep that order as the fallback.
    reordered = _order_spots(input_route, start_location, matrix)
    if feasible(reordered):
        current = reordered
    else:
        current = input_route
    current_elapsed = _scheduled_elapsed_minutes(
        current,
        start_location,
        matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time, restaurants,
    )
    if current_elapsed > daily_limit or (
        budget_limit is not None and _visit_cost(current, visitor_number) > budget_limit
    ):
        # Fine-tuning only refines feasible routes; fitting is a separate pass.
        return current, current_elapsed
    current_rank = _route_rank(
        current_elapsed, total_score(current), daily_limit, len(current), min_spots
    )

    iterations = 0
    while True:
        iterations += 1
        if max_iterations is not None and iterations > max_iterations:
            break
        current_keys = {_spot_key(item) for item in current}
        pool = [
            spot for spot in optional_spots if _spot_key(spot) not in current_keys
        ]
        if max_pool is not None and len(pool) > max_pool:
            pool = sorted(
                pool,
                key=lambda spot: float(spot.get("match_score", 0)),
                reverse=True,
            )[:max_pool]
        removable = [spot for spot in current if _spot_key(spot) not in must_keys]
        best_move = None

        # 0-for-1: insert a single pool spot.
        for inp in pool:
            candidate = evaluate([*current, inp])
            if candidate is not None and candidate[2] > current_rank:
                if best_move is None or candidate[2] > best_move[2]:
                    best_move = candidate

        # 1-for-1: replace one removable spot with one pool spot.
        for out in removable:
            out_key = _spot_key(out)
            for inp in pool:
                candidate_set = [
                    spot for spot in current if _spot_key(spot) != out_key
                ] + [inp]
                candidate = evaluate(candidate_set)
                if candidate is not None and candidate[2] > current_rank:
                    if best_move is None or candidate[2] > best_move[2]:
                        best_move = candidate

        # 1-for-2: replace one removable spot with two pool spots.
        for out in removable:
            out_key = _spot_key(out)
            base = [spot for spot in current if _spot_key(spot) != out_key]
            for left in range(len(pool)):
                for right in range(left + 1, len(pool)):
                    candidate = evaluate([*base, pool[left], pool[right]])
                    if candidate is not None and candidate[2] > current_rank:
                        if best_move is None or candidate[2] > best_move[2]:
                            best_move = candidate

        if best_move is None:
            break
        current, current_elapsed, _ = best_move
        current_rank = best_move[2]

    return current, current_elapsed
