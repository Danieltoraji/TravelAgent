"""Backward-compatible facade for the split ``algorithoms`` modules.

The original single-file ``route.py`` has been split into focused modules:

- ``_common.py``            共享原语（景点/费用/预算/时间解析）。
- ``route_ordering.py``     路线排序（最近邻、2-opt、交通边与距离）。
- ``timeline.py``           时间轴（事件构建与计入/总时长）。
- ``repair.py``             校验修复（容量/预算拟合与回填）。
- ``spot_assignment.py``    景点分配（必去景点跨天分配）。
- ``planner.py``            单日/多日/候选路线生成。

This module keeps the original public API so existing callers (``main.py``,
tests and ``route.route(...)``) continue to work unchanged.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms.planner import generate_route_candidates, plan_multi_day, plan_one_day
from algorithoms.spot_assignment import assign_must_spots_to_days
from data_transmission.meal import DEFAULT_MEAL_WINDOWS

__all__ = [
    "assign_must_spots_to_days",
    "generate_route_candidates",
    "plan_multi_day",
    "plan_one_day",
    "route",
]


def route(
    requirement,
    candidate_spots,
    start_location=None,
    day_start_time="09:00",
    travel_time_provider=None,
    meal_windows=DEFAULT_MEAL_WINDOWS,
):
    """Backward-friendly entry point for callers that expect route.route(...)."""
    return plan_one_day(
        requirement,
        candidate_spots,
        start_location=start_location,
        day_start_time=day_start_time,
        travel_time_provider=travel_time_provider,
        meal_windows=meal_windows,
    )
