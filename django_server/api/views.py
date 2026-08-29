"""Django REST 视图：把 B 侧能力暴露给 C（Android/Web）。

单用户 Demo：无认证，直接操作 runtime 单例。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, Optional

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from config.settings import settings
from core.schemas import ActionStatus, to_dict
from itinerary.ics_exporter import build_ics
from itinerary.markdown_exporter import render_markdown
from runtime.agent_runtime import runtime
from tools import ToolProvider

logger = logging.getLogger("api.views")


def _json_body(request: HttpRequest) -> Dict[str, Any]:
    if not request.body:
        return {}
    try:
        return json.loads(request.body.decode("utf-8"))
    except json.JSONDecodeError:
        return {}


def _error(message: str, status: int = 400) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


# ── 基础 ────────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def health(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok", "project": "TravelAgent-Django"})


@require_http_methods(["GET"])
def status(request: HttpRequest) -> JsonResponse:
    return JsonResponse(runtime.status())


@require_http_methods(["GET"])
def agent_info(request: HttpRequest) -> JsonResponse:
    info = runtime.agent_info()
    if info is None:
        return _error("Timeline not set", status=400)
    return JsonResponse(info)


@require_http_methods(["GET"])
def profile(request: HttpRequest) -> JsonResponse:
    """用户画像占位：当前单用户 Demo 无 A 侧 Memory，后续由 A 填充。"""
    return JsonResponse({
        "user_id": "demo-user",
        "profile": {},
        "note": "A 侧接入后在此返回用户偏好/历史决策",
    })


# ── 工具 ────────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def list_tools(request: HttpRequest) -> JsonResponse:
    names = runtime.registry.names()
    specs = runtime.registry.list_specs()
    return JsonResponse({
        "tools": names,
        "specs": [to_dict(s) for s in specs],
        "count": len(names),
    })


@require_http_methods(["GET"])
def get_tool_spec(request: HttpRequest, name: str) -> JsonResponse:
    try:
        return JsonResponse(to_dict(runtime.registry.get_spec(name)))
    except KeyError as exc:
        return _error(str(exc), status=404)


@csrf_exempt
@require_http_methods(["POST"])
def invoke_tool_llm(request: HttpRequest) -> JsonResponse:
    payload = _json_body(request)
    name = payload.get("name", "")
    if not name:
        return _error("name is required")
    arguments = payload.get("arguments") or {}
    provider = ToolProvider(runtime.registry)
    try:
        return JsonResponse(provider.call_json(name, arguments))
    except KeyError as exc:
        return _error(str(exc), status=404)


@csrf_exempt
@require_http_methods(["POST"])
def invoke_tool(request: HttpRequest, name: str) -> JsonResponse:
    # WARNING（R1）：本端点无 readonly 过滤——booking 等写工具可被直调。
    # 只读白名单入口是 POST /api/tools/invoke/；收紧前需与 C 确认依赖
    # （docs/code_defects_and_fixes_20260828.md R1，2026-08-28 决策暂不动）。
    payload = _json_body(request)
    try:
        return JsonResponse(runtime.registry.call(name, **payload).to_dict())
    except KeyError as exc:
        return _error(str(exc), status=404)


# ── 酒店数据展示端点（C 只读，不触发工具调用）────────────────────────────
# 数据来源：A/Planner/B 调度调用 hotel_tool 时，由 AgentRuntime 自动缓存。

@require_http_methods(["GET"])
def hotels(request: HttpRequest) -> JsonResponse:
    """返回历史 hotel_tool 搜索快照（只读展示）。"""
    return JsonResponse({
        "hotel_search_results": runtime.hotel_search_results,
        "count": len(runtime.hotel_search_results),
        "latest": runtime.hotel_search_results[-1] if runtime.hotel_search_results else None,
    })


@require_http_methods(["GET"])
def hotel_detail(request: HttpRequest, hotel_id: str) -> JsonResponse:
    """返回某个酒店已被查询过的房型/价格明细（只读展示）。"""
    data = runtime.hotel_details.get(hotel_id)
    if data is None:
        return _error(f"Hotel detail not found: {hotel_id}", status=404)
    return JsonResponse(data)


@require_http_methods(["GET"])
def hotel_tags(request: HttpRequest) -> JsonResponse:
    """返回最近一次 hotel_tool tags 调用结果（只读展示）。"""
    if runtime.hotel_tags is None:
        return _error("Hotel tags not available", status=404)
    return JsonResponse(runtime.hotel_tags)


# ── 时间轴 ──────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["GET", "POST"])
def timeline(request: HttpRequest) -> JsonResponse:
    if request.method == "GET":
        if runtime.timeline is None:
            return _error("No timeline set. POST /api/timeline/ first.")
        return JsonResponse(to_dict(runtime.timeline))

    payload = _json_body(request)
    try:
        timeline_obj = runtime.set_timeline_from_payload(payload)
    except Exception as exc:
        return _error(f"Invalid timeline: {exc}")
    return JsonResponse({
        "status": "ok",
        "message": "Timeline set",
        "timeline": to_dict(timeline_obj),
    })


@require_http_methods(["GET"])
def timeline_history(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        "history": runtime.timeline_history,
        "count": len(runtime.timeline_history),
    })


# ── A 侧需求 → 规划（AB 合码方案 §三.5）────────────────────────────────────


def _parse_free_text_requirement(payload: Dict[str, Any]) -> Dict[str, Any]:
    """备注解析（问题六修复，8.30）：非空 ``free_text_requirement`` → LLM 结构化。

    把 C 端备注框的原始文本 + 表单字段整体交给 A 的
    ``call_llm.parse_input.parse_requirement_input``（LLM 把备注语义归并到
    preferred_tags / avoid_tags / constraints / food_preferences /
    travel_priority / hotel_preferences，标签映射到知识库标准名）。

    - 解析成功 → 返回解析后的完整需求（表单数值字段由解析 prompt 规则 1
      原样保留，语义字段是新增量）；
    - 任何失败（无 key / LLM 超时 / 结果不可解析 / content 缺失）→ 原样
      返回 payload（按无备注规划，绝不阻断主链路）；
    - 备注为空 → 原样返回（不产生 LLM 调用，零延迟）。
    """
    content = payload.get("content") if isinstance(payload, dict) else None
    remark = content.get("free_text_requirement") if isinstance(content, dict) else None
    if not isinstance(remark, str) or not remark.strip():
        return payload
    try:
        from call_llm.parse_input import parse_requirement_input

        parsed = parse_requirement_input(payload)
    except Exception as exc:  # noqa: BLE001  备注解析失败不阻断规划
        logger.warning("free_text_requirement LLM 解析失败，按无备注规划：%s", exc)
        return payload
    if not isinstance(parsed, dict) or not isinstance(parsed.get("content"), dict):
        logger.warning("free_text_requirement LLM 解析结果形状异常，按无备注规划")
        return payload
    return parsed


@csrf_exempt
@require_http_methods(["POST"])
def plan(request: HttpRequest) -> JsonResponse:
    """A 侧需求 JSON → A 的 Planner → TripTimeline（``POST /api/plan/``）。

    body 为 A 侧结构化需求（沿 A 现有管线，含 ``content`` 键，见
    ``data_transmission/requirement.py`` 的 requirement_schema），
    例如 ``{"content": {"destination": "北京", "days": 2, "constraints": {...}}}``。
    ``content.free_text_requirement``（C 端备注框原文）非空时先经 LLM 解析成
    结构化需求再规划（失败自动按无备注规划，见 ``_parse_free_text_requirement``）。
    规划失败（BPlannerHook 降级为空时间轴）返回 400 + ``planner_error``。
    旧 ``POST /api/timeline/`` 保留：C 直接喂时间轴的兼容路径。
    """
    payload = _json_body(request)
    if not payload:
        return _error("requirement JSON body required")
    payload = _parse_free_text_requirement(payload)
    # B1（8.28 反馈）：C 端一直发 `departure_location` 但从未映射到
    # `content.origin`（views 零引用）→ 一行映射（已传 origin 则以其为准）。
    content = payload.get("content") if isinstance(payload, dict) else None
    if isinstance(content, dict) and not content.get("origin") and content.get("departure_location"):
        content["origin"] = content.pop("departure_location")
    try:
        timeline_obj = runtime.init_from_requirement(payload)
    except Exception as exc:
        return _error(f"plan failed: {exc}", status=500)
    if not timeline_obj.days:
        err = getattr(runtime, "_last_planner_error", None) or "planner produced empty timeline"
        return _error(f"规划失败：{err}")
    return JsonResponse({
        "status": "ok",
        "message": "Timeline generated from requirement",
        "timeline": to_dict(timeline_obj),
        "planner_error": None,
    })


# ── 预约 ────────────────────────────────────────────────────────────────

@csrf_exempt
@require_http_methods(["POST"])
def booking_prepare(request: HttpRequest) -> JsonResponse:
    payload = _json_body(request)
    place = payload.get("place", "")
    target_date = payload.get("target_date", "")
    party_size = int(payload.get("party_size", 1))
    booking_type = payload.get("booking_type", "scenic")
    try:
        rec = runtime.booking_manager.prepare(
            place=place, target_date=target_date,
            party_size=party_size, booking_type=booking_type,
        )
        return JsonResponse(to_dict(rec))
    except RuntimeError as exc:
        return _error(str(exc), status=500)


@csrf_exempt
@require_http_methods(["POST"])
def booking_confirm(request: HttpRequest, booking_id: str) -> JsonResponse:
    try:
        rec = runtime.booking_manager.confirm(booking_id)
        return JsonResponse(to_dict(rec))
    except KeyError as exc:
        return _error(str(exc), status=404)
    except ValueError as exc:
        return _error(str(exc), status=400)
    except RuntimeError as exc:
        # 预订提交失败（业务失败，如满房）：400 + 结构化信息（对 C 端友好），
        # 而非 500 空 body——原实现会让前端抛 "API error 500" 且无任何细节。
        # 修复 0827：附带失败后的 booking 状态（FAILED）与该预约的 Action（BLOCKED）。
        try:
            rec = runtime.booking_manager.get(booking_id)
            actions = [
                to_dict(a) for a in runtime.booking_manager.actions()
                if a.target == f"booking:{booking_id}"
            ]
            return JsonResponse({
                "error": str(exc),
                "booking": to_dict(rec),
                "actions": actions,
            }, status=400)
        except KeyError:
            return _error(str(exc), status=400)


@csrf_exempt
@require_http_methods(["POST"])
def booking_cancel(request: HttpRequest, booking_id: str) -> JsonResponse:
    try:
        rec = runtime.booking_manager.cancel(booking_id)
        return JsonResponse(to_dict(rec))
    except KeyError as exc:
        return _error(str(exc), status=404)


@csrf_exempt
@require_http_methods(["POST"])
def booking_payment(request: HttpRequest, booking_id: str) -> JsonResponse:
    try:
        item = runtime.booking_manager.payment_action(booking_id)
        return JsonResponse(to_dict(item))
    except KeyError as exc:
        return _error(str(exc), status=404)


@require_http_methods(["GET"])
def list_bookings(request: HttpRequest) -> JsonResponse:
    records = runtime.booking_manager.records()
    return JsonResponse({
        "bookings": [to_dict(r) for r in records],
        "count": len(records),
    })


@require_http_methods(["GET"])
def get_booking(request: HttpRequest, booking_id: str) -> JsonResponse:
    try:
        rec = runtime.booking_manager.get(booking_id)
        return JsonResponse(to_dict(rec))
    except KeyError as exc:
        return _error(str(exc), status=404)


# ── Action Queue ────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def list_actions(request: HttpRequest) -> JsonResponse:
    actions = runtime.booking_manager.actions()
    return JsonResponse({
        "actions": [to_dict(a) for a in actions],
        "count": len(actions),
    })


@csrf_exempt
@require_http_methods(["POST"])
def approve_action(request: HttpRequest, action_id: str) -> JsonResponse:
    for a in runtime.booking_manager.actions():
        if a.action_id == action_id:
            a.status = ActionStatus.APPROVED
            a.decided_at = datetime.now().isoformat(timespec="seconds")
            a.decided_by = "c_end_user"   # 单用户 Demo：无认证体系
            # E1：hotel: 动作批准即执行真实预订（此前为死信）。
            # booking:/payment: 等 target 保持仅标记（E2 语义变更待 C 确认）。
            if a.target.startswith("hotel:"):
                try:
                    runtime.booking_manager.execute_action(a)
                except Exception as exc:  # noqa: BLE001  预订失败 → 动作置 BLOCKED
                    a.status = ActionStatus.BLOCKED
                    a.description = (a.description + "；" if a.description else "") + str(exc)
                    return JsonResponse({
                        "error": str(exc), "action": to_dict(a),
                    }, status=400)
            return JsonResponse(to_dict(a))
    return _error(f"Action not found: {action_id}", status=404)


@csrf_exempt
@require_http_methods(["POST"])
def reject_action(request: HttpRequest, action_id: str) -> JsonResponse:
    for a in runtime.booking_manager.actions():
        if a.action_id == action_id:
            a.status = ActionStatus.REJECTED
            a.decided_at = datetime.now().isoformat(timespec="seconds")
            a.decided_by = "c_end_user"
            return JsonResponse(to_dict(a))
    return _error(f"Action not found: {action_id}", status=404)


@csrf_exempt
@require_http_methods(["POST"])
def booking_mark_confirmed(request: HttpRequest, booking_id: str) -> JsonResponse:
    """E4：服务方确认回调（SUBMITTED → CONFIRMED；Demo 期人工/脚本触发）。"""
    try:
        rec = runtime.booking_manager.mark_confirmed(booking_id)
        return JsonResponse(to_dict(rec))
    except KeyError as exc:
        return _error(str(exc), status=404)
    except ValueError as exc:
        return _error(str(exc), status=400)


# ── 监控事件 ────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def list_events(request: HttpRequest) -> JsonResponse:
    try:
        since = int(request.GET.get("since", 0))
    except ValueError:
        return _error("since must be an integer")
    events = runtime.events[since:]
    return JsonResponse({
        "events": [to_dict(e) for e in events],
        "count": len(events),
        "total": len(runtime.events),
    })


@require_http_methods(["GET"])
def list_replans(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        "replans": runtime.replan_history,
        "count": len(runtime.replan_history),
    })


@require_http_methods(["GET"])
def get_replan(request: HttpRequest, index: int) -> JsonResponse:
    if index < 1 or index > len(runtime.replan_history):
        return _error("Replan not found", status=404)
    return JsonResponse(runtime.replan_history[index - 1])


@require_http_methods(["GET"])
def tool_calls(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        "tool_calls": runtime.tool_call_log,
        "count": len(runtime.tool_call_log),
    })


@csrf_exempt
@require_http_methods(["POST"])
def execution_poll(request: HttpRequest) -> JsonResponse:
    try:
        events = runtime.poll()
    except RuntimeError as exc:
        return _error(str(exc), status=400)
    return JsonResponse({
        "status": "ok",
        "events": [to_dict(e) for e in events],
        "count": len(events),
    })


@csrf_exempt
@require_http_methods(["POST"])
def execution_lookahead(request: HttpRequest) -> JsonResponse:
    payload = _json_body(request)
    try:
        if payload and "now" in payload:
            now = datetime.fromisoformat(payload["now"])
        else:
            now = datetime.now()
        events = runtime.lookahead(now)
    except RuntimeError as exc:
        return _error(str(exc), status=400)
    except ValueError as exc:
        return _error(str(exc), status=400)
    return JsonResponse({
        "status": "ok",
        "events": [to_dict(e) for e in events],
        "count": len(events),
    })


# ── 导出 ────────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def export_ics(request: HttpRequest) -> HttpResponse:
    if runtime.timeline is None:
        return _error("No timeline set", status=400)
    content = build_ics(runtime.timeline)
    if request.GET.get("raw") == "1":
        return HttpResponse(content, content_type="text/calendar; charset=utf-8")
    return JsonResponse({"content": content})


@require_http_methods(["GET"])
def export_markdown(request: HttpRequest) -> HttpResponse:
    if runtime.timeline is None:
        return _error("No timeline set", status=400)
    content = render_markdown(runtime.timeline)
    if request.GET.get("raw") == "1":
        return HttpResponse(content, content_type="text/markdown; charset=utf-8")
    return JsonResponse({"content": content})


# ── 配置 ────────────────────────────────────────────────────────────────

@require_http_methods(["GET"])
def config_info(request: HttpRequest) -> JsonResponse:
    return JsonResponse({
        "demo_mode": settings.demo_mode,
        "use_real_api": settings.use_real_api,
        "use_real_map_api": settings.use_real_map_api,
        "scenic_lookahead_min": settings.scenic_lookahead_min,
        "food_lookahead_min": settings.food_lookahead_min,
        "polling": {
            "weather_interval_s": settings.polling.weather_interval_s,
            "traffic_interval_s": settings.polling.traffic_interval_s,
            "scenic_interval_s": settings.polling.scenic_interval_s,
            "food_interval_s": settings.polling.food_interval_s,
        },
    })


@csrf_exempt
@require_http_methods(["POST"])
def config_reload(request: HttpRequest) -> JsonResponse:
    settings.reload()
    return JsonResponse({
        "status": "ok",
        "demo_mode": settings.demo_mode,
        "use_real_api": settings.use_real_api,
        "use_real_map_api": settings.use_real_map_api,
    })
