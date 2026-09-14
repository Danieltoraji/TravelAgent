"""schedule_plan：排程器 tool 化（智能体编排方案阶段 a 的枢纽，2026-09-14）。

把 ``algorithoms/planner.plan_multi_day`` 包装为**本地工具执行器**，供
``call_llm/orchestrator.py`` 的 LLM 编排循环调用——LLM 只改参数（min_spots /
allocator / 中心计划），时刻表/金额/可行性永远由排程器落锤（方案红线 1：
确定性计算不交 LLM）。

**落位与边界（口径拍板，2026-09-14）**：本工具是 A 侧纯本地函数，**不进
``data_transmission.tool_specs.TOOL_SPECS``**——该注册表的不变量是「工具名 =
B 侧 ``tool_provider.call`` 字符串」，且 ``budget_default``/``cached``/``mode``
三个字段对排程器全部无意义（不耗真源额度；``(name,o,d,date)`` 缓存键是城际
城市对形状；mode 只有 train/air）。排程器的「限额」是编排轮数预算（
``ctx.max_calls``，超限返回结构化 error，方案红线 4），不是 QuotaManager
per-mode 预算。它有独立的 OpenAI 工具面（``SCHEDULE_TOOL_SCHEMA``），与 B
工具面在编排器 ``tool_executor`` 门面统一分派给 LLM。

executor 契约（与 ``planner_agent.intercity_verify_executor`` 同款鸭子 ctx）：
ctx 需含 ``requirement`` / ``spots``（[must, conflict, scored] 三组池）/
``planner_fn``（None = 缺省 ``plan_multi_day``，测试可注入）/
``travel_time_provider`` / ``restaurants`` / ``first_day_start_time`` /
``last_day_end_minutes``（时刻窗由城际骨架落锤，**不对 LLM 开放**）/
``center_schedule``（已规则定界的按日中心，None = 单中心路径）/
``max_calls``（调用上限；``calls`` 可变计数器 dict）。

``params``（LLM 可改参数，入参 schema 见 ``SCHEDULE_TOOL_SCHEMA``）：
- ``min_spots``：完整一天至少排多少个景点（0-10，缺省 0 = 不设硬规则）；
- ``allocator``：可选景点跨天分配策略（balanced / greedy，缺省 balanced）；
- ``center_plan``：按日中心计划（``[{day_from, day_to, center: {type:
  poi|city_center, poi?, reason?}}]``）——**规则定界在编排器**（复用
  ``scenic_search_planner._validate_center_schedule``），传入本 executor 时
  已是合法 ``center_schedule``，此处直接构造 ``affinity_fn``/``day_anchors``
  （与 ``BPlannerHook._planner`` P5.7 同口径）。

返回 ``{"status": "ok", "summary": {days/费用/可行性摘要}, "plan": 完整计划
dict}``——summary 回填 LLM（token 可控），完整 plan 由编排器持有进工作台；
非法入参/超限 → ``{"status": "error", "error": 机器可读名, "detail": 一句话}``
（错误被工具回路消费，不抛异常不炸链路，与 P5.6-S2 结构化错误同哲学）。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Dict, Optional

SCHEDULE_TOOL_NAME = "schedule_plan"

# LLM 工具面：只暴露「可改参数」，时刻窗/池子/时刻表一律不经 LLM 之手。
SCHEDULE_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": SCHEDULE_TOOL_NAME,
        "description": (
            "确定性排程器（全流程唯一落锤时刻表的地方）。传入可调参数，"
            "返回整案草案（逐日景点/费用/可行性）+ 确定性质量信号。"
            "规则：改参数 → 调本工具 → 读质量信号 → 决定下一轮改什么；"
            "你绝不自己生成任何时刻或金额。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "min_spots": {
                    "type": "integer",
                    "description": "完整的一天至少排多少个景点（0-10，0=不设硬规则）",
                },
                "allocator": {
                    "type": "string",
                    "enum": ["balanced", "greedy"],
                    "description": "可选景点跨天分配策略：balanced 均匀 / greedy 逐日优先",
                },
                "center_plan": {
                    "type": "array",
                    "description": (
                        "按日空间中心计划（多日游，几个片区各玩几天）："
                        "[{day_from, day_to, center: {type: \"poi\"|\"city_center\", "
                        "poi: 景点名(type=poi 时必填), reason: 一句理由}}]，"
                        "区间覆盖 1..天数；不传 = 沿用当前中心计划"
                    ),
                    "items": {"type": "object"},
                },
            },
            "additionalProperties": False,
        },
    },
}

_ERROR_INVALID_PARAMS = "invalid_params"
_ERROR_BUDGET = "schedule_budget_exceeded"


def _error(error: str, detail: str) -> Dict[str, Any]:
    """结构化错误（错误被工具回路消费：模型读后换参数/如实说明，不炸链路）。"""
    return {"status": "error", "error": error, "detail": detail}


def _validate_params(params: Dict[str, Any]) -> tuple:
    """校验 LLM 可改参数 → (min_spots, allocator) 或抛 ValueError（转结构化）。"""
    min_spots = params.get("min_spots") or 0
    try:
        min_spots = int(min_spots)
    except (TypeError, ValueError):
        raise ValueError(f"min_spots 必须是整数，收到 {min_spots!r}")
    if not 0 <= min_spots <= 10:
        raise ValueError(f"min_spots 超出 0-10：{min_spots}")

    allocator = str(params.get("allocator") or "balanced")
    if allocator not in {"balanced", "greedy"}:
        raise ValueError(f"未知 allocator：{allocator!r}（可选 balanced / greedy）")
    return min_spots, allocator


def _build_affinity(center_schedule: Optional[list], spots: Any, day_count: int):
    """center_schedule → (affinity_fn, day_anchors)（与 BPlannerHook._planner 同口径）。

    schedule 空 / 池空 / 解析失败 → (None, None)（原单中心路径，零回归）。
    """
    if not center_schedule:
        return None, None
    try:
        from algorithoms.select_spots import (
            build_center_affinity_fn,
            resolve_day_anchors,
        )

        day_anchors = resolve_day_anchors(center_schedule, spots, day_count)
        if day_anchors is None:
            return None, None
        affinity_fn = build_center_affinity_fn(center_schedule, spots, day_count)
        return affinity_fn, day_anchors
    except Exception as exc:  # noqa: BLE001  亲和构造失败不拖垮排程（回退单中心）
        import logging

        logging.getLogger("call_llm.schedule_tool").warning(
            "center_schedule 亲和构造失败，回退单中心路径：%s: %s",
            type(exc).__name__, exc,
        )
        return None, None


def _plan_summary(plan: Dict[str, Any]) -> Dict[str, Any]:
    """完整 plan → 给 LLM 的结构化摘要（token 可控；完整 plan 留在工作台）。"""
    from algorithoms._common import _plan_cost_summary

    days_out = []
    for day in plan.get("days") or []:
        spots = [
            node.get("name") or ""
            for node in (day.get("route_details") or [])
            if node.get("type") == "spot"
        ]
        days_out.append({
            "day": day.get("day"),
            "spots": spots,
            "spot_count": len(spots),
            "utilization_rate": day.get("utilization_rate"),
        })
    return {
        "feasible": bool(plan.get("feasible")),
        "days": days_out,
        "cost": _plan_cost_summary(plan),
        "total_match_score": plan.get("total_match_score"),
        "warnings": list(plan.get("warnings") or []),
    }


def run_schedule_plan(ctx: Any, params: Dict[str, Any]) -> Dict[str, Any]:
    """排程器本地工具执行器（编排器 ``_tool_executor`` 的 ``schedule_plan`` 分派目标）。

    返回 ``{"status": "ok", "summary": ..., "plan": ...}`` 或结构化 error
    （见模块 docstring）。``planner_fn`` 缺省 ``plan_multi_day``：签名
    ``(requirement, spots, **kwargs)``，测试可注入假排程器。
    """
    params = params if isinstance(params, dict) else {}
    try:
        min_spots, allocator = _validate_params(params)
    except ValueError as exc:
        return _error(_ERROR_INVALID_PARAMS, str(exc))

    # 调用预算在参数校验后计数：只对「真实起排程器」的调用计费（非法入参
    # 没跑排程器、无成本，不占预算）；排程器内部异常同样计费（失败也计费，
    # 与 QuotaManager 语义一致——防失败重试打空预算窗口）。
    calls = ctx.calls if hasattr(ctx, "calls") else {}
    calls[SCHEDULE_TOOL_NAME] = calls.get(SCHEDULE_TOOL_NAME, 0) + 1
    max_calls = int(getattr(ctx, "max_calls", 0) or 0)
    if max_calls and calls[SCHEDULE_TOOL_NAME] > max_calls:
        return _error(
            _ERROR_BUDGET,
            f"schedule_plan 调用达上限 {max_calls}，请基于现有草案收尾",
        )

    requirement = ctx.requirement
    try:
        day_count = int((requirement.get("content") or {}).get("days") or 0)
    except (TypeError, ValueError):
        day_count = 0
    if day_count <= 0:
        return _error(_ERROR_INVALID_PARAMS, "requirement 缺少有效的 days")

    center_schedule = getattr(ctx, "center_schedule", None)
    affinity_fn, day_anchors = _build_affinity(center_schedule, ctx.spots, day_count)

    planner_fn = getattr(ctx, "planner_fn", None)
    if planner_fn is None:
        from algorithoms.planner import plan_multi_day as planner_fn

    try:
        plan = planner_fn(
            requirement,
            ctx.spots,
            travel_time_provider=getattr(ctx, "travel_time_provider", None),
            restaurants=getattr(ctx, "restaurants", None),
            first_day_start_time=getattr(ctx, "first_day_start_time", None),
            last_day_end_minutes=getattr(ctx, "last_day_end_minutes", None),
            allocator=allocator,
            min_spots=min_spots,
            affinity_fn=affinity_fn,
            day_anchors=day_anchors,
        )
    except Exception as exc:  # noqa: BLE001  排程器内部错误 → 结构化回填
        return _error("planner_error", f"排程器执行失败：{type(exc).__name__}: {exc}")

    if not isinstance(plan, dict) or not plan.get("days"):
        return _error("planner_error", "排程器未产出可用计划（days 为空）")
    return {"status": "ok", "summary": _plan_summary(plan), "plan": plan}


def schedule_tool_ctx(
    requirement: Dict[str, Any],
    spots: Any,
    *,
    planner_fn: Optional[Any] = None,
    travel_time_provider: Any = None,
    restaurants: Any = None,
    first_day_start_time: Optional[str] = None,
    last_day_end_minutes: Optional[int] = None,
    center_schedule: Optional[list] = None,
    max_calls: int = 4,
    calls: Optional[Dict[str, int]] = None,
) -> SimpleNamespace:
    """组装 ``run_schedule_plan`` 的 ctx（编排器/测试共用，字段见模块 docstring）。"""
    return SimpleNamespace(
        requirement=requirement,
        spots=spots,
        planner_fn=planner_fn,
        travel_time_provider=travel_time_provider,
        restaurants=restaurants,
        first_day_start_time=first_day_start_time,
        last_day_end_minutes=last_day_end_minutes,
        center_schedule=center_schedule,
        max_calls=max_calls,
        calls=calls if calls is not None else {},
    )
