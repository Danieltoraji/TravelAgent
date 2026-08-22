"""AB 整合后的单用户运行时。

- 持有 ToolRegistry / ToolProvider / BookingManager / ExecutionAgent
- 在内存中维护 timeline、events（单用户 Demo 不落库）
- decision_hook 来自 runtime/a_interface.py，是 A 侧预留接入点
"""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime
from typing import Any, Dict, List, Optional

from booking.booking_manager import BookingManager
from config.settings import settings
from core.schemas import DayPlan, EventType, MonitorEvent, Place, TripTimeline, to_dict
from execution.execution_agent import ExecutionAgent
from runtime.a_interface import build_decision_hook, build_planner_hook
from tools import ToolProvider, ToolRegistry, build_registry

logger = logging.getLogger("runtime.agent")


class AgentRuntime:
    """Django 进程内的单例 AB Runtime（单用户 Demo 用）。"""

    def __init__(self) -> None:
        self.registry: ToolRegistry = build_registry()
        self.tool_provider = ToolProvider(self.registry)
        self.booking_manager = BookingManager(
            self.registry, on_booking_failed=self._on_booking_failed
        )
        self.requirement: Optional[Dict[str, Any]] = None   # A 侧结构化需求（/api/plan/ 提交时存）
        self.timeline: Optional[TripTimeline] = None
        self.agent: Optional[ExecutionAgent] = None
        self.events: List[MonitorEvent] = []
        self.replan_history: List[Dict[str, Any]] = []
        self.timeline_history: List[Dict[str, Any]] = []
        self.tool_call_log: List[Dict[str, Any]] = []
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
        # 先缓冲（/api/events 轮询可见），再交 ExecutionAgent 决策 → 重规划
        self._on_event(event)
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

    def _wrap_tool_call_logging(self) -> None:
        """包装 registry.call，记录所有工具调用供 C 展示。"""
        original_call = self.registry.call

        def logged_call(name: str, **kwargs: Any) -> Any:
            result = original_call(name, **kwargs)
            self.tool_call_log.append({
                "tool": name,
                "arguments": kwargs,
                "status": result.status.value,
                "source": result.source,
                "elapsed_ms": result.elapsed_ms,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "error": result.error,
                "has_data": result.data is not None,
            })
            return result

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
        self.booking_manager = BookingManager(
            self.registry, on_booking_failed=self._on_booking_failed
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
