"""AB 整合后的单用户运行时。

- 持有 ToolRegistry / ToolProvider / BookingManager / ExecutionAgent
- 在内存中维护 timeline、events（单用户 Demo 不落库）
- decision_hook 来自 runtime/a_interface.py，是 A 侧预留接入点
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from booking.booking_manager import BookingManager
from config.settings import settings
from core.schemas import (
    ActionItem,
    ActionStatus,
    DayPlan,
    EventType,
    MonitorEvent,
    PermissionLevel,
    Place,
    ReplanRequest,
    TripTimeline,
    to_dict,
)
from execution.execution_agent import ExecutionAgent
from runtime.a_interface import build_decision_hook, build_planner_hook
from tools import MockWorld, ToolProvider, ToolRegistry, build_registry

logger = logging.getLogger("runtime.agent")


def _timeline_diff(old_tl: Any, new_tl: Any) -> List[str]:
    """对话改时间轴的简化 diff（对齐 replan 的 diff_summary 展示形态）。

    按景点名称对比新旧时间轴的（天, 到达时间）：added / removed / rescheduled。
    """
    def slots(tl: Any) -> Dict[str, tuple]:
        out: Dict[str, tuple] = {}
        for day in tl.days:
            for it in day.items:
                name = str(it.name or "").strip()
                if name and name not in out:
                    out[name] = (day.day, it.arrival)
        return out

    old_slots, new_slots = slots(old_tl), slots(new_tl)
    diffs: List[str] = []
    for name, (day, arrival) in new_slots.items():
        if name not in old_slots:
            diffs.append(f"[added] {name}：第{day}天 {arrival}")
    for name, (day, arrival) in old_slots.items():
        if name not in new_slots:
            diffs.append(f"[removed] {name}（原第{day}天 {arrival}）")
        elif (day, arrival) != new_slots[name]:
            nd, n_arrival = new_slots[name]
            diffs.append(f"[rescheduled] {name}：第{day}天 {arrival} → 第{nd}天 {n_arrival}")
    return diffs or ["[updated] 行程已按对话调整"]


def _replan_to_actions(replan: Any) -> List[ActionItem]:
    """把 A 侧重规划结果（``ReplanRequest``）解析为 Action Queue 条目。

    B 侧拿不到 A 侧 ``replan()`` 的结构化 changes dict（已被包装成
    ``ReplanRequest``），故按可解释的 ``diff_summary`` 条目前缀解析，产出
    形态对齐 A 侧 ``workflow.actions.build_actions``（修复 0827 起接入）：
      - 有新时间轴 → 「更新路线」（low / auto，直接执行）；
      - ``[hotel_changed] 新酒店名（原因）`` → 「预订{新酒店}」
        （medium / confirm，进队列等用户确认）。
    其余条目（removed / added / move / rescheduled）随「更新路线」一并处理，
    门票级预约动作待候选池接入后可细化。
    """
    actions: List[ActionItem] = []
    if replan is None or getattr(replan, "new_timeline", None) is None:
        return actions
    actions.append(ActionItem(
        action_id=f"route-{uuid.uuid4().hex[:8]}",
        title="更新路线",
        description=f"检测到行程变化，已生成新路线：{replan.reason or ''}",
        status=ActionStatus.PENDING,
        permission=PermissionLevel.AUTO,
        target="timeline:update",
        type="ROUTE_UPDATE",
    ))
    for line in getattr(replan, "diff_summary", None) or []:
        s = str(line)
        if s.startswith("[hotel_changed]"):
            rest = s[len("[hotel_changed]"):].strip()
            name = rest.split("（", 1)[0].strip() or "新酒店"
            actions.append(ActionItem(
                action_id=f"hotel-{uuid.uuid4().hex[:8]}",
                title=f"预订{name}",
                description=rest,
                status=ActionStatus.PENDING,
                permission=PermissionLevel.CONFIRM,
                target=f"hotel:{name}",
                type="HOTEL_BOOK",
            ))
    return actions


class AgentRuntime:
    """Django 进程内的单例 AB Runtime（单用户 Demo 用）。"""

    def __init__(self) -> None:
        # 共享 MockWorld：假池数据源，同时是 Live 模式下的突发事件 override 层。
        # /api/debug/inject/ 的 persist_world 直接操作它，让注入状态对后续轮询持续可见。
        self.world: MockWorld = MockWorld()
        self.registry: ToolRegistry = build_registry(self.world)
        self.tool_provider = ToolProvider(self.registry)
        # E5：动作/预约持久化（BOOKING_PERSIST_PATH 开启，默认关闭避免测试/本地污染）
        self.booking_manager = BookingManager(
            self.registry, on_booking_failed=self._on_booking_failed,
            persist_path=settings.booking_persist_path or None,
        )
        self.requirement: Optional[Dict[str, Any]] = None   # A 侧结构化需求（/api/plan/ 提交时存）
        self.timeline: Optional[TripTimeline] = None
        self.agent: Optional[ExecutionAgent] = None
        self.events: List[MonitorEvent] = []
        self.replan_history: List[Dict[str, Any]] = []
        self.timeline_history: List[Dict[str, Any]] = []
        self.tool_call_log: List[Dict[str, Any]] = []
        # 酒店数据缓存：由 A/Planner/B 调度调用 hotel_tool 时自动记录，供 C 只读展示
        self.hotel_search_results: List[Dict[str, Any]] = []
        self.hotel_details: Dict[str, Any] = {}
        self.hotel_tags: Optional[Dict[str, Any]] = None
        self.started_at: str = datetime.now().isoformat(timespec="seconds")
        self._decision_hook: Any = None
        self._last_planner_error: Optional[str] = None
        self._wrap_tool_call_logging()

    # -- C 侧事件回调 -----------------------------------------------------

    def _on_event(self, event: MonitorEvent) -> None:
        """C 的接入点：Demo 中把事件追加到内存列表供 /api/events 轮询。"""
        self.events.append(event)
        logger.info("Event buffered: %s @ %s", event.event_type.value, event.place)

    def _on_booking_failed(self, record: Any) -> None:
        """预订确认失败 → 组装 BOOKING MonitorEvent 交 ExecutionAgent（酒店满房闭环）。

        AB 合码方案 §三.7（闭环最后一环）：confirm 失败置 FAILED + Action 置
        BLOCKED 后，这里补发事件，A 的 BDecisionHook 走硬规则触发换酒店。
        事件 data 按 b_contract._booking_to_hotel_event 约定（hotel_id 必填）；
        hotel_id 优先按名称从 A 侧酒店池映射，保证 replanner 能把原酒店排除掉。
        """
        agent = self.agent
        if agent is None:
            return
        hotel_id, hotel_name = str(record.booking_id), str(record.place)
        try:
            from data_transmission.hotel import load_hotels

            city = self.timeline.city if self.timeline is not None else ""
            place_key = str(record.place).replace("（满房）", "").replace("满房", "").strip()
            match = None
            for h in load_hotels(city):
                if h.name == place_key or str(h.id) == place_key:
                    match = h
                    break
            if match is None:
                match = next(
                    (h for h in load_hotels(city)
                     if place_key.startswith(h.name) or h.name.startswith(place_key)),
                    None,
                )
            if match is not None:
                hotel_id, hotel_name = str(match.id), match.name
        except Exception:  # noqa: BLE001  池映射失败回退 booking_id 作 hotel_id
            pass
        event = MonitorEvent(
            event_id=f"bevt-{record.booking_id}",
            event_type=EventType.BOOKING,
            place=record.place,
            observed_at=datetime.now(),
            rule_name="booking-confirm",
            spot_id="",
            data={
                "hotel_id": hotel_id,
                "hotel_name": hotel_name,
                "hotel_full": True,
            },
        )
        # 交 ExecutionAgent 处理：handle_event 内部会先回调 on_event 缓冲一次
        # （/api/events 轮询可见），再走决策 → 重规划。
        # 修复 0827：这里不再显式 _on_event——否则同一条 BOOKING 事件会被缓冲两次
        # （服务器核对实测：/api/events 出现重复 bevt-*）。
        asyncio.run(agent.handle_event(event))

    # -- A 侧接入点 -------------------------------------------------------

    def _get_decision_hook(self) -> Any:
        if self._decision_hook is None:
            raw_hook = build_decision_hook(tool_provider=self.tool_provider)

            def hook(req: Any) -> Any:
                replan = raw_hook(req)
                self._record_decision(req, replan)
                # 2026-08-31：apply_replan 只更新 agent.timeline（决策链路），
                # 这里同步 runtime.timeline（/api/timeline/ 展示端）——
                # 条件与 execution_agent.apply_replan 一致（new_timeline 非空）。
                # 此前重规划后 C 端拉 /api/timeline/ 仍是旧路线。
                if replan is not None and getattr(replan, "new_timeline", None) is not None:
                    self.timeline = replan.new_timeline
                return replan

            self._decision_hook = hook
        return self._decision_hook

    def _record_decision(self, req: Any, replan: Any) -> None:
        """记录每次决策请求与结果，供 C 展示决策解释。"""
        entry = {
            "id": f"replan-{len(self.replan_history) + 1}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "events": [to_dict(e) for e in req.events],
            "current_timeline": to_dict(req.current_timeline),
            "context": req.context,
            "decision": None if replan is None else to_dict(replan),
        }
        self.replan_history.append(entry)

        if replan is not None and replan.new_timeline is not None:
            self.timeline_history.append({
                "id": f"tl-{len(self.timeline_history) + 1}",
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "reason": replan.reason,
                "timeline": to_dict(replan.new_timeline),
            })
            # 修复 0827：重规划结果回填 Action Queue（更新路线 auto / 换宿预订 confirm），
            # 让 C 端的 Action Queue 展示「决策 → 动作 → 用户确认」完整链路。
            items = _replan_to_actions(replan)
            if items:
                self.booking_manager.enqueue_actions(items)

    def _wrap_tool_call_logging(self) -> None:
        """包装 registry.call，记录所有工具调用供 C 展示。"""
        original_call = self.registry.call

        def logged_call(name: str, **kwargs: Any) -> Any:
            result = original_call(name, **kwargs)
            tool = self.registry.get(name)
            logged_data = result.data if tool.readonly else None
            self.tool_call_log.append({
                "tool": name,
                "arguments": kwargs,
                "status": result.status.value,
                "source": result.source,
                "elapsed_ms": result.elapsed_ms,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "error": result.error,
                "has_data": logged_data is not None,
                "data": logged_data,
            })
            _capture_hotel_data(name, kwargs, result)
            return result

        def _capture_hotel_data(name: str, kwargs: Dict[str, Any], result: Any) -> None:
            """hotel_tool 被 A/Planner/B 调用后，缓存结果供 C 只读展示。"""
            if name != "hotel" or result.status.value != "ok" or result.data is None:
                return
            action = kwargs.get("action", "search")
            data = result.data
            if action == "search":
                self.hotel_search_results.append({
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                    "arguments": kwargs,
                    "data": data,
                })
                # 只保留最近 50 条，避免内存无限增长
                if len(self.hotel_search_results) > 50:
                    self.hotel_search_results = self.hotel_search_results[-50:]
            elif action == "detail":
                hotel_id = data.get("hotelId") if isinstance(data, dict) else None
                if hotel_id is not None:
                    self.hotel_details[str(hotel_id)] = data
            elif action == "tags":
                self.hotel_tags = data

        # 实例属性覆盖方法，确保 registry / ToolProvider / BookingManager 都走这里。
        self.registry.call = logged_call  # type: ignore[method-assign]

    # -- 初始化 -----------------------------------------------------------

    def init_timeline(self, timeline: TripTimeline) -> None:
        self.timeline = timeline
        self.agent = ExecutionAgent(
            timeline=timeline,
            registry=self.registry,
            decision_hook=self._get_decision_hook(),
            on_event=self._on_event,
            booking_manager=self.booking_manager,
            tool_provider=self.tool_provider,
        )
        self.timeline_history.append({
            "id": f"tl-{len(self.timeline_history) + 1}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "reason": "initial",
            "timeline": to_dict(timeline),
        })
        logger.info("Timeline set: city=%s, days=%d", timeline.city, len(timeline.days))

    def init_from_requirement(self, payload: Dict[str, Any]) -> TripTimeline:
        """A 侧需求（POST /api/plan/）→ A 的 Planner → TripTimeline。

        AB 合码方案 §三.4：``build_planner_hook(payload).generate_timeline()``
        → ``init_timeline(timeline)``。新需求视为新会话，清空上一份行程的内存态。
        规划失败时 BPlannerHook 返回空时间轴并记 ``last_error``（本方法不抛异常），
        由视图层检查 ``days`` 是否为空并给出 HTTP 提示。
        """
        requirement = payload if isinstance(payload, dict) else {}
        self.requirement = requirement
        self._decision_hook = None
        self._last_planner_error = None
        # 新需求 = 新会话：清空上一份行程的运行时状态（单用户 Demo 内存态）
        self.events = []
        self.replan_history = []
        self.timeline_history = []
        self.tool_call_log = []
        self.hotel_search_results = []
        self.hotel_details = {}
        self.hotel_tags = None
        self.booking_manager = BookingManager(
            self.registry, on_booking_failed=self._on_booking_failed,
            persist_path=settings.booking_persist_path or None,
            restore=False,   # 新计划 = 新会话：清空旧动作，覆写持久化文件
        )
        planner_hook = build_planner_hook(
            requirement=requirement, tool_provider=self.tool_provider
        )
        timeline = planner_hook.generate_timeline()
        self._last_planner_error = getattr(planner_hook, "last_error", None)
        self.init_timeline(timeline)
        return timeline

    def status(self) -> Dict[str, Any]:
        """汇总当前运行时状态，供 C 首页/调试展示。"""
        return {
            "started_at": self.started_at,
            "timeline_set": self.timeline is not None,
            "timeline": to_dict(self.timeline) if self.timeline is not None else None,
            "events_count": len(self.events),
            "actions_count": len(self.booking_manager.actions()),
            "bookings_count": len(self.booking_manager.records()),
            "replan_history_count": len(self.replan_history),
            "timeline_history_count": len(self.timeline_history),
            "tool_call_count": len(self.tool_call_log),
            "demo_mode": settings.demo_mode,
            "use_real_api": settings.use_real_api,
            "use_real_map_api": settings.use_real_map_api,
        }

    def agent_info(self) -> Optional[Dict[str, Any]]:
        """暴露 ExecutionAgent 内部可观测数据（调试/可视化用）。"""
        agent = self.agent
        if agent is None:
            return None
        return {
            "impact_threshold": agent.impact_threshold,
            "periodic_rules": [
                {
                    "name": rule.name,
                    "event_type": rule.event_type.value,
                    "interval_s": rule.interval_s,
                    "place": rule.place,
                    "spot_id": rule.spot_id,
                    "enabled": rule.enabled,
                }
                for rule in agent.periodic_rules
            ],
            "lookahead_rules": [
                {
                    "name": rule.name,
                    "event_type": rule.event_type.value,
                    "place": rule.place,
                    "spot_id": rule.spot_id,
                    "lookahead_min": rule.lookahead_min,
                    "fire_at": rule.fire_at.isoformat() if rule.fire_at else None,
                    "fired": rule.fired,
                    "enabled": rule.enabled,
                }
                for rule in agent.lookahead_rules
            ],
            "booked_places": sorted(agent._booked_places),
            "place_info": {
                name: to_dict(place) for name, place in agent._place_info.items()
            },
        }

    def require_agent(self) -> ExecutionAgent:
        if self.agent is None:
            raise RuntimeError("No timeline set. POST /api/timeline/ first.")
        return self.agent

    # -- 时间轴解析（与旧 FastAPI app/service.py 保持兼容） ---------------

    def parse_timeline_payload(self, payload: Dict[str, Any]) -> TripTimeline:
        """payload → TripTimeline 对象（只解析不应用；校验失败抛异常）。

        2026-09-01：从 ``set_timeline_from_payload`` 抽取，供 chat v2
        （update_timeline 工具）复用同一套解析/校验逻辑。
        """
        city = payload.get("city", "")
        start = payload.get("start_date", "")
        end = payload.get("end_date", "")
        days_data = payload.get("days", [])
        if not city or not start or not end or not isinstance(days_data, list):
            raise ValueError("city/start_date/end_date/days 均为必填")

        start_date = date.fromisoformat(start) if isinstance(start, str) else start
        end_date = date.fromisoformat(end) if isinstance(end, str) else end

        days: List[Any] = []
        for d in days_data:
            if not isinstance(d, dict) or not d.get("items"):
                raise ValueError(f"第 {len(days) + 1} 天缺少 items")
            d_date = d.get("date", "")
            d_date_val = date.fromisoformat(d_date) if isinstance(d_date, str) else d_date
            items = []
            for it in d.get("items", d.get("activities", [])):
                name = str(it.get("name", "")).strip()
                arrival = str(it.get("arrival", "")).strip()
                if not name or not arrival:
                    raise ValueError(f"第 {len(days) + 1} 天存在缺 name/arrival 的项目")
                items.append(Place(
                    id=it.get("id", ""),
                    name=name,
                    lat=it.get("lat", 0.0),
                    lng=it.get("lng", 0.0),
                    category=it.get("category", "scenic"),
                    arrival=arrival,
                    end_time=it.get("end_time", ""),
                    open_time=it.get("open_time", "09:00-17:00"),
                    queue_min=it.get("queue_min", 0),
                    ticket_required=it.get("ticket_required", False),
                    price=it.get("price", 0.0),
                ))
            days.append(DayPlan(day=d.get("day", len(days) + 1), date=d_date_val, items=items))

        return TripTimeline(
            id=payload.get("id", ""),
            city=city,
            start_date=start_date,
            end_date=end_date,
            days=days,
            total_cost=payload.get("total_cost", 0.0),
            walking_distance=payload.get("walking_distance", 0.0),
        )

    def set_timeline_from_payload(self, payload: Dict[str, Any]) -> TripTimeline:
        timeline = self.parse_timeline_payload(payload)
        self.init_timeline(timeline)
        return timeline

    def apply_timeline_from_chat(
        self, timeline: TripTimeline, reason: str = ""
    ) -> Dict[str, Any]:
        """对话（chat v2）直接修改时间轴：替换 + 重建监控规则 + 记录。

        与 A 侧 replan 的 ``apply_replan`` 同语义（保留已预约状态），并把改动
        记入 ``replan_history`` / ``timeline_history``（source=chat）——
        C 端轮询因 replan 数量变化自动刷新时间轴，前端零改动。
        返回 ``{"diff_summary": [...], "entry_id": ...}``。
        """
        old_timeline = self.timeline
        self.timeline = timeline
        reason = reason or "对话调整"
        if self.agent is not None:
            # 重建监控规则 + 保留已预约状态（apply_replan 内部逻辑）
            self.agent.apply_replan(
                ReplanRequest(new_timeline=timeline, reason=reason)
            )
        else:
            self.init_timeline(timeline)
        # 2026-09-01：对话改行程后同样补充真源公交导航（失败静默）
        self.enrich_transport_details(timeline)
        diff = _timeline_diff(old_timeline, timeline) if old_timeline is not None else []
        entry = {
            "id": f"replan-{len(self.replan_history) + 1}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "source": "chat",
            "events": [],
            "current_timeline": to_dict(old_timeline) if old_timeline is not None else None,
            "context": {"impact_threshold": 0, "origin": "chat"},
            "decision": {
                "need_replan": True,
                "impact": 0.0,
                "reason": reason,
                "diff_summary": diff,
                "new_timeline": to_dict(timeline),
            },
        }
        self.replan_history.append(entry)
        self.timeline_history.append({
            "id": f"tl-{len(self.timeline_history) + 1}",
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "reason": reason,
            "timeline": to_dict(timeline),
        })
        logger.info("chat 调整时间轴: %s | diff=%s", reason, diff)
        return {"diff_summary": diff, "entry_id": entry["id"]}

    # -- 执行入口（Django 同步视图内包 asyncio） --------------------------

    def enrich_transport_details(self, timeline: Any, max_workers: int = 3) -> None:
        """对时间轴 transport 段并发查询真源公交导航（map.route transit）。

        2026-09-01（C 端反馈：交通段只有时长、前端兜底渲染）：
        成功后把 ``{mode, distance_km, duration_min, fare, transit,
        transit_text, walking_m, source:"live"}`` 合并进 ``Place.details``
        供 C 端展示公共交通具体信息与路程；失败/无真源静默降级
        （保留矩阵时长与透传的 from/to/distance_km）。
        城际段（details.kind 有值，outbound/return）跳过 enrich（T5，
        2026-09-05）：其 from/to 是城市级地名，transit 查询产生怪路线覆盖
        展示，且真源班次数据已足够展示。

        并发 max_workers=3 对齐高德免费 key QPS（~3/s）：每线程同一时刻
        最多 1 个在途请求（geocode → route 顺序依赖），并发意义是重叠
        网络等待；瞬时超限由 amap_client 10021 退避重试兜底。
        """
        if not getattr(settings, "use_real_map_api", False):
            return
        segments = [
            item for day in timeline.days for item in day.items
            if getattr(item, "category", "") == "transport"
        ]
        if not segments:
            return
        city = getattr(timeline, "city", "") or ""

        def _split_name(name: str):
            for sep in ("→", "->", "-"):
                if sep in name:
                    parts = [p.strip() for p in name.split(sep, 1)]
                    if parts[0] and parts[1]:
                        return parts[0], parts[1]
            return "", ""

        def _enrich(item: Any) -> None:
            try:
                is_intercity = bool((item.details or {}).get("kind"))
                if is_intercity:
                    # T5（2026-09-05）：城际段（outbound/return）的 from/to 是
                    # 城市级地名（「天津」/「北京」），按地名查 amap transit 会
                    # 返回跨市/被夹转的怪路线并覆盖展示（实测：天津地铁+机场
                    # 巴士 184.81km/362min、锦州→张掖联运段被覆盖 2657km/
                    # 996min）——城际段已带真源班次（service_no/发到时刻/票价），
                    # transit 路线文本无意义，跳过 enrich；距离展示由 C 端
                    # haversine 兜底。十一节的 same_city 门控与 >80km sanity
                    # 仍护市内段。
                    return
                origin = str((item.details or {}).get("from") or "").strip()
                destination = str((item.details or {}).get("to") or "").strip()
                if not origin or not destination:
                    origin, destination = _split_name(item.name)
                if not origin or not destination:
                    return
                result = self.registry.call(
                    "map", action="route",
                    origin=origin, destination=destination,
                    city=city, mode="transit",
                    same_city=True,  # 市内段：全国兜底城市校验（十一节）
                )
                if result.status.value != "ok" or not isinstance(result.data, dict):
                    return
                # 地理保真 sanity（十一节）：市内段 enrich 距离超 80km（POI 漂移
                # 跨市残留）→ 放弃合并保留矩阵值；城际段（kind 有值）跨城属正常
                dist = result.data.get("distance_km")
                if (
                    not is_intercity
                    and isinstance(dist, (int, float))
                    and dist > 80
                ):
                    return
                merged = dict(item.details or {})
                for key in ("mode", "distance_km", "duration_min", "fare",
                            "transit", "transit_text", "walking_m", "source"):
                    if result.data.get(key) is not None:
                        merged[key] = result.data[key]
                item.details = merged
            except Exception:  # noqa: BLE001  单段失败静默降级，不阻断
                pass

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            list(pool.map(_enrich, segments))

    def poll(self) -> List[MonitorEvent]:
        return asyncio.run(self.require_agent().poll_once())

    def lookahead(self, now: Optional[datetime] = None) -> List[MonitorEvent]:
        return asyncio.run(self.require_agent().check_lookahead(now or datetime.now()))


runtime = AgentRuntime()
