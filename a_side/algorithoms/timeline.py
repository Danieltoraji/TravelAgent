"""Timeline construction: schedule events and counted/elapsed minutes."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms._common import Location, Spot, _duration, _parse_time, _spot_key
from data_transmission.meal import MealWindow
from transport.providers import TravelTimeMatrix

# 用餐提前的最大干等容忍（分钟）：下一个景点会跨过用餐窗口结束、且当前离窗口
# 开始还超过该值时，先游览景点、稍后按宽限期延后或跳过用餐——避免「09:00 到
# 餐厅却要干等到 11:00」这种空等。
MAX_MEAL_WAIT = 45

# 用餐窗口的宽限期（分钟）：到达餐厅晚于窗口结束、但仍在「窗口 + 宽限期」内时
# 照常安排（延后用餐）；只有超过窗口 + 宽限期才跳过（保留「午餐未安排」提示）。
# 例如午餐 11:00–13:00，宽限期 60 → 最晚 14:00 仍正常吃午饭。
MEAL_GRACE_MINUTES = 60


def _scheduled_elapsed_minutes(
    ordered: Sequence[Spot],
    start_location: Optional[Location],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    restaurants=None,
) -> int:
    events, _ = _build_schedule_events(
        ordered, matrix, day_start_minutes, meal_windows, restaurants
    )
    return sum(
        event["end_minutes"] - event["start_minutes"]
        for event in events
        if include_meal_time or event["type"] != "meal"
    )


def _build_schedule_events(
    ordered: Sequence[Spot],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    restaurants=None,
):
    """Build raw timeline events including travel, opening waits and meals.

    When ``restaurants`` (a ``RestaurantResolver``) is provided, every meal is
    anchored to the attraction the traveller is at, a restaurant is chosen
    (nearest, or cuisine-matching when food preferences exist), and the two
    transport legs to/from the restaurant are emitted and counted as transport
    time. Without it, meals stay abstract with no extra transport.
    """
    events = []
    warnings = []
    current_minutes = day_start_minutes
    # (id, name) of the traveller's current location; a spot id or a
    # restaurant id once a meal has been taken.
    current_node = None
    remaining_meals = list(sorted(meal_windows, key=lambda meal: meal.window_start_minutes))

    def edge_between(from_id, to_id):
        if from_id is None or from_id == to_id:
            return None
        if restaurants is not None and (
            restaurants.is_restaurant(from_id) or restaurants.is_restaurant(to_id)
        ):
            return restaurants.travel_edge(from_id, to_id)
        return matrix.get(from_id, to_id)

    def add_meal(meal: MealWindow):
        nonlocal current_minutes, current_node
        anchor_id = current_node[0] if current_node is not None else None
        restaurant = None
        if restaurants is not None and anchor_id is not None:
            restaurant = restaurants.select(anchor_id)

        start_minutes = current_minutes
        if start_minutes < meal.window_start_minutes:
            start_minutes = meal.window_start_minutes

        # Transport to the restaurant (if any) is part of the meal start time,
        # so it must fit inside the window too.
        transport_minutes = 0
        transport_edge = None
        if restaurant is not None and anchor_id is not None:
            transport_edge = restaurants.travel_edge(anchor_id, restaurant.id)
            transport_minutes = (
                transport_edge.transport_minutes if transport_edge is not None else 0
            )

        if start_minutes + transport_minutes > meal.window_end_minutes + MEAL_GRACE_MINUTES:
            warnings.append(f"{meal.name}未安排：到达餐厅时已超过用餐窗口（含宽限期）")
            return

        if start_minutes > current_minutes:
            events.append(
                {
                    "type": "waiting",
                    "name": f"等待{meal.name}时间",
                    "start_minutes": current_minutes,
                    "end_minutes": start_minutes,
                    "details": {"reason": "meal_window"},
                }
            )
            current_minutes = start_minutes

        # 去餐厅
        if transport_minutes:
            events.append(
                {
                    "type": "transport",
                    "name": f"{current_node[1]} → {restaurant.name}",
                    "start_minutes": current_minutes,
                    "end_minutes": current_minutes + transport_minutes,
                    "details": {
                        "from": current_node[1],
                        "to": restaurant.name,
                        "distance_km": (
                            round(transport_edge.distance_km, 2)
                            if transport_edge is not None
                            else 0.0
                        ),
                    },
                }
            )
            current_minutes += transport_minutes
        if restaurant is not None:
            current_node = (restaurant.id, restaurant.name)

        details = {
            "window_start_minutes": meal.window_start_minutes,
            "window_end_minutes": meal.window_end_minutes,
        }
        if restaurant is not None:
            details.update(
                {
                    "restaurant_id": restaurant.id,
                    "restaurant_name": restaurant.name,
                    "cuisine": list(restaurant.cuisine_tags),
                    "average_cost": restaurant.average_cost,
                }
            )
        events.append(
            {
                "type": "meal",
                "name": meal.name,
                "start_minutes": current_minutes,
                "end_minutes": current_minutes + meal.duration_minutes,
                "details": details,
            }
        )
        current_minutes += meal.duration_minutes

    for index, spot in enumerate(ordered, start=1):
        spot_id = _spot_key(spot)
        spot_name = spot.get("name", spot_id)
        opening_minutes = _parse_time(spot.get("opening_time", "00:00"))

        def projected_spot_end():
            from_id = current_node[0] if current_node is not None else None
            edge = edge_between(from_id, spot_id)
            transport = edge.transport_minutes if edge is not None else 0
            return max(current_minutes + transport, opening_minutes) + _duration(spot)

        # If the next indivisible visit would cross the end of a meal window,
        # take that meal before departing for the attraction — unless doing so
        # would mean idling for more than MAX_MEAL_WAIT before the window even
        # opens (then sightsee first; the meal is deferred/skipped later).
        while (
            remaining_meals
            and projected_spot_end() > remaining_meals[0].window_end_minutes
            and (
                remaining_meals[0].window_start_minutes - current_minutes
                <= MAX_MEAL_WAIT
            )
        ):
            add_meal(remaining_meals.pop(0))

        from_id = current_node[0] if current_node is not None else None
        edge = edge_between(from_id, spot_id)
        transport_minutes = edge.transport_minutes if edge is not None else 0
        if transport_minutes:
            events.append(
                {
                    "type": "transport",
                    "name": f"{current_node[1]} → {spot_name}",
                    "start_minutes": current_minutes,
                    "end_minutes": current_minutes + transport_minutes,
                    "details": {
                        "from": current_node[1],
                        "to": spot_name,
                        "distance_km": round(edge.distance_km, 2),
                    },
                }
            )
            current_minutes += transport_minutes

        if current_minutes < opening_minutes:
            events.append(
                {
                    "type": "waiting",
                    "name": f"等待{spot_name}开放",
                    "start_minutes": current_minutes,
                    "end_minutes": opening_minutes,
                    "details": {"spot_id": spot_id},
                }
            )
            current_minutes = opening_minutes

        departure_minutes = current_minutes + _duration(spot)
        closing_minutes = _parse_time(spot.get("closing_time", "24:00"))
        if departure_minutes > closing_minutes:
            warnings.append(f"{spot_name} 的计划结束时间超过闭馆时间")
        events.append(
            {
                "type": "spot",
                "name": spot_name,
                "start_minutes": current_minutes,
                "end_minutes": departure_minutes,
                "details": {
                    "order": index,
                    "spot_id": spot_id,
                    "match_score": spot.get("match_score"),
                    "opening_time": spot.get("opening_time"),
                    "closing_time": spot.get("closing_time"),
                },
            }
        )
        current_minutes = departure_minutes
        current_node = (spot_id, spot_name)

        while (
            remaining_meals
            and remaining_meals[0].window_start_minutes <= current_minutes
        ):
            add_meal(remaining_meals.pop(0))

    # A meal is needed only if the sightseeing day reaches its window.
    while remaining_meals and remaining_meals[0].window_start_minutes <= current_minutes:
        add_meal(remaining_meals.pop(0))
    return events, warnings
