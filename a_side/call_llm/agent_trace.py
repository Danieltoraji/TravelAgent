"""agent_trace：LLM 编排过程的可解释轨迹（C 端可见的思考/调 tool 过程）。

需求（2026-09-15，用户拍板）：把大模型思考、调 tool 的过程向 C 端展示——
答辩卖点「LLM 指挥官 + 确定性工具执行部队」从口号变成可见的步骤流。

**给结构化摘要而非原始回声**：ToolResult 全文（景点池/票价矩阵）又大又噪，
C 端要的是「调了什么、看到什么关键事实、据此做了什么决定」。本模块把
``PlanOrchestrator.run()`` 的结果（``tool_trace``/``reviews``/
``quality_history``/最终收尾 JSON）整形为按序步骤列表：

```json
{"enabled": true, "rounds": 2, "steps": [
  {"seq": 1, "phase": "查真源", "tool": "train_trip",
   "args_digest": {...}, "result_digest": {...}, "ms": 1500},
  {"seq": 2, "phase": "审查", "verdict": "ok", "reason": "…"},
  {"seq": 3, "phase": "落锤", "tool": "schedule_plan",
   "args_digest": {...}, "signals_digest": {...}, "ms": 9000},
  {"seq": 4, "phase": "收尾", "accepted": true, "summary": "…"}]}
```

整形纪律：result_digest 按工具白名单取关键字段（其余截断）；步数上限
``MAX_STEPS``；纯函数不抛异常（坏输入给空轨迹）。展示侧（C 端）按 phase
图标渲染步骤流，此处不关心样式。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

MAX_STEPS = 50
_DIGEST_CAP = 200

_MODE_TEXT = {"train": "火车", "air": "航班", "driving": "自驾"}


def _cap(value: Any, cap: int = _DIGEST_CAP) -> Any:
    """字符串截断（非字符串原样返回；防极端结果刷爆轨迹体积）。"""
    if isinstance(value, str) and len(value) > cap:
        return value[:cap] + "…"
    return value


def digest_args(tool: str, args: Any) -> Dict[str, Any]:
    """工具入参摘要（入参本身就是 LLM 的决策，基本不裁剪；仅截断长串）。"""
    if not isinstance(args, dict):
        return {"raw": _cap(args)}
    out = {}
    for key, value in args.items():
        if isinstance(value, list) and len(value) > 3:
            out[key] = value[:3] + [f"…共{len(value)}项"]
        else:
            out[key] = _cap(value)
    return out


def digest_result(tool: str, result: Any) -> Dict[str, Any]:
    """工具返回摘要（按工具白名单取关键事实；错误如实透出）。"""
    if not isinstance(result, dict):
        return {"raw": _cap(result)}
    if result.get("status") == "error":
        return {
            "status": "error",
            "error": _cap(result.get("error"), 80),
            "detail": _cap(result.get("detail"), 160),
        }
    tool = str(tool or "")
    if tool == "schedule_plan":
        return _digest_schedule_result(result)
    if tool in ("train_trip", "train_ticket", "flight_search"):
        keys = ("status", "mode", "train_no", "flight_no", "departure",
                "arrival", "duration_minutes", "price", "count")
        return {k: result[k] for k in keys if k in result} or {"status": "ok"}
    if tool == "scenic":
        data = result.get("data") or result.get("spots") or []
        names = [str(x.get("name") or "") for x in data[:5] if isinstance(x, dict)]
        return {"count": len(data), "top": [n for n in names if n]}
    if tool == "hotel":
        data = result.get("data") or result.get("hotels") or []
        names = [str(x.get("name") or x.get("hotel_name") or "")
                 for x in data[:5] if isinstance(x, dict)]
        return {"count": len(data), "top": [n for n in names if n]}
    if tool == "food":
        data = result.get("data") or []
        return {"count": len(data) if isinstance(data, list) else 1}
    if tool == "map":
        return {"action": result.get("action"), "count": len(result.get("data") or [])}
    return {"keys": sorted(k for k in result.keys())[:8]}


def _digest_schedule_result(result: Dict[str, Any]) -> Dict[str, Any]:
    """schedule_plan 返回摘要：逐日景点名 + 五项费用 + 信号要点。"""
    summary = result.get("summary") or {}
    signals = result.get("quality_signals") or {}
    days_out = []
    for day in (summary.get("days") or [])[:10]:
        if isinstance(day, dict):
            days_out.append({
                "day": day.get("day"),
                "spots": (day.get("spots") or [])[:5],
                "spot_count": day.get("spot_count"),
            })
    budget = signals.get("budget") or {}
    out: Dict[str, Any] = {
        "feasible": summary.get("feasible"),
        "days": days_out,
        "total_cost": (budget.get("cost") or {}).get("total")
        if isinstance(budget.get("cost"), dict) else None,
        "over_budget": budget.get("over_budget"),
    }
    if budget.get("note"):
        out["budget_note"] = _cap(budget["note"], 120)
    for key in ("single_spot_days", "must_missing", "empty_days"):
        if signals.get(key):
            out[key] = signals[key]
    return out


def _step(seq: int, phase: str, **fields: Any) -> Dict[str, Any]:
    step = {"seq": seq, "phase": phase}
    step.update({k: v for k, v in fields.items() if v is not None})
    return step


def build_orchestrator_trace(orch_result: Any, *, tool_stats: Any = None) -> Dict[str, Any]:
    """编排器结果 → agent_trace dict（纯函数；坏输入给降级轨迹不抛异常）。

    ``orch_result``：``PlanOrchestrator.run()`` 的返回 dict（tool_trace /
    reviews / quality_history / accepted / summary / fallback_reason /
    preferred_stations / tools_enabled）。``tool_stats``：编排器按调用顺序
   记录的 ``[{"name", "ms", "ok"}]``（与 tool_trace 的调用顺序对齐）。
    """
    result = orch_result if isinstance(orch_result, dict) else {}
    enabled = bool(result.get("tools_enabled"))
    trace: Dict[str, Any] = {
        "enabled": enabled,
        "steps": [],
        "fallback_reason": result.get("fallback_reason"),
    }
    steps: List[Dict[str, Any]] = trace["steps"]
    if not enabled:
        # 门控关/未启用：只记回落原因（C 端不展示空流程）
        if result.get("fallback_reason"):
            steps.append(_step(1, "回落", reason=_cap(result["fallback_reason"], 200)))
        return trace

    tool_trace = result.get("tool_trace") or []
    reviews = result.get("reviews") or []
    stats = list(tool_stats or [])
    stats_index = 0
    seq = 0

    def _next_step(phase: str, **fields: Any) -> None:
        nonlocal seq
        if seq >= MAX_STEPS:
            return
        seq += 1
        steps.append(_step(seq, phase, **fields))

    for round_info in tool_trace:
        if not isinstance(round_info, dict):
            continue
        for call in round_info.get("calls") or []:
            if not isinstance(call, dict):
                continue
            name = str(call.get("name") or "")
            ms = None
            ok = None
            if stats_index < len(stats) and isinstance(stats[stats_index], dict):
                ms = stats[stats_index].get("ms")
                ok = stats[stats_index].get("ok")
            stats_index += 1
            _next_step(
                "落锤" if name == "schedule_plan" else "查真源",
                tool=name,
                args_digest=digest_args(name, call.get("arguments")),
                result_digest=digest_result(name, call.get("result")),
                ms=ms,
            )
        review = next(
            (r for r in reviews if isinstance(r, dict)
             and r.get("round") == round_info.get("round")),
            None,
        )
        if review is not None:
            _next_step(
                "审查",
                verdict=review.get("review"),
                reason=_cap(review.get("reason"), 160),
                tools=review.get("tools"),
            )

    preferred = result.get("preferred_stations") or {}
    for direction, station in preferred.items():
        _next_step("提名站对", direction=direction, station=station)

    content = result.get("summary")
    reasons = result.get("reasons") or []
    _next_step(
        "收尾",
        accepted=result.get("accepted"),
        summary=_cap(content, 400) if content else None,
        reasons=[_cap(r, 160) for r in reasons[:3]] or None,
    )
    trace["rounds"] = result.get("tool_rounds")
    trace["schedule_calls"] = result.get("schedule_calls")
    trace["accepted"] = result.get("accepted")
    return trace


def build_minimal_trace(
    *,
    decision_reason: Optional[str] = None,
    notices: Optional[List[str]] = None,
    fallback_reason: Optional[str] = None,
) -> Dict[str, Any]:
    """门控关（固定管线）的迷你轨迹：决策理由 + 降级告知（无编排步骤流）。"""
    steps: List[Dict[str, Any]] = []
    if decision_reason:
        steps.append(_step(1, "决策", reason=_cap(decision_reason, 200)))
    for i, n in enumerate((notices or [])[: MAX_STEPS - len(steps)], start=len(steps) + 1):
        steps.append(_step(i, "降级告知", reason=_cap(n, 200)))
    return {"enabled": False, "steps": steps, "fallback_reason": fallback_reason}
