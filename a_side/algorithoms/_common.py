"""Shared primitives for spot reading, cost, budget and requirement parsing.

These helpers are used by every other ``algorithoms`` module, so they live in
their own leaf module to avoid import cycles.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence, Tuple


Spot = Dict[str, Any]
Location = Tuple[float, float]
TARGET_DAY_UTILIZATION = 0.85


def _route_rank(
    elapsed: int,
    score: float,
    capacity: int,
    spot_count: int = 0,
    min_spots: int = 0,
) -> tuple:
    """Rank a feasible route so every stage optimizes the same objective.

    A day that reaches ``TARGET_DAY_UTILIZATION`` is ranked by preference
    first; below that threshold, closing the idle-time gap takes priority.
    ``min_spots`` (default 0, i.e. no constraint) makes any route that meets
    the minimum attraction count rank strictly above one that does not, so a
    "full day" is never allowed to collapse to a single attraction. Larger
    tuples are better, matching the historical knapsack ordering.
    """
    utilization = elapsed / capacity if capacity else 1.0
    meets_min = 1 if spot_count >= min_spots else 0
    if utilization >= TARGET_DAY_UTILIZATION:
        # Once the day is sufficiently full, user preference is primary.
        return (meets_min, 1, score, elapsed, spot_count)
    # Below the target, closing the idle-time gap is primary.
    return (meets_min, 0, elapsed, score, spot_count)


def _spot_key(spot: Spot) -> str:
    """Return a stable key so must-see and scored lists can be de-duplicated."""
    return str(spot.get("id") or spot.get("name"))


def _duration(spot: Spot) -> int:
    value = spot.get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"景点 {spot.get('name', '<unknown>')} 缺少有效的 duration")
    value = int(value)
    if value <= 0:
        raise ValueError(f"景点 {spot.get('name', '<unknown>')} 的 duration 必须大于 0")
    return value


def _price(spot: Spot) -> float:
    """Return one visitor's ticket price; missing prices are free for compatibility."""
    value = spot.get("price", 0)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"景点 {spot.get('name', '<unknown>')} 缺少有效的 price")
    if value < 0:
        raise ValueError(f"景点 {spot.get('name', '<unknown>')} 的 price 不能小于 0")
    return float(value)


def _ticket_cost(spots: Sequence[Spot], visitor_number: int) -> float:
    return sum(_price(spot) * visitor_number for spot in spots)


def _budget_context(requirement: Dict[str, Any]) -> Tuple[Optional[float], int]:
    """Read total budget and party size, preserving older programmatic callers."""
    content = requirement.get("content", {})
    constraints = content.get("constraints", {})
    budget = constraints.get("budget")
    visitor_number = content.get("visitor_number", 1)
    if budget is not None:
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or budget < 0:
            raise ValueError("budget 必须是非负金额")
        budget = float(budget)
    if (
        isinstance(visitor_number, bool)
        or not isinstance(visitor_number, (int, float))
        or int(visitor_number) != visitor_number
        or visitor_number <= 0
    ):
        raise ValueError("visitor_number 必须是正整数")
    return budget, int(visitor_number)


def _parse_time(value: str) -> int:
    try:
        hour_text, minute_text = value.split(":", 1)
        hour, minute = int(hour_text), int(minute_text)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"无效时间格式：{value!r}，应为 HH:MM") from exc
    if not (0 <= hour <= 24 and 0 <= minute < 60) or (hour == 24 and minute != 0):
        raise ValueError(f"无效时间：{value!r}")
    return hour * 60 + minute


def _food_preferences(requirement: Dict[str, Any]) -> list:
    """Read optional food preferences; absent values mean no food requirement."""
    try:
        preferences = requirement["content"]["preferences"]
        raw = preferences.get("food_preferences") or preferences.get("food_tags") or []
    except (KeyError, TypeError):
        return []
    return [str(item).strip() for item in raw if str(item).strip()]


def _include_meal_time_in_daily_limit(requirement: Dict[str, Any]) -> bool:
    """Read the user's meal-time accounting choice.

    Requirements produced by the LLM must contain this field. Missing values
    default to the historical behavior for backward compatibility with older
    programmatic callers and saved test fixtures.
    """
    try:
        constraints = requirement["content"]["constraints"]
    except (KeyError, TypeError) as exc:
        raise ValueError("requirement 缺少 constraints") from exc
    value = constraints.get("include_meal_time_in_daily_limit", False)
    if value is None:
        raise ValueError("请确认用餐时间是否计入每日出游时长")
    if not isinstance(value, bool):
        raise ValueError("include_meal_time_in_daily_limit 必须是布尔值")
    return value
