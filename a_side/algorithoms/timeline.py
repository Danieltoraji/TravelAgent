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

# A3 修复（8.29，晚餐调度死锁）：
# - 「完整一天」最少行程分钟数：行程实际时长 ≥ 该值且已过午饭固定时段 → 视为整天，
#   末景点后追加剩余餐（晚餐），与晚间（如酒店入住 20:00）衔接——不再依赖游览
#   自然推进到晚餐窗口（6h 预算的行程永远到不了 17:30，此前晚餐永不触发）。
MEAL_TAIL_MIN_FULL_DAY = 300
# 收尾补餐只对「午后固定窗口」的餐（晚餐 17:30 ≥ 13:00）生效，避免把午餐滥补；
# 与 meal_windows 的“最后一餐”语义解耦（防御：自定义窗口仅含午餐时不触发）。
MEAL_TAIL_LUNCH_END = 13 * 60


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
    complete_day: bool = False,
):
    """Build raw timeline events including travel, opening waits and meals.

    When ``restaurants`` (a ``RestaurantResolver``) is provided, every meal is
    anchored to the attraction the traveller is at, a restaurant is chosen
    (nearest, or cuisine-matching when food preferences exist), and the two
    transport legs to/from the restaurant are emitted and counted as transport
    time. Without it, meals stay abstract with no extra transport.

    ``complete_day``（A3 修复，仅**最终展示**路径传 True；预算评估/候选枚举传 False）：
    行程收尾时若已构成「完整一天」（时长 ≥ MEAL_TAIL_MIN_FULL_DAY 且已过午饭时段），
    把剩余餐（晚餐）追加到其窗口开始时刻，与晚间衔接——此前 6h 预算行程永远到不了
    晚餐窗口（17:30）导致晚餐永不触发；追加不会延长预算评估（elapsed 计算不经此路径）。
    """
    events = []
    warnings = []
    current_minutes = day_start_minutes
    # (id, name) of the traveller's current location; a spot id or a
    # restaurant id once a meal has been taken.
    current_node = None
    remaining_meals = list(sorted(meal_windows, key=lambda meal: meal.window_start_minutes))
    # 本条时间轴（一天）内已排定的餐厅 id：同天跨顿去重（8.31 P0）。
    # 只在单次构建的局部作用域累积——resolver 会被规划器反复试算，
    # 去重状态放实例上会跨试算泄漏。
    chosen_restaurant_ids = []

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
            restaurant = restaurants.select(
                anchor_id, exclude_ids=chosen_restaurant_ids
            )
        if restaurant is not None:
            chosen_restaurant_ids.append(restaurant.id)

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
            gap = start_minutes - current_minutes
            if gap <= MAX_MEAL_WAIT:
                events.append(
                    {
                        "type": "waiting",
                        "name": f"等待{meal.name}时间",
                        "start_minutes": current_minutes,
                        "end_minutes": start_minutes,
                        "details": {"reason": "meal_window"},
                    }
                )
            # A3（8.29）：短等待（≤MAX_MEAL_WAIT）才生成 waiting 事件；行程早结束、
            # 晚餐窗口未到时的大 gap（如 14:36 结束等 17:30 晚餐）**不生成**大段干等
            # 事件，时间轴直接锚到窗口开始时刻（间隔为隐式自由时间/休息，不渲染）。
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
                    # A4 修复（8.30）：餐厅真实坐标透传（Restaurant.location 为
                    # (lat, lng) tuple），C 端地图可标注餐厅（此前 meal 段坐标恒 0）。
                    "location": restaurant.location,
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
                    # 坐标/价格透传：B 契约 Place 需 lat/lng（C 端真源判定依据，见
                    # plan/real_api.md §3）；plan_to_trip_timeline 从这里取。
                    # 8.30 预算口径：讲解费（guide_price）一并透出，供费用汇总
                    # （_plan_cost_summary）与 C 端明细展示。
                    "location": spot.get("location"),
                    "price": spot.get("price"),
                    "guide_price": spot.get("guide_price"),
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

    # 收尾（A3 修复放宽）：已开窗 **或 临窗（≤MAX_MEAL_WAIT，如 17:00 结束等 17:30 晚餐）**
    # 的剩余餐也补上——此前只有「游览自然推进到窗口开始之后」才安排。
    while remaining_meals and remaining_meals[0].window_start_minutes <= current_minutes + MAX_MEAL_WAIT:
        add_meal(remaining_meals.pop(0))

    # A3 修复（8.29）：**完整一天**（实际行程 ≥ MEAL_TAIL_MIN_FULL_DAY 且已过午饭固定时段）
    # 时，末景点后追加剩余餐（晚餐）——6h 预算行程 09:00–14:36 就结束、永远到不了
    # 17:30 晚餐窗口，但当天还有晚间安排（如酒店入住 20:00），晚餐应照样安排；
    # 仅最终展示路径（complete_day=True）生效，预算评估/候选枚举路径不变。
    if (
        complete_day
        and remaining_meals
        and current_minutes - day_start_minutes >= MEAL_TAIL_MIN_FULL_DAY
        and remaining_meals[0].window_start_minutes >= MEAL_TAIL_LUNCH_END
    ):
        add_meal(remaining_meals.pop(0))
    return events, warnings
