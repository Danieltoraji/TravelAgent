"""Django REST 视图：把 B 侧能力暴露给 C（Android/Web）。

单用户 Demo：无认证，直接操作 runtime 单例。
"""

from __future__ import annotations

import json
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
    payload = _json_body(request)
    try:
        return JsonResponse(runtime.registry.call(name, **payload).to_dict())
    except KeyError as exc:
        return _error(str(exc), status=404)


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
        return _error(str(exc), status=500)


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
            return JsonResponse(to_dict(a))
    return _error(f"Action not found: {action_id}", status=404)


@csrf_exempt
@require_http_methods(["POST"])
def reject_action(request: HttpRequest, action_id: str) -> JsonResponse:
    for a in runtime.booking_manager.actions():
        if a.action_id == action_id:
            a.status = ActionStatus.REJECTED
            return JsonResponse(to_dict(a))
    return _error(f"Action not found: {action_id}", status=404)


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
