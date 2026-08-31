"""对话改时间轴的深度可行性校验（chat v2.2，2026-09-01）。

L2 三项（B 侧可自算、可解释，不引入交通矩阵）：
  1. 闭馆：scenic 的到达 / 预计结束时间必须在 ``open_time`` 内；
  2. 每日时长：游览 + 段间交通 + 餐饮 ≤ ``constraints.daily_travel_time``；
  3. 预算：景点门票 + 酒店等价格求和 ≤ ``constraints.budget``。

估算口径（与 A 侧 replanner 对齐，避免误判）：
  - 景点时长：优先 ``end_time - arrival``，缺失用候选池 ``Spot.duration``
    （select_spots 按名称/别名匹配），再缺失用默认 90 分钟；
  - 段间交通：优先 ``end_time - arrival``，缺失默认 30 分钟/段；
  - 餐饮：优先 ``end_time - arrival``，缺失默认 60 分钟/段。

校验失败信息精确到「第X天 景点名 原因」，回填 LLM 后由模型调整重试。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

DEFAULT_SPOT_MIN = 90
DEFAULT_TRANSPORT_MIN = 30
DEFAULT_MEAL_MIN = 60


def _parse_hhmm(value: Any) -> Optional[int]:
    """HH:MM → 当天分钟数；解析失败返回 None。"""
    try:
        hh, mm = (str(value).split(":") + ["00"])[:2]
        return int(hh) * 60 + int(mm)
    except (ValueError, AttributeError):
        return None


def _open_window(open_time: Any) -> Optional[tuple]:
    """'09:00-17:00' → (open_min, close_min)；非法返回 None。"""
    parts = str(open_time or "").split("-")
    if len(parts) != 2:
        return None
    op, cl = _parse_hhmm(parts[0]), _parse_hhmm(parts[1])
    if op is None or cl is None:
        return None
    return op, cl


def _segment_minutes(item: Any) -> Optional[int]:
    """``end_time - arrival`` 的分钟数；缺失/非法返回 None。"""
    arrival = _parse_hhmm(item.arrival)
    end = _parse_hhmm(item.end_time)
    if arrival is None or end is None or end <= arrival:
        return None
    return end - arrival


def _spot_duration_minutes(name: str, candidate_pool: Sequence[Sequence[dict]]) -> int:
    """候选池按名称/别名匹配 duration；失败用默认 90 分钟。"""
    for group in candidate_pool:
        for spot in group:
            if spot.get("name") == name or name in (spot.get("alias") or []):
                return int(spot.get("duration") or DEFAULT_SPOT_MIN)
    return DEFAULT_SPOT_MIN


def _budget_of(requirement: Dict[str, Any]) -> Optional[float]:
    content = (requirement or {}).get("content") or {}
    cons = content.get("constraints") or {}
    try:
        return float(cons.get("budget"))
    except (TypeError, ValueError):
        return None


def _daily_travel_time_of(requirement: Dict[str, Any]) -> Optional[int]:
    content = (requirement or {}).get("content") or {}
    cons = content.get("constraints") or {}
    try:
        value = int(cons.get("daily_travel_time") or 0)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _estimate_total_cost(timeline: Any) -> float:
    total = 0.0
    for day in timeline.days:
        for item in day.items:
            total += float(item.price or 0.0)
    return total


def _estimate_day_minutes(day: Any, candidate_pool: Sequence[Sequence[dict]]) -> int:
    total = 0
    for item in day.items:
        category = getattr(item, "category", "scenic")
        seg = _segment_minutes(item)
        if category == "scenic":
            total += seg if seg is not None else _spot_duration_minutes(
                str(item.name or ""), candidate_pool
            )
        elif category == "food":
            total += seg if seg is not None else DEFAULT_MEAL_MIN
        elif category == "transport":
            total += seg if seg is not None else DEFAULT_TRANSPORT_MIN
    return total


def _check_closing(item: Any, candidate_pool: Sequence[Sequence[dict]]) -> Optional[str]:
    """闭馆校验：到达/预计结束必须在 open_time 内。"""
    window = _open_window(item.open_time)
    if window is None:
        return None  # 无营业时间信息不校验
    open_min, close_min = window
    arrival = _parse_hhmm(item.arrival)
    if arrival is None:
        return None
    end = _segment_minutes(item)
    if end is None:
        end = arrival + _spot_duration_minutes(str(item.name or ""), candidate_pool)
    if arrival < open_min:
        return f"到达时间 {item.arrival} 早于开门时间 {item.open_time}"
    if end > close_min:
        return f"预计结束时间超过闭馆时间（{item.open_time}）"
    return None


def _load_candidate_pool(requirement: Dict[str, Any]) -> List[List[dict]]:
    """A 侧候选池（北京/上海假图有 duration）；加载失败返回空（用默认估算）。"""
    try:
        from algorithoms.select_spots import select_spots

        pool = select_spots(requirement, ask_user_on_conflict=False)
        return [[dict(s) for s in group] for group in pool] if pool else []
    except Exception:  # noqa: BLE001  校验是增强，加载失败降级默认估算
        return []


def validate_timeline(
    timeline: Any,
    requirement: Optional[Dict[str, Any]] = None,
    candidate_pool: Optional[Sequence[Sequence[dict]]] = None,
) -> List[str]:
    """返回校验错误列表（空 = 通过）。

    ``candidate_pool`` 缺省按 ``requirement`` 加载（A 侧 select_spots）。
    """
    requirement = requirement or {}
    errors: List[str] = []
    if candidate_pool is None:
        candidate_pool = _load_candidate_pool(requirement)

    budget = _budget_of(requirement)
    total = _estimate_total_cost(timeline)
    if budget is not None and total > budget:
        errors.append(
            f"预算超支：估算总花费 ¥{total:.0f} 超过预算 ¥{budget:.0f}"
        )

    daily_limit = _daily_travel_time_of(requirement)
    for day in timeline.days:
        day_total = _estimate_day_minutes(day, candidate_pool)
        if daily_limit is not None and day_total > daily_limit:
            errors.append(
                f"第{day.day}天超时：估算 {day_total} 分钟超过"
                f"每日上限 {daily_limit} 分钟（景点时长取候选池、"
                f"交通 30 分钟/段、餐饮 60 分钟）"
            )
        for item in day.items:
            if getattr(item, "category", "") != "scenic":
                continue
            closing_err = _check_closing(item, candidate_pool)
            if closing_err:
                errors.append(f"第{day.day}天 {item.name}：{closing_err}")
    return errors
