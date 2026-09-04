"""P4 拆分：BPlannerHook 的职责 mixin 包。

- ``TripSegmentAttacher``：城际来去程段构建/注入/归一化（planner_parts/trip_segments.py）
- ``RestaurantOrchestrator``：真源餐厅两阶段矩阵 + 聚类编排（planner_parts/restaurants.py）
- ``HotelAttacher``：住宿选择/真源酒店池（planner_parts/hotels.py）
- ``DataSourceResolver``：live/fake/live_fallback 三态判定与回退（planner_parts/data_source.py）

``call_llm.b_planner_hook.BPlannerHook`` 继承本包各 mixin，只剩编排。
"""

from call_llm.planner_parts.data_source import DataSourceResolver  # noqa: F401
from call_llm.planner_parts.hotels import HotelAttacher  # noqa: F401
from call_llm.planner_parts.restaurants import (  # noqa: F401
    RestaurantOrchestrator,
    _collect_meal_anchors,
    _collect_plan_spot_names,
)
from call_llm.planner_parts.trip_segments import (  # noqa: F401
    TripSegmentAttacher,
    _first_day_start_from_segments,
    _last_day_end_from_segments,
    _realize_outbound_with_schedule,
    _rebuild_return_with_schedule,
    _select_outbound_combination,
    _select_return_combination,
    _windowed_last_day_end,
)

__all__ = [
    "DataSourceResolver",
    "HotelAttacher",
    "RestaurantOrchestrator",
    "TripSegmentAttacher",
    "_collect_meal_anchors",
    "_collect_plan_spot_names",
    "_first_day_start_from_segments",
    "_last_day_end_from_segments",
    "_realize_outbound_with_schedule",
    "_rebuild_return_with_schedule",
    "_select_outbound_combination",
    "_select_return_combination",
    "_windowed_last_day_end",
]