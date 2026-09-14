"""确定性质量信号（智能体编排方案 §3：规则算信号，LLM 判权衡）。

编排器每次 ``schedule_plan`` 落锤后调用 ``compute_quality_signals``，把纯规则
算出的信号字典回填给模型——模型据此决定「接受 / 改参数重排」（混合模式与
``decision_engine`` 同哲学：规则把关可行性，LLM 负责权衡）。本模块**不调
LLM、不判达标**，只算数——判「好/坏」是模型的事。

信号口径（方案 §3 信号表的可计算子集，全部来自 plan dict / requirement，
不依赖真源）：
- 可行性：整案 feasible、计划天数 vs 需求天数、空景点天；
- 预算：五项费用（``algorithoms._common._plan_cost_summary`` 单一来源）vs
  requirement 预算、酒店/城际占比；
- 景点-行程：逐日景点数、单景点天、必去覆盖缺口（must_visit 名单 vs 计划
  内景点名）、平均利用率；
- 其它：排程 warnings 数。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _requirement_budget(requirement: Dict[str, Any]) -> Optional[float]:
    try:
        budget = (requirement.get("content") or {}).get("constraints", {}).get(
            "budget"
        )
    except AttributeError:
        return None
    if budget is None:
        return None
    try:
        return float(budget)
    except (TypeError, ValueError):
        return None


def _required_days(requirement: Dict[str, Any]) -> Optional[int]:
    try:
        days = int((requirement.get("content") or {}).get("days") or 0)
    except (TypeError, ValueError):
        return None
    return days or None


def _must_visit_names(requirement: Dict[str, Any]) -> List[str]:
    try:
        must = (
            (requirement.get("content") or {})
            .get("constraints", {})
            .get("must_visit")
        )
    except AttributeError:
        return []
    return [str(name) for name in (must or []) if str(name or "").strip()]


def compute_quality_signals(
    plan: Dict[str, Any], requirement: Dict[str, Any]
) -> Dict[str, Any]:
    """plan + requirement → 确定性信号字典（纯函数，不抛异常，坏输入给零值）。"""
    from algorithoms._common import _plan_cost_summary

    plan = plan if isinstance(plan, dict) else {}
    days = plan.get("days") or []

    day_reports: List[Dict[str, Any]] = []
    plan_spot_names: List[str] = []
    for day in days:
        if not isinstance(day, dict):
            continue
        names = [
            node.get("name") or ""
            for node in (day.get("route_details") or [])
            if isinstance(node, dict) and node.get("type") == "spot"
        ]
        plan_spot_names.extend(names)
        day_reports.append({
            "day": day.get("day"),
            "spot_count": len(names),
            "utilization_rate": day.get("utilization_rate"),
        })

    single_spot_days = [d["day"] for d in day_reports if d["spot_count"] == 1]
    empty_days = [d["day"] for d in day_reports if d["spot_count"] == 0]
    utilizations = [
        d["utilization_rate"]
        for d in day_reports
        if isinstance(d.get("utilization_rate"), (int, float))
    ]

    must_missing = [
        name for name in _must_visit_names(requirement)
        if not any(name in spot for spot in plan_spot_names)
    ]

    cost = _plan_cost_summary(plan)
    budget = _requirement_budget(requirement)
    total = float(cost.get("total") or 0.0)
    budget_signals: Dict[str, Any] = {
        "cost": cost,
        "budget": budget,
        "over_budget": bool(budget is not None and total > budget),
    }
    if total > 0:
        budget_signals["hotel_share"] = round(
            float(cost.get("hotel") or 0.0) / total, 3
        )
        budget_signals["transit_share"] = round(
            float(cost.get("transit") or 0.0) / total, 3
        )

    return {
        "feasible": bool(plan.get("feasible")),
        "days_planned": len(day_reports),
        "days_requested": _required_days(requirement),
        "empty_days": empty_days,
        "single_spot_days": single_spot_days,
        "avg_utilization": (
            round(sum(utilizations) / len(utilizations), 3) if utilizations else None
        ),
        "must_missing": must_missing,
        "budget": budget_signals,
        "warnings_count": len(plan.get("warnings") or []),
    }
