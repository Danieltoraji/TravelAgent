"""RePlanner：事件驱动的增量重规划（最小化变更）。

整体分三步：
    1. ``_translate_events``      事件 → 约束翻译（已实现）
    2. ``_repair_affected_days``  增量修复：定位受影响天 → 应用事件 → 溢出则
                                  fit / refill / fine-tune（已实现）
    3. ``_build_changes``         新旧计划 diff + 可解释原因（已实现）
    ``replan``                    主入口：翻译 → 修复 → 失败兜底全量重跑 → diff

Step 1 设计（8.19 确认）：
- 事件是运行时状态，翻译成对「规划输入」的修改，不直接改当前计划：
    closed  → 从候选池移除该景点（硬不可行）
    queue   → ``spot_duration_deltas``：该景点 duration 增量（用 metrics 数值，
              不解析 detail 文本）
    weather → 事件显式指定景点时扣 match_score；未指定则无法精准翻译（数据
              无户外/室内标签），记 note
    traffic / budget → 留接口：traffic 需矩阵叠加层（Step 2 消费 travel_deltas），
              budget 需改 requirement；当前只记 note，不产出数值
- 纯函数：不修改传入的 requirement / candidate_spots / events。

Step 2 设计（8.19 确认 + 8.20 升级）：
- 定位受影响天：事件 spot（queue 增量 / closed 移除）所在的天。
- 未受影响的天原样保留（最小化打扰，diff 时这些天完全一致）。
- 受影响天：从「应用过事件翻译的候选池」还原景点（queue 时长增量 / weather 降权
  已作用到池中副本），移除 closed 景点 → 重新排程。修复优先级（8.20 起）：
    ① **错峰重排（能力 1）**：超时且有排队增量景点时，先试「增量景点挪到午后、
       全部景点保留」的序列（`_offset_peak_reorder`），可行则不删；
    ② **跨天搬移（能力 2）**：单天删了景点 / 必去放不下时，优先把景点搬到其它
       有空档的天（接收天也不删），`changes` 产出 move；必去景点搬走后当天
       腾空再修复；
    ③ **删兜底**：①② 都失败才 `_fit_route_to_daily_limit`（must 不可移除）。
- 单天修复失败（必去景点本身超时，且跨天搬移无接收天）→ 返回
  ``feasible=False`` + ``fallback_needed=True``，由 ``replan`` 全量重跑兜底。

Step 3 设计（8.19 确认）：
- 以 spot_id 对比新旧计划：removed / added / move（跨天）/ rescheduled（同天时间变化）。
- reason 模板引用事件信息（关闭 / 排队增量 / 天气降权），无事件关联时用通用约束模板。
- 同天时间变化 < 10 分钟视为微调噪音不报告。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms._common import (
    Spot,
    _budget_context,
    _include_meal_time_in_daily_limit,
    _parse_time,
    _spot_key,
    _ticket_cost,
)
from algorithoms.repair import (
    _fine_tune_route,
    _fit_route_to_daily_limit,
    _refill_route_with_feasible_spots,
)
from algorithoms.route_ordering import _order_spots
from algorithoms.select_spots import match_name
from algorithoms.timeline import _build_schedule_events, _scheduled_elapsed_minutes
from data_transmission.city_graph import DEFAULT_GRAPH_DIR
from data_transmission.itinerary import build_itinerary_node
from data_transmission.meal import DEFAULT_MEAL_WINDOWS, MealWindow
from transport.providers import JsonTravelTimeProvider, TravelTimeMatrix, TravelTimeProvider

# weather 降权（match_score 扣分）：high / medium / low
WEATHER_PENALTY_BY_SEVERITY = {"high": 30, "medium": 15, "low": 5}

# 翻译结果里备注用的固定文案
_NOTE_TRAFFIC_UNSUPPORTED = "traffic 事件翻译留接口：需在矩阵上叠加 travel_deltas（Step 2 消费）"
_NOTE_BUDGET_UNSUPPORTED = "budget 事件翻译留接口：需下调 requirement.budget"
_NOTE_WEATHER_NO_SPOT = "weather 事件未指定景点，且景点数据无户外/室内标签，暂不翻译"
_NOTE_SPOT_NOT_FOUND = "事件指定的景点在候选池中未找到，已忽略"
_NOTE_HOTEL_NO_ID = "hotel 事件缺少 metrics.hotel_id，无法翻译"
_NOTE_HOTEL_FULL = "hotel 事件满房已生效"


def _severity_penalty(severity: Optional[str]) -> int:
    """把事件的严重程度翻译成 match_score 扣分（weather 用）。"""
    return WEATHER_PENALTY_BY_SEVERITY.get(severity, 0)


def _match_spot_in_pool(
    spot_name: Optional[str], candidate_spots: Sequence[Sequence[Spot]]
) -> Optional[Spot]:
    """在候选池里按名称/别名匹配事件指定的景点（复用 select_spots.match_name）。"""
    if not spot_name:
        return None
    for group in candidate_spots:
        for spot in group:
            if match_name(spot, spot_name) in (1, 2):
                return spot
    return None


def _remove_spot_from_pool(
    candidate_spots: Sequence[Sequence[Spot]], spot: Spot
) -> List[List[Spot]]:
    """从 [must, conflict, scored] 三组里移除指定景点，返回新列表（原列表不变）。"""
    key = _spot_key(spot)
    return [
        [candidate for candidate in group if _spot_key(candidate) != key]
        for group in candidate_spots
    ]


def _spot_with_duration_delta(spot: Spot, delta_minutes: int) -> Spot:
    """返回 duration 增加 delta 分钟后的景点副本（供 Step 2 应用 queue 翻译）。"""
    adjusted = dict(spot)
    adjusted["duration"] = int(spot.get("duration", 0)) + int(delta_minutes)
    return adjusted


def _translate_queue_event(
    event: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    translation: Dict[str, Any],
) -> None:
    """queue 事件 → spot_duration_deltas（用 metrics 数值，不解析 detail 文本）。"""
    metrics = event.get("metrics") or {}
    delta = metrics.get("queue_delta_minutes")
    if delta is None:
        delta = metrics.get("queue_minutes")
    if delta is None:
        translation["notes"].append(
            "queue 事件缺少结构化 metrics（queue_delta_minutes / queue_minutes），已忽略"
        )
        return
    spot = _match_spot_in_pool(event.get("spot"), candidate_spots)
    if spot is None:
        translation["notes"].append(_NOTE_SPOT_NOT_FOUND)
        return
    key = _spot_key(spot)
    translation["spot_duration_deltas"][key] = int(delta)
    translation["notes"].append(
        f"{spot.get('name')} 排队增加 {int(delta)} 分钟，duration 将按此增量调整"
    )


def _translate_closed_event(
    event: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    translation: Dict[str, Any],
) -> None:
    """closed 事件 → 从候选池移除该景点（硬不可行）。"""
    spot = _match_spot_in_pool(event.get("spot"), candidate_spots)
    if spot is None:
        translation["notes"].append(_NOTE_SPOT_NOT_FOUND)
        return
    translation["candidate_spots"] = _remove_spot_from_pool(
        translation["candidate_spots"], spot
    )
    translation["removed_spots"].append(spot)


def _translate_weather_event(
    event: Dict[str, Any], translation: Dict[str, Any]
) -> None:
    """weather 事件 → 事件显式指定景点时扣 match_score；否则无法精准翻译。"""
    spot_name = event.get("spot")
    if not spot_name:
        translation["notes"].append(_NOTE_WEATHER_NO_SPOT)
        return
    penalty = _severity_penalty(event.get("severity"))
    if penalty <= 0:
        translation["notes"].append("weather 事件缺少有效 severity，已忽略")
        return
    spot = _match_spot_in_pool(spot_name, translation["candidate_spots"])
    if spot is None:
        translation["notes"].append(_NOTE_SPOT_NOT_FOUND)
        return
    key = _spot_key(spot)
    translation["spot_score_penalties"][key] = penalty
    translation["notes"].append(
        f"{spot.get('name')} 受天气影响，match_score 扣减 {penalty} 分"
    )


def _translate_hotel_event(
    event: Dict[str, Any], translation: Dict[str, Any]
) -> None:
    """hotel 事件 → 满房排除 / 每晚价格增量（用 metrics 结构化数值）。"""
    metrics = event.get("metrics") or {}
    hotel_id = metrics.get("hotel_id")
    if not hotel_id:
        translation["notes"].append(_NOTE_HOTEL_NO_ID)
        return
    hotel_id = str(hotel_id)
    if int(metrics.get("hotel_full") or 0):
        translation["hotel_exclude_ids"].append(hotel_id)
        translation["notes"].append(
            f"酒店 {hotel_id} 满房，从候选池排除（硬不可行）"
        )
    if metrics.get("price_delta") is not None:
        delta = float(metrics["price_delta"])
        translation["hotel_price_deltas"][hotel_id] = (
            translation["hotel_price_deltas"].get(hotel_id, 0.0) + delta
        )
        translation["notes"].append(
            f"酒店 {hotel_id} 每晚价格变化 {delta:+.0f} 元"
        )


def _translate_events(
    requirement: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    events: Sequence[Dict[str, Any]],
) -> Dict[str, Any]:
    """把变化事件翻译成对规划输入的修改（Step 1，纯函数）。

    返回的 Translation（dict）：
        requirement         透传原 requirement（budget 翻译未实现时不修改）
        candidate_spots     可能已移除 closed 景点（[must, conflict, scored]）
        spot_duration_deltas   {spot_key: 分钟}  queue 排队时长增量
        spot_score_penalties   {spot_key: 分}     weather 降权
        travel_deltas          {}                 traffic 留接口（Step 2 消费）
        removed_spots          [Spot]             closed 移除的景点
        hotel_exclude_ids      [str]              hotel 满房排除的酒店 id
        hotel_price_deltas     {hotel_id: 元}     hotel 每晚价格增量
        notes                  [str]              翻译说明 / 未支持项

    不修改传入对象；同一景点被多个事件命中时，各翻译累加进对应映射。
    """
    translation: Dict[str, Any] = {
        "requirement": requirement,
        "candidate_spots": [list(group) for group in candidate_spots],
        "spot_duration_deltas": {},
        "spot_score_penalties": {},
        "travel_deltas": {},
        "removed_spots": [],
        "hotel_exclude_ids": [],
        "hotel_price_deltas": {},
        "notes": [],
    }

    for event in events:
        event_type = event.get("event_type")
        if event_type == "queue":
            _translate_queue_event(event, translation["candidate_spots"], translation)
        elif event_type == "closed":
            _translate_closed_event(event, translation["candidate_spots"], translation)
        elif event_type == "weather":
            _translate_weather_event(event, translation)
        elif event_type == "traffic":
            translation["notes"].append(_NOTE_TRAFFIC_UNSUPPORTED)
        elif event_type == "budget":
            translation["notes"].append(_NOTE_BUDGET_UNSUPPORTED)
        elif event_type == "hotel":
            _translate_hotel_event(event, translation)
        else:
            translation["notes"].append(f"未知事件类型 {event_type!r}，已忽略")

    return translation


# ---------------------------------------------------------------------------
# Step 2：增量修复（_repair_affected_days）
# ---------------------------------------------------------------------------


def _day_spot_nodes(day: Dict[str, Any]) -> List[Dict[str, Any]]:
    """某天 route_details 里的 spot 节点（保持参观顺序）。"""
    return [node for node in day.get("route_details", []) if node.get("type") == "spot"]


def _day_must_keys(day: Dict[str, Any]) -> Set[str]:
    """某天 is_must_visit=True 的 spot_id 集合（修复时不可移除）。"""
    return {
        node.get("details", {}).get("spot_id")
        for node in _day_spot_nodes(day)
        if node.get("details", {}).get("is_must_visit")
    }


def _pool_by_key(candidate_spots: Sequence[Sequence[Spot]]) -> Dict[str, Spot]:
    return {
        _spot_key(spot): spot for group in candidate_spots for spot in group
    }


def _restore_day_spots(
    day: Dict[str, Any],
    pool_by_key: Dict[str, Spot],
    removed_keys: Set[str],
) -> List[Spot]:
    """把某天 route_details 的 spot 节点还原成完整 spot dict 序列。

    closed 移除的景点（removed_keys）直接跳过；计划里找不到对应 spot dict
    （不在修改后候选池）的节点也跳过，避免修复时引用已失效的景点。
    """
    restored: List[Spot] = []
    for node in _day_spot_nodes(day):
        key = node.get("details", {}).get("spot_id")
        if key in removed_keys:
            continue
        spot = pool_by_key.get(key)
        if spot is None:
            continue
        restored.append(spot)
    return restored


def _optional_pool_for_day(
    candidate_spots: Sequence[Sequence[Spot]],
    day_index: int,
    day_spots: Sequence[Sequence[Spot]],
) -> List[Spot]:
    """该天可补入的可选景点：候选池 scored 中未被其它天使用的。"""
    scored = candidate_spots[2] if len(candidate_spots) > 2 else []
    used_elsewhere: Set[str] = set()
    for other_index, other_spots in enumerate(day_spots):
        if other_index == day_index:
            continue
        used_elsewhere.update(_spot_key(spot) for spot in other_spots)
    return [spot for spot in scored if _spot_key(spot) not in used_elsewhere]


def _spots_close_ok(
    ordered: Sequence[Spot],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    restaurants,
) -> bool:
    """轻量闭馆检查：任一景点的计划结束时间不得超过其闭馆时间。"""
    closing_by_key = {
        _spot_key(spot): _parse_time(spot.get("closing_time", "24:00"))
        for spot in ordered
    }
    events, _ = _build_schedule_events(
        ordered, matrix, day_start_minutes, meal_windows, restaurants
    )
    for event in events:
        if event["type"] != "spot":
            continue
        closing = closing_by_key.get(event["details"]["spot_id"])
        if closing is not None and event["end_minutes"] > closing:
            return False
    return True


def _offset_peak_reorder(
    ordered: Sequence[Spot],
    deltas: Dict[str, int],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    restaurants,
    daily_limit: int,
) -> Optional[List[Spot]]:
    """事件感知错峰（能力 1）：把排队增量大的景点优先安排到午后。

    溢出时先试「全部景点保留」的错峰序列（增量景点按增量降序放在下午段），
    任一变体不超时且不超闭馆即采用；找不到返回 None（交给跨天搬移 / 删兜底）。
    无排队增量时直接返回 None（行为与 8.19 完全一致）。
    """
    delta_keys = {
        _spot_key(spot): int(deltas.get(_spot_key(spot), 0) or 0) for spot in ordered
    }
    heavy = [spot for spot in ordered if delta_keys[_spot_key(spot)] > 0]
    if not heavy:
        return None
    heavy_sorted = sorted(
        heavy, key=lambda spot: delta_keys[_spot_key(spot)], reverse=True
    )
    heavy_keys = {_spot_key(spot) for spot in heavy_sorted}
    light = [spot for spot in ordered if _spot_key(spot) not in heavy_keys]

    variants: List[List[Spot]] = []
    # A：上午（其它景点，交通优化）+ 下午（增量景点，交通优化）
    if light and heavy_sorted:
        variants.append(
            [
                *_order_spots(light, None, matrix),
                *_order_spots(heavy_sorted, None, matrix),
            ]
        )
    # B：原序稳定置尾（增量景点按增量降序挪到末尾）
    variants.append([*light, *heavy_sorted])

    for variant in variants:
        elapsed = _scheduled_elapsed_minutes(
            variant, None, matrix, day_start_minutes, meal_windows,
            include_meal_time, restaurants,
        )
        if elapsed <= daily_limit and _spots_close_ok(
            variant, matrix, day_start_minutes, meal_windows, restaurants
        ):
            return variant
    return None


def _repair_day(
    ordered: Sequence[Spot],
    must_keys: Set[str],
    optional_pool: Sequence[Spot],
    daily_limit: int,
    budget_limit: Optional[float],
    visitor_number: int,
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    restaurants,
    fine_tune_max_pool: Optional[int],
    deltas: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[List[Spot], int, List[Spot], List[Spot]]]:
    """修复一天：重排 →（溢出）事件感知错峰 → fit → refill → fine-tune。

    返回 ``(ordered, elapsed, refilled, removed_for_time)``；必去景点本身超时
    返回 ``None``（该天无法增量修复，需要跨天搬移 / 兜底）。

    ``deltas``（8.20 新增）：queue 排队增量 {spot_id: 分钟}，溢出时先尝试
    ``_offset_peak_reorder`` 错峰重排（保留全部景点），失败才 fit 删景点。
    """
    deltas = deltas or {}
    current = list(ordered)

    # 1. 交通最短重排；若导致用餐窗口溢出则保留原序（与 repair.py 同策略）。
    reordered = _order_spots(current, None, matrix)
    if (
        _scheduled_elapsed_minutes(
            reordered, None, matrix, day_start_minutes, meal_windows,
            include_meal_time, restaurants,
        )
        <= daily_limit
    ):
        current = reordered

    elapsed = _scheduled_elapsed_minutes(
        current, None, matrix, day_start_minutes, meal_windows,
        include_meal_time, restaurants,
    )
    removed_for_time: List[Spot] = []
    if elapsed > daily_limit:
        # 2. 事件感知错峰：把排队增量景点挪到午后，保留全部景点（8.20 新增）。
        offset = _offset_peak_reorder(
            current, deltas, matrix, day_start_minutes, meal_windows,
            include_meal_time, restaurants, daily_limit,
        )
        if offset is not None:
            current = offset
            elapsed = _scheduled_elapsed_minutes(
                current, None, matrix, day_start_minutes, meal_windows,
                include_meal_time, restaurants,
            )
        else:
            # 3. 删兜底：超时（或超闭馆）砍掉收益最低的可选景点（must 保护）。
            current, removed_for_time, elapsed = _fit_route_to_daily_limit(
                current, must_keys, daily_limit, None, matrix, day_start_minutes,
                meal_windows, include_meal_time, restaurants,
            )
        if elapsed > daily_limit:
            return None  # 必去景点本身超时，天内修复不了

    # 2. 用候选池补空（保证插入后仍可行）。
    current, refilled, elapsed = _refill_route_with_feasible_spots(
        current,
        optional_pool,
        daily_limit,
        None,
        matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time,
        budget_limit,
        visitor_number,
        restaurants=restaurants,
    )

    # 3. 微调（插入 / 1-for-1 / 1-for-2，严格提升 _route_rank）。
    current, elapsed = _fine_tune_route(
        current,
        optional_pool,
        must_keys,
        daily_limit,
        None,
        matrix,
        day_start_minutes,
        meal_windows,
        include_meal_time,
        budget_limit,
        visitor_number,
        restaurants=restaurants,
        max_pool=fine_tune_max_pool,
    )
    return current, elapsed, refilled, removed_for_time


def _build_day_output(
    day_number: int,
    ordered: Sequence[Spot],
    must_keys: Set[str],
    matrix: TravelTimeMatrix,
    day_start_minutes: int,
    meal_windows: Sequence[MealWindow],
    include_meal_time: bool,
    restaurants,
    provider: TravelTimeProvider,
    daily_limit: int,
    refilled: Sequence[Spot],
    removed_for_time: Sequence[Spot],
    visitor_number: int,
) -> Dict[str, Any]:
    """把修复后的景点序列重建为与 plan 输出一致的 day dict。"""
    raw_events, schedule_warnings = _build_schedule_events(
        ordered, matrix, day_start_minutes, meal_windows, restaurants
    )
    route_details = []
    for event in raw_events:
        details = dict(event["details"])
        if event["type"] == "spot":
            details["is_must_visit"] = details["spot_id"] in must_keys
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

    total_visit = sum(
        node["duration_minutes"] for node in route_details if node["type"] == "spot"
    )
    total_transport = sum(
        node["duration_minutes"]
        for node in route_details
        if node["type"] == "transport"
    )
    total_waiting = sum(
        node["duration_minutes"] for node in route_details if node["type"] == "waiting"
    )
    total_meal = sum(
        node["duration_minutes"] for node in route_details if node["type"] == "meal"
    )
    total_elapsed = sum(node["duration_minutes"] for node in route_details)
    total_counted = total_elapsed - (0 if include_meal_time else total_meal)

    return {
        "day": day_number,
        "route_details": route_details,
        "total_match_score": sum(float(spot.get("match_score", 0)) for spot in ordered),
        "total_counted_minutes": total_counted,
        "total_elapsed_minutes": total_elapsed,
        "total_visit_minutes": total_visit,
        "total_transport_minutes": total_transport,
        "total_waiting_minutes": total_waiting,
        "total_meal_minutes": total_meal,
        "estimated_ticket_cost": _ticket_cost(ordered, visitor_number),
        "is_overtime": total_counted > daily_limit,
        "time_overflow_minutes": max(total_counted - daily_limit, 0),
        "refilled_spots": [spot.get("name") for spot in refilled],
        "removed_spots": [spot.get("name") for spot in removed_for_time],
        "warnings": schedule_warnings,
    }


def _repair_affected_days(
    translation: Dict[str, Any],
    current_plan: Dict[str, Any],
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    day_start_time: str = "09:00",
    travel_time_provider: Optional[TravelTimeProvider] = None,
    meal_windows: Sequence[MealWindow] = DEFAULT_MEAL_WINDOWS,
    restaurants=None,
    fine_tune_max_pool: Optional[int] = 10,
) -> Dict[str, Any]:
    """在现有计划上做最小增量修复（Step 2）。

    ``translation`` 是 Step 1 ``_translate_events`` 的结果；``current_plan`` 是
    正在执行的计划（``plan_multi_day`` / ``plan_one_day`` 输出）。未受影响的天
    原样保留；受影响天应用事件后重新排程，溢出则 fit / refill / fine-tune。
    单天修复失败返回 ``feasible=False`` + ``fallback_needed=True``。
    """
    requirement = translation["requirement"]
    # 统一使用「应用过事件翻译」的候选池：queue 时长增量 / weather 降权已作用到
    # 池中景点副本上，refill / fine-tune 补入的景点不会把受影响景点按原时长
    # 悄悄加回来（否则可选景点的排队增量会被 fit 移除后又被原样补回）。
    pool = _apply_translation_to_pool(translation)
    deltas = translation["spot_duration_deltas"]
    removed_keys = {_spot_key(spot) for spot in translation["removed_spots"]}

    raw_days = current_plan.get("days")
    if not raw_days:
        # 单日计划（plan_one_day 输出没有 days 数组）包装成统一结构。
        raw_days = [dict(current_plan, day=1)]
    if not raw_days:
        return {**current_plan, "replanned": False, "notes": translation["notes"]}

    content = requirement["content"]
    destination = content["destination"]
    daily_limit = int(content["constraints"]["daily_travel_time"])
    include_meal_time = _include_meal_time_in_daily_limit(requirement)
    day_start_minutes = _parse_time(day_start_time)
    budget_limit, visitor_number = _budget_context(requirement)

    # 交通矩阵覆盖：候选池 + 当前计划所有景点。
    pool_keys = {_spot_key(spot) for group in pool for spot in group}
    plan_keys = {
        node.get("details", {}).get("spot_id")
        for day in raw_days
        for node in _day_spot_nodes(day)
    }
    provider = travel_time_provider or JsonTravelTimeProvider(destination, graph_dir)
    matrix = provider.get_matrix(pool_keys | plan_keys)

    pool_by_key = _pool_by_key(pool)

    # 定位受影响天：queue 增量 / closed 移除 的景点所在天。
    affected_keys = set(deltas) | removed_keys
    affected_indexes = {
        index
        for index, day in enumerate(raw_days)
        if {
            node.get("details", {}).get("spot_id")
            for node in _day_spot_nodes(day)
        }
        & affected_keys
    }

    day_spots = [
        _restore_day_spots(day, pool_by_key, removed_keys) for day in raw_days
    ]

    notes = list(translation["notes"])

    def day_must_keys_of(index: int) -> Set[str]:
        return _day_must_keys(raw_days[index])

    # 第一遍：逐个修复受影响天（含事件感知错峰，见 _repair_day）。
    # day_results：index -> (ordered, elapsed, refilled, removed_for_time)
    # must_overflow：index -> 该天必须搬走的 must 景点 key 集合（单天放不下）
    day_results: Dict[int, Tuple[List[Spot], int, List[Spot], List[Spot]]] = {}
    final_must_keys: Dict[int, Set[str]] = {
        index: day_must_keys_of(index) for index in range(len(raw_days))
    }
    must_overflow: Dict[int, Set[str]] = {}
    for index in sorted(affected_indexes):
        repaired = _repair_day(
            day_spots[index],
            final_must_keys[index],
            _optional_pool_for_day(pool, index, day_spots),
            daily_limit,
            None,  # 预算为全程约束，天内不单独扣减（与 plan_multi_day 一致）
            visitor_number,
            matrix,
            day_start_minutes,
            meal_windows,
            include_meal_time,
            restaurants,
            fine_tune_max_pool,
            deltas,
        )
        if repaired is None:
            must_overflow[index] = set(final_must_keys[index])
        else:
            day_results[index] = repaired

    # 第二遍：跨天搬移（能力 2，8.20 新增）——单天修复删了景点 / 必去放不下时，
    # 优先把景点搬到其它有空档的天（接收天也不产生删减），全部景点保留优先于删减。
    # 修复优先级：错峰重排（能力 1）→ 跨天搬移（能力 2）→ 删兜底（fit）。
    if len(raw_days) > 1 and (
        must_overflow or any(result[3] for result in day_results.values())
    ):
        def trial_repair(
            ordered_spots: List[Spot], must_keys: Set[str], index: int
        ) -> Optional[Tuple[List[Spot], int, List[Spot], List[Spot]]]:
            """试排某天：可行且不删景点则返回完整修复结果，否则 None。"""
            result = _repair_day(
                ordered_spots,
                must_keys,
                _optional_pool_for_day(pool, index, day_spots),
                daily_limit,
                None,
                visitor_number,
                matrix,
                day_start_minutes,
                meal_windows,
                include_meal_time,
                restaurants,
                fine_tune_max_pool,
                deltas,
            )
            if result is None or result[3] or result[1] > daily_limit:
                return None
            return result

        # 目标：删减清零 / 必去搬出。每轮至少落一次，循环到无可搬为止。
        for _ in range(len(raw_days) * 12):
            moved_any = False

            # Type R：把被 fit 删掉的景点恢复到另一天（减少删减）。
            for index, (ordered, _elapsed, _refilled, removed_for_time) in list(
                day_results.items()
            ):
                for spot in list(removed_for_time):
                    for other in range(len(raw_days)):
                        if other == index or other in must_overflow:
                            continue
                        trial = trial_repair(
                            [*day_spots[other], spot],
                            final_must_keys[other],
                            other,
                        )
                        if trial is None:
                            continue
                        day_spots[other] = trial[0]
                        day_results[other] = trial
                        removed_for_time.remove(spot)
                        notes.append(
                            f"{spot.get('name', spot)} 移至第{other + 1}天，避免删减"
                        )
                        moved_any = True
                        break
                    if moved_any:
                        break

            # Type M：must 本身放不下的天，把 must 搬去另一天（当天腾空后再修复）。
            for index, must_keys in list(must_overflow.items()):
                must_spots = [
                    spot
                    for spot in day_spots[index]
                    if _spot_key(spot) in must_keys
                ]
                for spot in must_spots:
                    key = _spot_key(spot)
                    for other in range(len(raw_days)):
                        if other == index or other in must_overflow:
                            continue
                        trial = trial_repair(
                            [*day_spots[other], spot],
                            final_must_keys[other] | {key},
                            other,
                        )
                        if trial is None:
                            continue
                        sender = trial_repair(
                            [s for s in day_spots[index] if _spot_key(s) != key],
                            final_must_keys[index] - {key},
                            index,
                        )
                        if sender is None:
                            continue
                        day_spots[other] = trial[0]
                        day_results[other] = trial
                        final_must_keys[other] = final_must_keys[other] | {key}
                        day_spots[index] = sender[0]
                        day_results[index] = sender
                        final_must_keys[index] = final_must_keys[index] - {key}
                        must_keys.discard(key)
                        notes.append(
                            f"{spot.get('name', spot)} 移至第{other + 1}天，避免删减"
                        )
                        moved_any = True
                        break
                    if moved_any:
                        break
                if not must_keys:
                    del must_overflow[index]

            if not moved_any:
                break

    # 全部景点保留时给出可解释说明（错峰生效时 removed 为空且有排队增量）。
    for index in sorted(affected_indexes):
        result = day_results.get(index)
        if result is None or result[3]:
            continue
        day_delta_keys = {
            key for key in deltas if key in {_spot_key(s) for s in day_spots[index]}
        }
        if day_delta_keys:
            notes.append(f"第{index + 1}天排队增量已处理，全部景点保留")

    new_days: List[Dict[str, Any]] = []
    for index, day in enumerate(raw_days):
        if index in must_overflow:
            return {
                "feasible": False,
                "reason": (
                    f"第{index + 1}天增量修复失败：必去景点总时长超过每日出游时长"
                    "（跨天搬移无可用接收天）"
                ),
                "days": new_days,
                "failed_day": index + 1,
                "fallback_needed": True,
                "notes": notes,
            }
        if index not in day_results:
            new_days.append(dict(day))
            continue
        ordered, elapsed, refilled, removed_for_time = day_results[index]
        new_days.append(
            _build_day_output(
                index + 1,
                ordered,
                final_must_keys[index],
                matrix,
                day_start_minutes,
                meal_windows,
                include_meal_time,
                restaurants,
                provider,
                daily_limit,
                refilled,
                removed_for_time,
                visitor_number,
            )
        )

    if not affected_indexes:
        notes = [*notes, "受影响景点不在当前计划中，计划无需修改"]

    estimated_ticket_cost = sum(
        day.get("estimated_ticket_cost", 0) for day in new_days
    )
    budget_remaining = (
        budget_limit - estimated_ticket_cost if budget_limit is not None else None
    )
    return {
        "feasible": all(not day.get("is_overtime", False) for day in new_days),
        "days_requested": len(new_days),
        "daily_travel_time": daily_limit,
        "include_meal_time_in_daily_limit": include_meal_time,
        "days": new_days,
        "visitor_number": visitor_number,
        "budget": budget_limit,
        "estimated_ticket_cost": estimated_ticket_cost,
        "budget_remaining": budget_remaining,
        "budget_exceeded": budget_remaining is not None and budget_remaining < 0,
        "replanned": bool(affected_indexes),
        "affected_days": sorted(index + 1 for index in affected_indexes),
        "fallback_needed": False,
        "notes": notes,
        "warnings": translation["notes"],
    }


# ---------------------------------------------------------------------------
# Step 3：diff + 原因（_build_changes）与 replan() 主入口
# ---------------------------------------------------------------------------


def _extract_plan_spots(plan: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """把计划提取成 {spot_id: {day, name, start_minutes, end_minutes}}。

    兼容 ``plan_multi_day``（days 数组）与 ``plan_one_day``（无 days）两种输出。
    """
    result: Dict[str, Dict[str, Any]] = {}
    days = plan.get("days")
    if days:
        for day in days:
            day_number = day.get("day", 1)
            for node in day.get("route_details", []):
                if node.get("type") == "spot":
                    result[node["details"]["spot_id"]] = {
                        "day": day_number,
                        "name": node.get("name"),
                        "start_minutes": node.get("start_minutes"),
                        "end_minutes": node.get("end_minutes"),
                    }
    else:
        for node in plan.get("route_details", []):
            if node.get("type") == "spot":
                result[node["details"]["spot_id"]] = {
                    "day": 1,
                    "name": node.get("name"),
                    "start_minutes": node.get("start_minutes"),
                    "end_minutes": node.get("end_minutes"),
                }
    return result


def _format_slot(day_number: int, start_minutes: int, end_minutes: int) -> str:
    from data_transmission.itinerary import format_minutes

    return f"第{day_number}天 {format_minutes(start_minutes)}-{format_minutes(end_minutes)}"


def _removal_reason(name: str, spot_key: str, removed_keys: Set[str], deltas: Dict[str, int]) -> str:
    if spot_key in removed_keys:
        return f"{name} 关闭，无法继续游览，已从行程中移除"
    if spot_key in deltas:
        return f"{name} 排队增加 {deltas[spot_key]} 分钟，超出每日出游时长，已移除"
    return f"为满足每日出游时长约束，已移除{name}"


def _event_reason(
    name: str,
    spot_key: str,
    deltas: Dict[str, int],
    penalties: Dict[str, int],
    removed_keys: Set[str],
) -> Optional[str]:
    """与事件直接相关的景点给出事件原因；否则 None（用通用模板）。"""
    if spot_key in removed_keys:
        return f"{name} 关闭，无法继续游览"
    if spot_key in deltas:
        return f"{name} 排队增加 {deltas[spot_key]} 分钟，行程需调整"
    if spot_key in penalties:
        return f"{name} 受天气影响（match_score 扣减 {penalties[spot_key]} 分），行程需调整"
    return None


def _build_changes(
    old_plan: Dict[str, Any],
    new_plan: Dict[str, Any],
    translation: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """对比新旧计划，生成可解释的 changes（removed / added / move / rescheduled）。

    对齐 `plan/8.17-8.23.md` 的 RePlanner 输出契约（type / spot / from / to / reason）。
    同天时间变化小于 10 分钟视为微调噪音，不报告。
    """
    import re

    old_spots = _extract_plan_spots(old_plan)
    new_spots = _extract_plan_spots(new_plan)
    deltas = translation["spot_duration_deltas"]
    penalties = translation["spot_score_penalties"]
    removed_keys = {_spot_key(spot) for spot in translation["removed_spots"]}

    changes: List[Dict[str, Any]] = []
    for spot_key in sorted(set(old_spots) | set(new_spots)):
        old = old_spots.get(spot_key)
        new = new_spots.get(spot_key)
        name = (new or old)["name"]

        if old and not new:
            changes.append(
                {
                    "type": "removed",
                    "spot": name,
                    "from": _format_slot(
                        old["day"], old["start_minutes"], old["end_minutes"]
                    ),
                    "to": None,
                    "reason": _removal_reason(name, spot_key, removed_keys, deltas),
                }
            )
        elif new and not old:
            changes.append(
                {
                    "type": "added",
                    "spot": name,
                    "from": None,
                    "to": _format_slot(
                        new["day"], new["start_minutes"], new["end_minutes"]
                    ),
                    "reason": f"为填补调整后的行程空隙加入{name}",
                }
            )
        elif old["day"] != new["day"]:
            changes.append(
                {
                    "type": "move",
                    "spot": name,
                    "from": f"第{old['day']}天",
                    "to": f"第{new['day']}天",
                    "reason": _event_reason(
                        name, spot_key, deltas, penalties, removed_keys
                    )
                    or f"为满足每日出游时长约束，{name}调整至第{new['day']}天",
                }
            )
        else:
            time_changed = (old["start_minutes"], old["end_minutes"]) != (
                new["start_minutes"],
                new["end_minutes"],
            )
            if time_changed and (
                abs(new["start_minutes"] - old["start_minutes"])
                + abs(new["end_minutes"] - old["end_minutes"])
                >= 10
            ):
                changes.append(
                    {
                        "type": "rescheduled",
                        "spot": name,
                        "from": _format_slot(
                            old["day"], old["start_minutes"], old["end_minutes"]
                        ),
                        "to": _format_slot(
                            new["day"], new["start_minutes"], new["end_minutes"]
                        ),
                        "reason": _event_reason(
                            name, spot_key, deltas, penalties, removed_keys
                        )
                        or "为满足每日出游时长约束，调整游览时间",
                    }
                )

    # 按 (天, 开始时间) 排序，removed/added 取有值的一侧。
    def sort_key(change: Dict[str, Any]) -> tuple:
        slot = change.get("from") or change.get("to") or ""
        match = re.match(r"第(\d+)天 (\d{2}:\d{2})-", slot)
        if match:
            return (int(match.group(1)), match.group(2))
        match = re.match(r"第(\d+)天$", slot)
        if match:
            return (int(match.group(1)), "")
        return (0, "")

    changes.sort(key=sort_key)
    return changes


def _apply_translation_to_pool(translation: Dict[str, Any]) -> List[List[Spot]]:
    """把 queue 时长增量 / weather 降权应用到候选池副本（fallback 全量重跑用）。"""
    deltas = translation["spot_duration_deltas"]
    penalties = translation["spot_score_penalties"]
    adjusted: List[List[Spot]] = []
    for group in translation["candidate_spots"]:
        new_group: List[Spot] = []
        for spot in group:
            key = _spot_key(spot)
            adjusted_spot = spot
            if key in deltas:
                adjusted_spot = _spot_with_duration_delta(adjusted_spot, deltas[key])
            if key in penalties:
                adjusted_spot = dict(adjusted_spot)
                adjusted_spot["match_score"] = (
                    float(adjusted_spot.get("match_score", 0)) - penalties[key]
                )
            new_group.append(adjusted_spot)
        adjusted.append(new_group)
    return adjusted


def _replan_hotels(
    translation: Dict[str, Any],
    current_plan: Dict[str, Any],
    repaired: Dict[str, Any],
    graph_dir: Path = DEFAULT_GRAPH_DIR,
) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """酒店事件 → 对（新）计划重选住宿并生成 hotel_changed。

    返回 (补充的 changes, 新 accommodation 或 None)。无酒店事件时不重选；
    酒店没变但价格变了（hotel.price_change）也按 hotel_changed 报告。
    """
    from transport.hotels import select_hotels_for_plan

    exclude_ids = translation["hotel_exclude_ids"]
    price_deltas = translation["hotel_price_deltas"]
    if not exclude_ids and not price_deltas:
        return [], current_plan.get("accommodation")

    current_acc = current_plan.get("accommodation")
    new_acc = select_hotels_for_plan(
        translation["requirement"],
        repaired,
        graph_dir=graph_dir,
        exclude_ids=exclude_ids,
        price_deltas=price_deltas,
    )
    notes = translation["notes"] or ["住宿安排调整"]
    reason = "；".join(notes)

    if new_acc is None:
        if current_acc is None:
            return [], None
        old_name = current_acc.get("constant_hotel", {}).get("hotel_name") or "原酒店"
        return [
            {
                "type": "hotel_changed",
                "spot": old_name,
                "from": old_name,
                "to": None,
                "reason": f"{reason}；无可用替代酒店，住宿待人工处理",
            }
        ], None

    current_ids = [b["hotel_id"] for b in current_acc.get("bookings", [])] if current_acc else []
    new_ids = [b["hotel_id"] for b in new_acc["bookings"]]
    if current_ids != new_ids:
        old_name = (
            current_acc.get("constant_hotel", {}).get("hotel_name") if current_acc else None
        )
        new_name = new_acc["constant_hotel"]["hotel_name"]
        return [
            {
                "type": "hotel_changed",
                "spot": new_name,
                "from": old_name,
                "to": new_name,
                "reason": reason,
            }
        ], new_acc

    # 酒店不变但总价变化（价格事件只影响费用口径）
    old_cost = current_acc.get("hotel_cost", 0.0) if current_acc else None
    new_cost = new_acc["hotel_cost"]
    if old_cost is not None and old_cost != new_cost:
        return [
            {
                "type": "hotel_changed",
                "spot": new_acc["constant_hotel"]["hotel_name"],
                "from": f"¥{old_cost:.0f}",
                "to": f"¥{new_cost:.0f}",
                "reason": reason,
            }
        ], new_acc
    return [], current_acc


def replan(
    requirement: Dict[str, Any],
    current_plan: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Spot]],
    events: Sequence[Dict[str, Any]],
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    day_start_time: str = "09:00",
    travel_time_provider: Optional[TravelTimeProvider] = None,
    meal_windows: Sequence[MealWindow] = DEFAULT_MEAL_WINDOWS,
    restaurants=None,
    beam_width: int = 200,
    fine_tune_max_pool: Optional[int] = 10,
) -> Dict[str, Any]:
    """RePlanner 主入口：翻译事件 → 增量修复 → 失败兜底全量重跑 → 生成 diff。

    返回：
        triggered_by   触发重规划的事件类型列表
        changes        可解释变化（removed / added / move / rescheduled + reason）
        new_plan       修复后的计划（增量修复或 fallback 全量重跑的结果）
        feasible       新计划是否可行
        fallback_used  是否走了全量重跑兜底
        notes          翻译与修复说明
    """
    from algorithoms.planner import plan_multi_day

    translation = _translate_events(requirement, candidate_spots, events)
    repaired = _repair_affected_days(
        translation,
        current_plan,
        graph_dir=graph_dir,
        day_start_time=day_start_time,
        travel_time_provider=travel_time_provider,
        meal_windows=meal_windows,
        restaurants=restaurants,
        fine_tune_max_pool=fine_tune_max_pool,
    )

    fallback_attempted = False
    fallback_used = False
    if repaired.get("fallback_needed"):
        # 兜底：把事件翻译（queue 时长增量 / weather 降权）应用到候选池后全量重跑。
        fallback_attempted = True
        adjusted_pool = _apply_translation_to_pool(translation)
        fallback_plan = plan_multi_day(
            translation["requirement"],
            adjusted_pool,
            graph_dir=graph_dir,
            day_start_time=day_start_time,
            travel_time_provider=travel_time_provider,
            meal_windows=meal_windows,
            restaurants=restaurants,
            beam_width=beam_width,
        )
        if fallback_plan["feasible"]:
            repaired = fallback_plan
            fallback_used = True
        else:
            repaired = {**repaired, "fallback_attempted": True}

    changes = _build_changes(current_plan, repaired, translation)
    hotel_changes, new_accommodation = _replan_hotels(
        translation, current_plan, repaired, graph_dir
    )
    changes.extend(hotel_changes)
    if new_accommodation is not None:
        repaired["accommodation"] = new_accommodation
    elif hotel_changes:
        repaired.pop("accommodation", None)
    return {
        "triggered_by": [event.get("event_type") for event in events],
        "changes": changes,
        "new_plan": repaired,
        "feasible": bool(repaired.get("feasible")),
        "fallback_attempted": fallback_attempted,
        "fallback_used": fallback_used,
        "notes": translation["notes"],
    }
