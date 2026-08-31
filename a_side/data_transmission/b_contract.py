"""B 契约桥接层：A 侧内部格式 ↔ B 的 ``core/schemas.py`` 契约互转（纯函数，可离线测试）。

按 B 仓库 README §4.1 / 契约责任表（§5.2）对齐 A 的接口格式：

- **Planner** 产出 \u2192 ``PlannerOutput``（``requirement_to_planner_output``）
- **Route Planner** 产出 \u2192 ``TripTimeline``（``plan_to_trip_timeline``）；
  B 侧时间轴 \u2192 A 内部计划（``trip_timeline_to_plan``，best-effort 逆向）
- **Decision Engine 消费**：B 的 ``MonitorEvent`` \u2192 A 的事件 dict
  （``monitor_events_to_a_events``）
- **RePlanner 产出** \u2192 ``ReplanRequest``（``replan_result_to_replan_request``）

**类身份约定（重要）**：本模块一律使用**顶层 ``import core.schemas``**，
不写 ``TravelAgent.core.schemas``。原因：联调时 A 的代码会跑进 B 的进程
（例如 B 的 ``django_server/runtime/a_interface.py`` 替换 ``build_decision_hook``），
此时 ``sys.path`` 里 B 仓库在前，``core.schemas`` 解析为 **B 的契约类**，
B 侧 ``isinstance(replan, ReplanRequest)`` 才成立；A 仓库独立运行时则解析为
本仓库 ``core/`` 的契约副本。两条路径下 ``core.schemas`` 都是进程内唯一类身份。
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.schemas import (  # noqa: E402  顶层导入，保证与进程内 core 包同源
    DayPlan,
    EventType,
    MonitorEvent,
    Place,
    PlannerOutput,
    ReplanRequest,
    TripTimeline,
)
from data_transmission.decision import DECISION_THRESHOLD  # noqa: E402
from data_transmission.itinerary import (  # noqa: E402
    build_itinerary_node,
    format_minutes,
)

# A 行程节点类型 → B 的 Place.category
NODE_TYPE_TO_CATEGORY = {
    "spot": "scenic",
    "meal": "food",
    "transport": "transport",
    "waiting": "scenic",  # 等待段无对应类目，归入 scenic 占位
}
# B 的 Place.category → A 行程节点类型（逆向映射，best-effort）
CATEGORY_TO_NODE_TYPE = {
    "scenic": "spot",
    "food": "meal",
    "transport": "transport",
    "shopping": "spot",
    "hotel": "spot",  # 酒店在 A 侧无专用节点类型，按景点位占位处理
}

__all__ = [
    "NODE_TYPE_TO_CATEGORY",
    "CATEGORY_TO_NODE_TYPE",
    "requirement_to_planner_output",
    "plan_to_trip_timeline",
    "trip_timeline_to_plan",
    "monitor_events_to_a_events",
    "changes_to_diff_summary",
    "replan_result_to_replan_request",
]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return date.today()
    return date.today()


def _int_or(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_minutes(value: Any) -> int:
    return max(0, _int_or(value, 0))


def _hhmm_to_minutes(value: str) -> int:
    """'HH:MM' → 从 00:00 起的分钟数；解析失败返回 0。"""
    if not value:
        return 0
    parts = str(value).split(":")
    if len(parts) != 2:
        return 0
    hh = _int_or(parts[0], -1)
    mm = _int_or(parts[1], -1)
    if hh < 0 or mm < 0 or hh > 23 or mm > 59:
        return 0
    return hh * 60 + mm


def _event_type(ev: MonitorEvent) -> Optional[EventType]:
    """兼容 Enum 与裸字符串的 event_type，统一成 EventType。"""
    et = ev.event_type
    if isinstance(et, EventType):
        return et
    if isinstance(et, str):
        try:
            return EventType(et)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Planner 输出对齐
# ---------------------------------------------------------------------------

def requirement_to_planner_output(requirement: Dict[str, Any]) -> PlannerOutput:
    """A 的结构化需求 → B 的 ``PlannerOutput``（Planner 输出契约）。

    B 的 ``PlannerOutput`` 只含最小编制字段；A 的其余需求细节（必去、每日时长、
    用餐口径等）继续留在 A 的 ``Requirement`` 里，不丢信息。
    """
    if not isinstance(requirement, dict):
        return PlannerOutput(city="", days=0, budget=0.0)
    content = requirement.get("content", requirement)
    if not isinstance(content, dict):
        content = {}
    constraints = content.get("constraints") or {}
    preferences = content.get("preferences") or {}
    return PlannerOutput(
        city=str(content.get("destination") or ""),
        days=_int_or(content.get("days"), 0),
        budget=float(_int_or(constraints.get("budget"), 0)),
        interests=[str(x) for x in (preferences.get("preferred_tags") or [])],
        avoid=[str(x) for x in (preferences.get("avoid_tags") or [])],
    )


# ---------------------------------------------------------------------------
# Route Planner 输出对齐（A plan ⟷ TripTimeline）
# ---------------------------------------------------------------------------

def _node_to_place(node: Dict[str, Any]) -> Place:
    details = node.get("details") or {}
    location = details.get("location")
    lat = lng = 0.0
    if isinstance(location, dict):
        lat = float(location.get("lat") or 0.0)
        lng = float(location.get("lng") or 0.0)
    elif isinstance(location, (list, tuple)) and len(location) >= 2:
        lat, lng = float(location[0] or 0.0), float(location[1] or 0.0)
    # 2026-08-31：is_must_visit 透传进 Place.details——B 侧发起的 replan
    # （current_timeline 往返）依赖它在 RePlanner 中恢复 must 保护，
    # 此前该字段在 A→B 转换时丢失导致 live 计划重规划时必去景点可被删。
    # 2026-09-01：dining_note（午餐错过窗口 → 景区内就餐）一并透传，
    # C 端时间轴景点备注可读「景区内就餐」，A/B 往返不丢。
    place_details: Dict[str, Any] = {}
    if details.get("is_must_visit"):
        place_details["is_must_visit"] = True
    if details.get("dining_note"):
        place_details["dining_note"] = str(details["dining_note"])
    return Place(
        id=str(details.get("spot_id") or ""),
        name=str(node.get("name") or ""),
        lat=lat,
        lng=lng,
        category=NODE_TYPE_TO_CATEGORY.get(node.get("type"), "scenic"),
        arrival=format_minutes(_safe_minutes(node.get("start_minutes"))),
        end_time=format_minutes(_safe_minutes(node.get("end_minutes"))),
        queue_min=_safe_minutes(details.get("queue_min")),
        ticket_required=bool(details.get("ticket_required")),
        price=float(details.get("price") or 0.0),
        # B2 扩展（0828）：meal 段 details 带 RestaurantResolver 选出的餐厅
        # （restaurant_id/restaurant_name/cuisine/average_cost），透传给 C 端展示
        restaurant_name=str(details.get("restaurant_name") or ""),
        cuisine=[str(c) for c in (details.get("cuisine") or [])],
        average_cost=float(details.get("average_cost") or 0.0),
        # 8.30 预算口径：讲解费明细（人均 × 人数 的汇总在 TripTimeline.cost_breakdown）
        guide_price=float(details.get("guide_price") or 0.0),
        details=place_details,
    )


def _attach_trip_segment_places(
    plan: Dict[str, Any], days: List[DayPlan]
) -> None:
    """把 ``plan["trip_segments"]``（城际来去程段）映射进 ``days`` 对应日期。

    - 段含 ``day_label``（YYYY-MM-DD）→ 匹配 ``DayPlan.date``；找不到该日 → 跳过；
    - 去程（``details.kind == "outbound"``）插到当天 **items 头部**（先出发再游玩），
      返程（``return``）追加 **items 尾部**（游玩毕再返程）；
    - ``Place.details`` 透传段 details（mode / from_station / to_station /
      cost_per_person / source / legs），C 端可读客运站对与两段式衔接结构；
      ``price`` 带人均费用（批次 3 起计入 ``cost_breakdown.transit``）。
    """
    segments = plan.get("trip_segments") or []
    if not segments:
        return
    day_by_label = {d.date.isoformat(): d for d in days}
    for seg in segments:
        if not isinstance(seg, dict) or seg.get("type") != "transport":
            continue
        day = day_by_label.get(str(seg.get("day_label") or ""))
        if day is None:
            continue
        details = seg.get("details") or {}
        place = Place(
            name=str(seg.get("name") or ""),
            category="transport",
            arrival=format_minutes(_safe_minutes(seg.get("start_minutes"))),
            end_time=format_minutes(_safe_minutes(seg.get("end_minutes"))),
            price=float(details.get("cost_per_person") or 0.0),
            details=dict(details),
        )
        if details.get("kind") == "return":
            day.items.append(place)
        else:
            day.items.insert(0, place)


def plan_to_trip_timeline(
    plan: Dict[str, Any],
    *,
    city: str = "",
    start_date: Any = None,
    end_date: Any = None,
    plan_id: str = "",
) -> TripTimeline:
    """A 侧计划（``plan_multi_day`` / ``replan().new_plan``）→ B 的 ``TripTimeline``。

    每个 ``day`` 的 ``route_details`` 节点按 ``NODE_TYPE_TO_CATEGORY`` 映射为
    ``DayPlan(items=[Place(...)])``；``start_date`` 缺省取今天，逐日累加日期，
    也可显式传 ``YYYY-MM-DD`` 或 ``date``。跨天搬移后计划天数可能与需求天数不同，
    以计划实际天数决定 ``end_date``。

    住宿段（酒店初始规划接入 8.27）：``plan.get("accommodation")`` 由
    ``transport.hotels.select_hotels_for_plan`` 产出（main.py / BPlannerHook
    写入，replan 换宿后由 replanner 更新），按晚数映射到每天末尾一个
    ``Place(category="hotel")``（当晚酒店）。

    8.30 预算口径（批次 3 扩展五项）：``total_cost`` = 门票 + 讲解 + 酒店 + 餐饮
    + **城际交通**（`transit`，去程+返程人均价 × 人数；市内交通金额小不计），由
    ``algorithoms._common._plan_cost_summary`` 单一来源计算；明细写入
    ``TripTimeline.cost_breakdown``（ticket/guide/hotel/meal/transit/total），
    供 C 端拆分展示。
    """
    plan = plan or {}
    days_in = plan.get("days") or []
    start = _as_date(start_date)
    if end_date is None and days_in:
        end_date = start + timedelta(days=max(0, len(days_in) - 1))
    end = _as_date(end_date) if end_date is not None else start

    acc = plan.get("accommodation") or {}
    bookings = acc.get("bookings") or []
    nights = acc.get("nights") or len(bookings)

    days: List[DayPlan] = []
    for day in days_in:
        day_num = max(1, _int_or(day.get("day"), 1))
        day_date = start + timedelta(days=day_num - 1)
        items = [_node_to_place(node) for node in (day.get("route_details") or [])]
        if bookings:
            night_index = min(day_num - 1, len(bookings) - 1)
            book = bookings[night_index]
            # 酒店时间随行程动态（9.2，不再写死 20:00）：取「当天最后事件结束
            # 时刻 / 城际到达时刻」的较大值，保底 20:00——空天（晚到达日）落在
            # 到达时刻或 20:00，行程结束晚（如夜景 22:30）则酒店顺延到结束。
            last_end_minutes = max(
                (
                    _safe_minutes(node.get("end_minutes"))
                    for node in (day.get("route_details") or [])
                ),
                default=0,
            )
            for seg in plan.get("trip_segments") or []:
                if not isinstance(seg, dict) or seg.get("type") != "transport":
                    continue
                if str(seg.get("day_label") or "") != day_date.isoformat():
                    continue
                if (seg.get("details") or {}).get("kind") == "outbound":
                    last_end_minutes = max(
                        last_end_minutes, _safe_minutes(seg.get("end_minutes"))
                    )
            hotel_arrival = format_minutes(max(last_end_minutes, 20 * 60))
            items.append(
                Place(
                    id=str(book.get("hotel_id") or ""),
                    name=str(book.get("hotel_name") or "酒店"),
                    category="hotel",
                    arrival=hotel_arrival,   # 随当天行程结束动态（9.2）
                    price=float(book.get("price") or 0.0),
                    # A4 修复（8.30）：酒店真实坐标（bookings 带 lat/lng，
                    # 由 select_hotels_for_plan 产出）——C 端地图可标注酒店，
                    # 不再整片坐标 0。
                    lat=float(book.get("lat") or 0.0),
                    lng=float(book.get("lng") or 0.0),
                )
            )
        days.append(
            DayPlan(
                day=day_num,
                date=day_date,
                items=items,
            )
        )
    # 批次 2（城际来去程 A1 闭环）：plan["trip_segments"]（build_trip_segments
    # 产出）→ 头/尾 transport 段——去程（kind=outbound）插入当天 items 头部、
    # 返程（kind=return）追加尾部；段 details 透传 mode/车站对/cost/source/legs
    # （两段式决策：城际方式已定，市内衔接 legs 预留供阶段三填充）。
    _attach_trip_segment_places(plan, days)
    # 8.30 预算口径（批次 3 扩展五项：门票/讲解/餐饮/酒店/城际交通），
    # 单一来源 _plan_cost_summary
    from algorithoms._common import _plan_cost_summary

    summary = _plan_cost_summary(plan)
    return TripTimeline(
        id=plan_id,
        city=city,
        start_date=start,
        end_date=end,
        days=days,
        total_cost=summary["total"],
        cost_breakdown={
            "ticket": summary["ticket"],
            "guide": summary["guide"],
            "meal": summary["meal"],
            "hotel": summary["hotel"],
            "transit": summary["transit"],
            "total": summary["total"],
        },
        walking_distance=float(plan.get("total_route_distance_km") or 0.0),
    )


def trip_timeline_to_plan(
    timeline: TripTimeline,
    *,
    daily_travel_time: int = 480,
) -> Dict[str, Any]:
    """B 的 ``TripTimeline`` → A 内部计划 dict（best-effort 逆向）。

    用于 A 的 RePlanner 在 B 进程内直接消费 ``DecisionRequest.current_timeline``。
    逆向是启发式的：A 侧统计字段（预算、剩余时长等）无法从时间轴恢复时取默认值，
    ``Place.id`` 会透传为 ``route_details[].details.spot_id`` 供 ``_build_changes``
    做 diff。跨天搬移 / 多次重规划后，首次以后以 A 侧自维护的 ``_current_plan`` 为准。
    """
    days: List[Dict[str, Any]] = []
    for day in timeline.days:
        route_details: List[Dict[str, Any]] = []
        for item in (day.items or []):
            if item.category == "hotel":
                # 酒店段不参与景点排程逆向（住宿由 plan.accommodation 承载）
                continue
            node_type = CATEGORY_TO_NODE_TYPE.get(item.category, "spot")
            start = _hhmm_to_minutes(item.arrival)
            end = _hhmm_to_minutes(item.end_time) if item.end_time else start
            if end < start:
                end = start
            if not item.name:
                continue
            node_details: Dict[str, Any] = {"spot_id": str(item.id or "")}
            # 2026-08-31：B Place.details 里的 must 标记还原进节点，
            # RePlanner 的 must 保护在 current_timeline 往返链路中生效。
            if (item.details or {}).get("is_must_visit"):
                node_details["is_must_visit"] = True
            # 2026-09-01：dining_note（午餐错过窗口 → 景区内就餐）同步还原，
            # A/B 往返不丢，重规划后 C 端备注仍可见。
            if (item.details or {}).get("dining_note"):
                node_details["dining_note"] = str(item.details["dining_note"])
            route_details.append(
                build_itinerary_node(
                    node_type,
                    item.name,
                    start,
                    end,
                    node_details,
                )
            )
        days.append(
            {"day": _int_or(day.day, len(days) + 1), "route_details": route_details,
             "feasible": True}
        )
    return {
        "feasible": True,
        "days": days,
        "daily_travel_time": max(1, _int_or(daily_travel_time, 480)),
        "include_meal_time_in_daily_limit": False,
        "budget": None,
    }


# ---------------------------------------------------------------------------
# Decision Engine 消费对齐（B MonitorEvent → A 事件 dict）
# ---------------------------------------------------------------------------

def _booking_to_hotel_event(
    data: Dict[str, Any],
    place: str,
) -> Optional[Dict[str, Any]]:
    """BOOKING ``MonitorEvent.data`` → A 侧 hotel 事件 dict（无 ``hotel_id`` 返回 None）。

    对齐 A 侧消费方（``replanner._translate_hotel_event`` / ``decision_engine``
    硬规则）的字段约定：

    - ``hotel_id``：酒店标识（**必填**，缺失则跳过——BOOKING 可能是门票预订）
    - ``hotel_name``：展示名，缺省用事件的 ``place``
    - ``hotel_full``：满房标记（truthy → A 侧硬不可行直接触发）
    - ``price_delta``：每晚价格增量（元，可选；经 ``_translate_hotel_event``
      计入酒店重选价格增量）
    """
    if not isinstance(data, dict):
        return None
    hotel_id = str(data.get("hotel_id") or "").strip()
    if not hotel_id:
        return None
    full = bool(data.get("hotel_full"))
    raw_delta = data.get("price_delta")
    delta = None if raw_delta is None else _int_or(raw_delta, 0)

    name = str(data.get("hotel_name") or place or hotel_id)
    metrics: Dict[str, Any] = {"hotel_id": hotel_id, "hotel_full": 1 if full else 0}
    if delta is not None:
        metrics["price_delta"] = delta

    if full:
        detail, severity = f"酒店 {name} 满房", "high"
    elif delta is not None:
        detail, severity = f"酒店 {name} 每晚价格变化 {delta:+.0f} 元", "medium"
    else:
        detail, severity = f"酒店 {name} 预订状态变化", "medium"
    return {
        "event_type": "hotel",
        "spot": name,
        "severity": severity,
        "detail": detail,
        "metrics": metrics,
    }


def monitor_events_to_a_events(
    events: Sequence[MonitorEvent],
    *,
    impact_threshold: int = DECISION_THRESHOLD,
) -> List[Dict[str, Any]]:
    """B 的 ``MonitorEvent`` 列表 → A 侧事件 dict（``data_transmission/decision.py`` 格式）。

    B 只把通过 ``ExecutionAgent._significant`` 门槛的事件放进 ``DecisionRequest``
    （weather: 降雨概率≥60% / scenic: 排队≥阈值 / traffic: 延误≥30 分钟 /
    booking: 预订失败含 hotel 信息），因此这里只需翻译这四种。映射规则：

    - ``weather`` → ``{"event_type": "weather", "metrics": {"rain_probability": ...}}``
    - ``scenic`` → ``{"event_type": "queue", "metrics": {"queue_minutes": ...}}``
    - ``traffic`` → ``{"event_type": "traffic", "metrics": {"travel_time_delta_minutes": ...}}``
    - ``booking`` → ``{"event_type": "hotel", "metrics": {"hotel_id", "hotel_full", "price_delta"}}``
      （仅当 data 含 ``hotel_id``；``hotel_full`` 走 A 侧硬规则直接触发换酒店）

    严重度按数值相对阈值折算，供 A 的 LLM 决策与展示使用。
    """
    out: List[Dict[str, Any]] = []
    threshold = max(1, _int_or(impact_threshold, DECISION_THRESHOLD))
    for ev in events:
        et = _event_type(ev)
        if et is None:
            continue
        data = ev.data if isinstance(ev.data, dict) else {}
        place = str(ev.place or "")

        if et is EventType.WEATHER:
            rain = max(0, _int_or(data.get("rain_probability"), 0))
            severity = "high" if rain >= 60 else ("medium" if rain >= 40 else "low")
            out.append(
                {
                    "event_type": "weather",
                    "spot": place,
                    "severity": severity,
                    "detail": f"降雨概率 {rain}%",
                    "metrics": {"rain_probability": rain},
                }
            )
        elif et is EventType.SCENIC:
            queue_minutes = max(0, _int_or(data.get("queue_min"), 0))
            severity = (
                "high" if queue_minutes >= 2 * threshold
                else ("medium" if queue_minutes >= threshold else "low")
            )
            out.append(
                {
                    "event_type": "queue",
                    "spot": place,
                    "severity": severity,
                    "detail": f"{place} 排队时长 {queue_minutes} 分钟",
                    "metrics": {"queue_minutes": queue_minutes},
                }
            )
        elif et is EventType.TRAFFIC:
            delay = max(0, _int_or(data.get("delay_min"), 0))
            severity = "high" if delay >= 30 else "low"
            out.append(
                {
                    "event_type": "traffic",
                    "spot": place,
                    "severity": severity,
                    "detail": f"{place} 交通延误 {delay} 分钟",
                    "metrics": {"travel_time_delta_minutes": delay},
                }
            )
        elif et is EventType.BOOKING:
            hotel = _booking_to_hotel_event(data, place)
            if hotel is not None:
                out.append(hotel)
        # FOOD / CALENDAR：B 侧不会进入 DecisionRequest（_significant=False），
        # 不做语义翻译；如后续 B 放行更多类型，在此扩展映射即可。
    return out


# ---------------------------------------------------------------------------
# RePlanner 输出对齐（A replan() 结果 → B ReplanRequest）
# ---------------------------------------------------------------------------

def changes_to_diff_summary(changes: Sequence[Dict[str, Any]]) -> List[str]:
    """A 的 ``changes``（removed/added/move/rescheduled）→ 可解释修改点列表。

    直接落入 ``ReplanRequest.diff_summary``，供 B/C 展示"为什么改"。
    """
    out: List[str] = []
    for ch in changes or []:
        if not isinstance(ch, dict):
            continue
        ctype = str(ch.get("type") or "")
        spot = str(ch.get("spot") or "")
        fro = ch.get("from")
        to = ch.get("to")
        reason = ch.get("reason")
        seg = f"[{ctype}] {spot}" if spot else f"[{ctype}]"
        if fro and to and str(fro) != str(to):
            seg += f"：{fro} → {to}"
        if reason:
            seg += f"（{reason}）"
        out.append(seg)
    return out


def replan_result_to_replan_request(
    result: Dict[str, Any],
    *,
    city: str = "",
    start_date: Any = None,
    plan_id: str = "",
    reason_prefix: str = "",
    impact: float = 0.0,
) -> ReplanRequest:
    """A 的 ``replan()`` 结果 → B 的 ``ReplanRequest``。

    - ``new_timeline`` 取 ``result["new_plan"]`` 转换后的时间轴
    - ``reason`` = 决策原因（reason_prefix）+ RePlanner notes
    - ``diff_summary`` = ``changes_to_diff_summary(changes)``
    - ``affected_spots`` = 新计划中所有景点 ID（跨天搬移后以新计划为准）
    - ``impact`` 0-1，由调用方（如 Decision Engine 的 score/100）传入
    """
    result = result or {}
    new_plan = result.get("new_plan")
    new_timeline = (
        plan_to_trip_timeline(
            new_plan,
            city=city,
            start_date=start_date,
            plan_id=plan_id,
        )
        if new_plan
        else None
    )

    notes: List[str] = [str(x) for x in (result.get("notes") or [])]
    if reason_prefix:
        notes.insert(0, str(reason_prefix))
    reason = "；".join(notes) or "重规划完成"

    affected: List[str] = []
    if new_plan:
        for day in (new_plan.get("days") or []):
            for node in (day.get("route_details") or []):
                if node.get("type") == "spot":
                    sid = str(((node.get("details") or {}).get("spot_id")) or "")
                    if sid:
                        affected.append(sid)

    return ReplanRequest(
        new_timeline=new_timeline,
        reason=reason,
        diff_summary=changes_to_diff_summary(result.get("changes") or []),
        need_replan=True,
        impact=max(0.0, min(1.0, float(impact))),
        affected_spots=affected,
    )