"""Meal-window configuration shared by itinerary planners."""

from dataclasses import dataclass
from typing import Tuple


@dataclass(frozen=True)
class MealWindow:
    name: str
    window_start_minutes: int
    window_end_minutes: int
    duration_minutes: int

    def __post_init__(self):
        if not self.name:
            raise ValueError("用餐名称不能为空")
        if not 0 <= self.window_start_minutes <= self.window_end_minutes:
            raise ValueError(f"{self.name}的用餐时间窗口无效")
        if self.duration_minutes <= 0:
            raise ValueError(f"{self.name}的持续时间必须大于 0")


DEFAULT_MEAL_WINDOWS: Tuple[MealWindow, ...] = (
    MealWindow("午餐", 11 * 60, 13 * 60, 60),
    MealWindow("晚餐", 17 * 60 + 30, 19 * 60 + 30, 60),
)
