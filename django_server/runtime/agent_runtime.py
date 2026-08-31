"""AB 整合后的单用户运行时。

- 持有 ToolRegistry / ToolProvider / BookingManager / ExecutionAgent
- 在内存中维护 timeline、events（单用户 Demo 不落库）
- decision_hook 来自 runtime/a_interface.py，是 A 侧预留接入点
"""

from __future__ import annotations

import asyncio
import logging
import uuid
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
    TripTimeline,
    to_dict,
)
from execution.execution_agent import ExecutionAgent
from runtime.a_interface import build_decision_hook, build_planner_hook
from tools import MockWorld, ToolProvider, ToolRegistry, build_registry

logger = logging.getLogger("runtime.agent")


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

    def set_timeline_from_payload(self, payload: Dict[str, Any]) -> TripTimeline:
        city = payload.get("city", "")
        start = payload.get("start_date", "")
        end = payload.get("end_date", "")
        days_data = payload.get("days", [])

        start_date = date.fromisoformat(start) if isinstance(start, str) else start
        end_date = date.fromisoformat(end) if isinstance(end, str) else end

        days: List[Any] = []
        for d in days_data:
            d_date = d.get("date", "")
            d_date_val = date.fromisoformat(d_date) if isinstance(d_date, str) else d_date
            items = []
            for it in d.get("items", d.get("activities", [])):
                items.append(Place(
                    id=it.get("id", ""),
                    name=it.get("name", ""),
                    lat=it.get("lat", 0.0),
                    lng=it.get("lng", 0.0),
                    category=it.get("category", "scenic"),
                    arrival=it.get("arrival", "09:00"),
                    end_time=it.get("end_time", ""),
                    open_time=it.get("open_time", "09:00-17:00"),
                    queue_min=it.get("queue_min", 0),
                    ticket_required=it.get("ticket_required", False),
                    price=it.get("price", 0.0),
                ))
            days.append(DayPlan(day=d.get("day", 1), date=d_date_val, items=items))

        timeline = TripTimeline(
            id=payload.get("id", ""),
            city=city,
            start_date=start_date,
            end_date=end_date,
            days=days,
            total_cost=payload.get("total_cost", 0.0),
            walking_distance=payload.get("walking_distance", 0.0),
        )
        self.init_timeline(timeline)
        return timeline

    # -- 执行入口（Django 同步视图内包 asyncio） --------------------------

    def poll(self) -> List[MonitorEvent]:
        return asyncio.run(self.require_agent().poll_once())

    def lookahead(self, now: Optional[datetime] = None) -> List[MonitorEvent]:
        return asyncio.run(self.require_agent().check_lookahead(now or datetime.now()))


runtime = AgentRuntime()
