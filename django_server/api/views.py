"""Django REST 视图：把 B 侧能力暴露给 C（Android/Web）。

单用户 Demo：无认证，直接操作 runtime 单例。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

from django.http import HttpRequest, HttpResponse, JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from config.settings import settings
from core.schemas import ActionStatus, EventType, MonitorEvent, to_dict
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
    # LLM 按 prompt 规则会把「没有对应信息」的字段填 null，而 A 侧
    # ``_include_meal_time_in_daily_limit`` 对显式 null 报错（要求用户确认）。
    # 备注里通常不含这个信息 → 解析结果里把它降级为历史默认 False，
    # 与 C 端一直没发该字段时的行为一致（缺省即 False）。
    constraints = parsed["content"].get("constraints")
    if isinstance(constraints, dict) and constraints.get(
        "include_meal_time_in_daily_limit"
    ) is None:
        constraints["include_meal_time_in_daily_limit"] = False
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
    # 预算必填（8.30 拍板）：缺失/null → 400 要求补全，不静默默认——
    # 预算参与路线选择（城际降档）与费用核算，缺省会导致"速度快但超支"
    # 的方案无人拦截。备注解析（LLM）产出的 budget 同样受此校验。
    if isinstance(content, dict):
        budget = (content.get("constraints") or {}).get("budget")
        if budget is None or (isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget < 0):
            return _error(
                "缺少总预算（constraints.budget）：预算必填。请提供整趟行程的"
                "人均总预算（元，含城际交通/住宿/餐饮/门票），例如 3000。",
                status=400,
            )
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


# ── 突发事件注入（演示专用）──────────────────────────────────────────────
# 真链路注入：构造 MonitorEvent → agent.handle_event() → 影响判定 →
# 决策（A 侧 BDecisionHook）→ 重规划 → 回填 /api/replans 与 /api/timeline。
# App 零改动：/api/events、/api/replans、/api/timeline 照常轮询即可看到。
# 公网（穿透）演示时建议设环境变量 DEBUG_INJECT_TOKEN，请求带 X-Debug-Token 头。

PRESET_EVENTS: Dict[str, Dict[str, Any]] = {
    "storm": {
        "description": "天气转暴雨（降雨概率 10% → 85%）",
        "event_type": EventType.WEATHER,
        "data": {"condition": "暴雨", "rain_probability": 85, "uv_index": 2},
    },
    "queue": {
        "description": "景点排队暴涨（20 → 120 分钟）",
        "event_type": EventType.SCENIC,
        "data": {"queue_min": 120},
    },
    "traffic_jam": {
        "description": "交通拥堵延误 45 分钟",
        "event_type": EventType.TRAFFIC,
        "data": {"delay_min": 45, "congestion": "拥堵"},
    },
    "hotel_full": {
        "description": "酒店满房（触发换宿决策）",
        "event_type": EventType.BOOKING,
        "data": {"hotel_full": True},
    },
}


def _split_traffic_place(place: str) -> tuple:
    """把 "北京→故宫" / "北京-故宫" / "北京 故宫" 拆成 (origin, destination)。"""
    for sep in ("→", "->", "-", " "):
        if sep in place:
            parts = [p.strip() for p in place.split(sep, 1)]
            if parts[0] and parts[1]:
                return parts[0], parts[1]
    return place, place


def _resolve_hotel_id(place: str, timeline: Any) -> str:
    """按名称从酒店池解析 hotel_id（与 runtime._on_booking_failed 同款映射）。

    假池（DEMO_MODE）路径：place 名称 → 假池 Hotel.id（BJ_HXXX）；
    live 路径：live 酒店不在假池，映射失败原样返回名称——调用方可显式传
    ``data.hotel_id``（如从 /api/timeline/ 的 Place.id 取值，live 换宿才生效）。
    """
    try:
        from data_transmission.hotel import load_hotels

        city = getattr(timeline, "city", None) or ""
        place_key = str(place).replace("（满房）", "").replace("满房", "").strip()
        for h in load_hotels(city):
            if h.name == place_key or str(h.id) == place_key:
                return str(h.id)
        match = next(
            (h for h in load_hotels(city)
             if place_key.startswith(h.name) or h.name.startswith(place_key)),
            None,
        )
        if match is not None:
            return str(match.id)
    except Exception:  # noqa: BLE001  池映射失败回退原名称
        pass
    return place


def _build_inject_event(payload: Dict[str, Any], timeline: Any) -> MonitorEvent:
    """payload → MonitorEvent（校验失败抛 ValueError）。

    body（二选一）：
      {"scenario": "storm|queue|traffic_jam|hotel_full", "place": "故宫", ...}
      {"event_type": "weather|scenic|traffic|food|booking", "place": "...",
       "data": {...}}
    """
    preset = payload.get("scenario")
    if preset is not None:
        spec = PRESET_EVENTS.get(str(preset))
        if spec is None:
            raise ValueError(
                f"unknown scenario: {preset}（可选：{', '.join(PRESET_EVENTS)}）"
            )
        event_type = spec["event_type"]
        data: Dict[str, Any] = dict(spec["data"])
        place = str(payload.get("place") or "")
    else:
        try:
            event_type = EventType(str(payload.get("event_type", "")))
        except ValueError as exc:
            raise ValueError(
                f"invalid event_type: {payload.get('event_type')!r}"
            ) from exc
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            raise ValueError("data must be an object")
        data = dict(data)
        place = str(payload.get("place") or "")
    # place 缺省：天气/交通事件挂城市（与轮询规则语义一致），其余类型必填
    if not place:
        city = getattr(timeline, "city", None) or "北京"
        if event_type in (EventType.WEATHER, EventType.TRAFFIC):
            place = city
        else:
            raise ValueError(f"place is required for event_type={event_type.value}")
    # 预订满房：hotel_id 是 _significant 判定的必填键；未显式传时按名称解析
    # 酒店池 id（2026-08-31：live 酒店映射失败回退名称，可显式传 data.hotel_id）
    if event_type == EventType.BOOKING and not data.get("hotel_id"):
        data["hotel_id"] = _resolve_hotel_id(place, timeline)
    return MonitorEvent(
        event_id=f"inject-{uuid.uuid4().hex[:8]}",
        event_type=event_type,
        place=place,
        observed_at=datetime.now(),
        rule_name=str(payload.get("rule_name") or "debug-inject"),
        spot_id=str(payload.get("spot_id") or ""),
        data=data,
    )


def _apply_persist_world(world: Any, event: MonitorEvent) -> None:
    """把注入状态写进假池（MockWorld）：后续轮询持续看到异常，而非一次性事件。

    Live 模式下 MockWorld 是 override 层（WeatherToolLive 等在 API 数据上
    叠加），因此该写入对 mock / live 两种数据模式都生效。
    """
    data = event.data or {}
    if event.event_type == EventType.WEATHER:
        world.set_weather(
            condition=str(data.get("condition", "暴雨")),
            rain_probability=int(data.get("rain_probability", 85)),
            uv_index=int(data.get("uv_index", 2)),
        )
    elif event.event_type == EventType.SCENIC:
        world.set_queue(event.place, int(data.get("queue_min", 120)))
    elif event.event_type == EventType.TRAFFIC:
        origin, destination = _split_traffic_place(event.place)
        world.set_traffic_delay(
            origin, destination,
            int(data.get("delay_min", 45)),
            congestion=str(data.get("congestion", "拥堵")),
        )
    # BOOKING / FOOD：无 world 状态可写


@csrf_exempt
@require_http_methods(["POST"])
def debug_inject(request: HttpRequest) -> JsonResponse:
    """演示专用：注入突发事件，走真实链路（监控 → 影响判定 → 决策 → 重规划）。

    body（二选一）：
      {"scenario": "storm|queue|traffic_jam|hotel_full", "place": "故宫", ...}
      {"event_type": "weather|scenic|traffic|food|booking", "place": "...",
       "data": {...}}
    可选字段：
      "persist_world": true → 同步写进假池（MockWorld），后续轮询持续可见
        （默认 false：一次性事件，避免轮询再次触发重复决策）；
      "rule_name" / "spot_id" / "observed_at" 透传给 MonitorEvent。
    鉴权：环境变量 DEBUG_INJECT_TOKEN 非空时，要求 X-Debug-Token 请求头。
    前置：必须先 POST /api/plan/（或 /api/timeline/）建好时间轴。
    """
    token = settings.debug_inject_token
    if token and request.headers.get("X-Debug-Token") != token:
        return _error("invalid or missing X-Debug-Token", status=401)
    payload = _json_body(request)
    if not isinstance(payload, dict) or not payload:
        return _error("JSON body required")
    try:
        agent = runtime.require_agent()
        event = _build_inject_event(payload, runtime.timeline)
    except (RuntimeError, ValueError) as exc:
        return _error(str(exc), status=400)
    if not token:
        logger.warning("debug_inject 未设 DEBUG_INJECT_TOKEN，公网可达时建议配置")
    # 可选：同步写进假池（Live 模式同样叠加 override）
    if payload.get("persist_world") and getattr(runtime, "world", None) is not None:
        try:
            _apply_persist_world(runtime.world, event)
        except Exception:  # noqa: BLE001
            logger.exception("persist_world failed")
    # 真链路：缓冲进 /api/events → 影响判定 → DecisionRequest → 重规划
    n_before = len(runtime.replan_history)
    try:
        req = asyncio.run(agent.handle_event(event))
    except Exception:  # noqa: BLE001
        logger.exception("handle_event failed")
        return _error("handle_event failed（见服务端日志）", status=500)
    recorded = len(runtime.replan_history) > n_before
    entry = runtime.replan_history[-1] if recorded else None
    decision = entry.get("decision") if entry else None
    return JsonResponse({
        "status": "ok",
        "scenario": payload.get("scenario"),
        "event": to_dict(event),
        "significant": req is not None,
        "decision": (
            "replanned" if (decision and decision.get("new_timeline"))
            else ("recorded" if recorded
                  else ("hook_error" if req is not None else "not_significant"))
        ),
        "replan": entry,
        "timeline_changed": bool(decision and decision.get("new_timeline")),
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


# ── C 端对话（旅行助手，2026-09-01 新增）───────────────────────────────
# 面向 C 的对话接口：C 端维护会话历史（服务端无状态），请求带当前消息 +
# 历史；服务端把行程上下文（行程摘要 / 需求 / 最近重规划原因）注入系统
# 提示词。v2：对话可调用私有工具 ``update_timeline`` 直接修改时间轴
# （LLM 输出结构化新时间轴 → 服务端校验 → 应用 → 记 replan_history，
# App 轮询自动刷新，前端零改动）。工具不进 registry，仅本会话内生效。

CHAT_HISTORY_LIMIT = 20

# update_timeline 的参数 Schema（与 GET /api/timeline/ 返回结构同构）
_TIMELINE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "城市，如 北京"},
        "start_date": {"type": "string", "description": "开始日期 YYYY-MM-DD"},
        "end_date": {"type": "string", "description": "结束日期 YYYY-MM-DD"},
        "days": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day": {"type": "integer"},
                    "date": {"type": "string", "description": "YYYY-MM-DD"},
                    "items": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "category": {
                                    "type": "string",
                                    "enum": ["scenic", "food", "hotel", "transport"],
                                },
                                "arrival": {"type": "string", "description": "到达时间 HH:MM"},
                                "end_time": {"type": "string", "description": "HH:MM，可空"},
                                "queue_min": {"type": "integer"},
                                "ticket_required": {"type": "boolean"},
                                "price": {"type": "number"},
                            },
                            "required": ["name", "arrival"],
                        },
                    },
                },
                "required": ["day", "date", "items"],
            },
        },
    },
    "required": ["city", "start_date", "end_date", "days"],
}

CHAT_TIMELINE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_timeline",
        "description": (
            "按用户要求修改当前行程时间轴。参数为完整的新时间轴对象"
            "（结构见 parameters）。仅当用户明确要求调整行程（增删景点、"
            "改时间、换顺序）时调用；调用后向用户简要说明改动内容。"
        ),
        "parameters": _TIMELINE_SCHEMA,
    },
}

# 对话可用的只读真源工具（v2.2 精选子集；来自既有 LLM 白名单，只读安全）
CHAT_READONLY_TOOLS = (
    "weather", "weather_brief", "air_quality",
    "food", "traffic", "train_trip", "flight_search", "web_search",
)


def _chat_tools() -> List[Dict[str, Any]]:
    """对话工具列表：update_timeline（私有写）+ 精选只读真源工具。"""
    tools: List[Dict[str, Any]] = [CHAT_TIMELINE_TOOL]
    try:
        provider = ToolProvider(runtime.registry)
        for tool in provider.to_openai_tools():
            name = tool.get("function", {}).get("name")
            if name in CHAT_READONLY_TOOLS:
                tools.append(tool)
    except Exception:  # noqa: BLE001  工具元数据加载失败只留 update_timeline
        logger.warning("chat: 只读工具元数据加载失败，仅保留 update_timeline")
    return tools


def _exec_chat_timeline(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """chat v2 私有工具执行器：校验并应用新时间轴（结果回填给 LLM）。

    v2.2：应用前先跑深度可行性校验（闭馆/每日时长/预算，
    ``timeline_validator``），不通过返回结构化错误由 LLM 调整重试。
    """
    if name != "update_timeline":
        return {"status": "error", "message": f"未知工具 {name}"}
    if not isinstance(arguments, dict):
        return {"status": "error", "message": "参数必须为对象"}
    try:
        runtime.require_agent()  # 未建行程时拒绝
        timeline_obj = runtime.parse_timeline_payload(arguments)
    except (RuntimeError, ValueError) as exc:
        return {"status": "error", "message": f"时间轴不合法：{exc}"}
    from api.timeline_validator import validate_timeline

    validation_errors = validate_timeline(
        timeline_obj, runtime.requirement or {}
    )
    if validation_errors:
        logger.warning(
            "chat update_timeline 校验拒绝: %s",
            "；".join(validation_errors[:5]),
        )
        return {
            "status": "error",
            "message": "时间轴不可行：" + "；".join(validation_errors[:5])
            + "（估算口径：景点时长取候选池、交通 30 分钟/段、餐饮 60 分钟，"
            "请调整方案后重试）",
        }
    try:
        result = runtime.apply_timeline_from_chat(timeline_obj, reason="对话调整")
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat update_timeline failed")
        return {"status": "error", "message": f"应用失败：{exc}"}
    return {
        "status": "applied",
        "message": "行程已更新",
        "diff_summary": result["diff_summary"],
        "timeline": to_dict(timeline_obj),
    }


def _exec_chat_tool(name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
    """对话工具统一分发：update_timeline 走私有逻辑，其余走只读白名单。"""
    if name == "update_timeline":
        return _exec_chat_timeline(name, arguments)
    try:
        provider = ToolProvider(runtime.registry)
        result = provider.call_json(name, arguments or {})
        return {"status": "ok", "result": result}
    except KeyError as exc:
        return {"status": "error", "message": f"工具不可用：{exc}"}
    except Exception as exc:  # noqa: BLE001
        logger.warning("chat tool %s failed: %s", name, exc)
        return {"status": "error", "message": f"工具调用失败：{exc}"}


def _chat_system_prompt() -> str:
    """构建带行程上下文的系统提示词（C 端对话用）。"""
    parts = [
        "你是 TravelAgent 的旅行助手，负责回答用户关于行程与旅行的问题。",
        "只回答与旅行、行程、景点、天气、交通、酒店、餐饮相关的问题；"
        "无关问题请礼貌拒绝。回答使用简体中文，简洁准确，不要使用 Markdown 标题。",
    ]
    tl = runtime.timeline
    if tl is not None:
        lines = [f"当前行程：{tl.city}，{tl.start_date} 至 {tl.end_date}"]
        for day in tl.days:
            items = " → ".join(
                f"{it.name}({it.arrival})" for it in day.items if it.name
            )
            lines.append(f"第{day.day}天（{day.date}）：{items}")
        parts.append("\n".join(lines))
    req = runtime.requirement or {}
    content = req.get("content") or {}
    if isinstance(content, dict):
        pref = content.get("preferences") or {}
        cons = content.get("constraints") or {}
        bits = []
        if content.get("destination"):
            bits.append(f"目的地 {content['destination']}")
        if content.get("days"):
            bits.append(f"{content['days']} 天")
        if cons.get("budget") is not None:
            bits.append(f"预算 {cons['budget']} 元")
        if pref.get("preferred_tags"):
            bits.append(f"偏好 {'、'.join(pref['preferred_tags'])}")
        if cons.get("must_visit"):
            bits.append(f"必去 {'、'.join(cons['must_visit'])}")
        if bits:
            parts.append("用户需求：" + "，".join(bits))
    if runtime.replan_history:
        last = runtime.replan_history[-1]
        d = last.get("decision") or {}
        if d.get("reason"):
            parts.append(f"最近一次行程调整：{str(d['reason'])[:200]}")
    return "\n\n".join(parts)


@csrf_exempt
@require_http_methods(["POST"])
def chat(request: HttpRequest) -> JsonResponse:
    """面向 C 的旅行助手对话：``POST /api/chat/``。

    body::

        {"message": "故宫几点关门？",
         "history": [{"role": "user|assistant", "content": "..."}, ...]}

    ``history`` 由 C 端维护（服务端无状态），仅最近 ``CHAT_HISTORY_LIMIT``
    条生效。返回 ``{"reply": "...", "elapsed_ms": 123}``。
    错误：400 参数不合法；502 LLM 未配置或调用失败。
    """
    payload = _json_body(request)
    if not isinstance(payload, dict) or not payload:
        return _error("JSON body required")
    message = str(payload.get("message") or "").strip()
    if not message:
        return _error("message is required")
    history = payload.get("history") or []
    if not isinstance(history, list):
        return _error("history must be a list")
    cleaned: List[Dict[str, str]] = []
    for item in history[-CHAT_HISTORY_LIMIT:]:
        if not isinstance(item, dict):
            continue
        role = str(item.get("role") or "")
        content = str(item.get("content") or "").strip()
        if role in ("user", "assistant") and content:
            cleaned.append({"role": role, "content": content})
    messages = [
        {"role": "system", "content": _chat_system_prompt()},
        *cleaned,
        {"role": "user", "content": message},
    ]
    try:
        from call_llm.client_factory import create_llm_client

        client = create_llm_client(ask_user_if_missing=False)
    except Exception as exc:  # noqa: BLE001
        logger.error("chat: create_llm_client failed: %s", exc)
        return _error(
            "LLM 未配置（检查 DEEPSEEK_API_KEY / GLM_API_KEY 环境变量）",
            status=502,
        )
    started = time.monotonic()
    try:
        result = client.generate(
            messages,
            tools=_chat_tools(),
            tool_executor=_exec_chat_tool,
            max_tool_rounds=5,   # v2.2：查询真源 + 改行程 + 校验重试需要更多轮
            expect_json=False,   # 对话模式：工具回路后返回自然语言
        )
        reply = str(result.get("content") or "").strip()
        if not reply:
            reply = "已处理你的请求。"
    except ValueError as exc:
        # 工具轮次超限等「流程性」失败：给用户友好提示而非 502
        logger.warning("chat flow rejected: %s", exc)
        return JsonResponse({
            "reply": "抱歉，这次调整没有完成（操作步骤过多）。"
                     "请简化需求或分步提出，例如只调整一个景点。",
            "elapsed_ms": int((time.monotonic() - started) * 1000),
        })
    except Exception as exc:  # noqa: BLE001
        logger.error("chat failed: %s", exc)
        return _error(f"LLM 调用失败: {exc}", status=502)
    elapsed_ms = int((time.monotonic() - started) * 1000)
    return JsonResponse({"reply": reply, "elapsed_ms": elapsed_ms})
