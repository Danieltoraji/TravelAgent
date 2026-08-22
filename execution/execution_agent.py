"""Execution Agent：持续监控（项目核心，对应《任务整理.md》第四节）。

职责：
  - 加载行程时间轴（TripTimeline，来自 Route Planner 的输出契约）；
  - 构建监控规则：周期性（天气30min / 交通5min）+ 到达前触发（景点前20min / 餐厅前30min）；
  - 每次观测产出 MonitorEvent，评估是否达到影响阈值；
  - 达到阈值时组装 DecisionRequest，交给 A 的 Decision Engine（通过 decision_hook 注入）。

本模块只依赖契约（core/schemas），不依赖 A 的实现 —— 保证 A/B 可并行开发。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Set

from booking.booking_manager import BookingManager
from config.settings import settings
from core.schemas import (
    DecisionRequest,
    EventType,
    MonitorEvent,
    Place,
    ReplanRequest,
    TripTimeline,
)
from monitor.monitor_scheduler import MonitorRule, MonitorScheduler
from tools import ToolRegistry, default_registry
from tools.tool_provider import ToolProvider

logger = logging.getLogger("execution")


@dataclass
class ExecutionAgent:
    """持续监控执行体。

    timeline          : 行程时间轴（Route Planner 产出，A/B 之间由 A 提供）
    decision_hook     : 注入 A 的 Decision Engine（可空，空则仅日志）
    on_event          : 注入 C 的日志/前端推送（可空）
    impact_threshold  : 影响评分阈值，达到即触发决策请求（默认 50）
    booking_manager   : 注入 BookingManager（可空，空则不自动预约）
    default_party_size: 自动预约时的默认人数
    """

    timeline: TripTimeline
    registry: ToolRegistry = default_registry
    decision_hook: Optional[Callable[[DecisionRequest], Any]] = None
    on_event: Optional[Callable[[MonitorEvent], Any]] = None
    impact_threshold: float = 50.0
    booking_manager: Optional[BookingManager] = None
    default_party_size: int = 1
    tool_provider: Optional[ToolProvider] = None
    # 可注入的“当前时间”函数：生产用 datetime.now，Demo/测试可注入模拟时钟
    now_fn: Callable[[], datetime] = datetime.now

    def __post_init__(self) -> None:
        self.scheduler = MonitorScheduler()
        self.periodic_rules: List[MonitorRule] = []
        self.lookahead_rules: List[MonitorRule] = []
        # 自动预约状态跟踪
        self._booked_places: Set[str] = set()
        self._place_info: Dict[str, Place] = {}
        # 默认给 A 侧 LLM 暴露一个只读工具门面
        if self.tool_provider is None:
            self.tool_provider = ToolProvider(self.registry)
        self._build_rules()

    # -- 规则构建 ----------------------------------------------------------
    def _tool_for(self, event_type: EventType) -> str:
        return {
            EventType.WEATHER: "weather",
            EventType.TRAFFIC: "traffic",
            EventType.SCENIC: "scenic",
            EventType.FOOD: "food",
        }[event_type]

    def _poll(self, event_type: EventType, **kwargs: Any) -> Any:
        result = self.registry.call(self._tool_for(event_type), **kwargs)
        if result.status.value == "ok":
            return result.data
        return {"error": result.error, "status": result.status.value}

    def _build_rules(self) -> None:
        cfg = settings.polling
        # 周期性规则：天气、交通（重新赋值，自动清空旧规则）
        self.periodic_rules = [
            MonitorRule(
                name="weather-poll",
                event_type=EventType.WEATHER,
                interval_s=cfg.weather_interval_s,
                place=self.timeline.city,
                call=lambda: self._poll(EventType.WEATHER, city=self.timeline.city),
            ),
            MonitorRule(
                name="traffic-poll",
                event_type=EventType.TRAFFIC,
                interval_s=cfg.traffic_interval_s,
                place=self.timeline.city,
                # 修复：原 origin=destination=city（北京→北京）无意义；
                # 改为查“当日首个景点”的交通状态，接真实 API 时才有意义。
                call=lambda: self._poll(
                    EventType.TRAFFIC,
                    origin=self.timeline.city,
                    destination=self._first_scenic_name(),
                ),
            ),
        ]
        # 到达前触发规则：景点（前20min）、餐饮（前30min）
        # 注意：必须先清空，否则 apply_replan 重建时会累积旧规则
        self.lookahead_rules = []
        # 重建地点信息映射；保留仍在新 timeline 中的已预约记录（防重复预约）
        new_place_names: Set[str] = set()
        for day in self.timeline.days:
            for item in day.items:
                new_place_names.add(item.name)
                self._place_info[item.name] = item
                if item.category == "scenic":
                    self.lookahead_rules.append(MonitorRule(
                        name=f"scenic-{item.name}",
                        event_type=EventType.SCENIC,
                        interval_s=cfg.scenic_interval_s,
                        place=item.name,
                        spot_id=item.id,
                        lookahead_min=settings.scenic_lookahead_min,
                        fire_at=self._fire_at(day.date, item.arrival, settings.scenic_lookahead_min),
                        call=lambda n=item.name: self._poll(EventType.SCENIC, place=n),
                    ))
                elif item.category == "food":
                    self.lookahead_rules.append(MonitorRule(
                        name=f"food-{item.name}",
                        event_type=EventType.FOOD,
                        interval_s=cfg.food_interval_s,
                        place=item.name,
                        spot_id=item.id,
                        lookahead_min=settings.food_lookahead_min,
                        fire_at=self._fire_at(day.date, item.arrival, settings.food_lookahead_min),
                        call=lambda n=item.name: self._poll(EventType.FOOD, near=n),
                    ))
        # 清除已不在新 timeline 中的已预约记录（重规划可能移除某些地点）
        self._booked_places &= new_place_names

    def _first_scenic_name(self) -> str:
        """返回行程中第一个景点的名称（供交通查询做目的地）；无景点时回退到城市名。"""
        for day in self.timeline.days:
            for item in day.items:
                if item.category == "scenic":
                    return item.name
        return self.timeline.city

    @staticmethod
    def _fire_at(day_date: Any, arrival: str, lookahead_min: int) -> datetime:
        hh, mm = (arrival.split(":") + ["00"])[:2]
        arrival_dt = datetime.combine(day_date, datetime.strptime(f"{hh}:{mm}", "%H:%M").time())
        return arrival_dt - timedelta(minutes=lookahead_min)

    # -- 事件处理 ----------------------------------------------------------
    async def handle_event(self, event: MonitorEvent) -> Optional[DecisionRequest]:
        """处理一次观测：回调 on_event；达到阈值则组装并发送 DecisionRequest。

        若 decision_hook 返回 ReplanRequest，则立即应用重规划（更新时间轴 + 重建规则），
        闭合"监控 → 决策 → 重规划 → 新监控"回环。

        支持同步和异步的 on_event / decision_hook（自动检测返回值是否为协程）。
        """
        if self.on_event is not None:
            try:
                result = self.on_event(event)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:  # noqa: BLE001
                logger.exception("on_event handler failed")
        if not self._significant(event):
            return None
        req = DecisionRequest(
            events=[event],
            current_timeline=self.timeline,
            context={
                "impact_threshold": self.impact_threshold,
                "tool_specs": self.tool_provider.list_tools_json(),
            },
        )
        if self.decision_hook is not None:
            try:
                replan = self.decision_hook(req)
                if asyncio.iscoroutine(replan):
                    replan = await replan
                if isinstance(replan, ReplanRequest):
                    self.apply_replan(replan)
            except Exception:  # noqa: BLE001
                logger.exception("decision_hook failed")
        return req

    def apply_replan(self, replan: ReplanRequest) -> bool:
        """应用 A 返回的重规划结果：替换时间轴并重建监控规则。

        返回 True 表示已应用（有新时间轴），False 表示无新方案（仅记录原因）。
        """
        if replan.new_timeline is None:
            logger.info("ReplanRequest 无新时间轴，原因: %s", replan.reason)
            return False
        logger.info("应用重规划: %s | diff=%s", replan.reason, replan.diff_summary)
        self.timeline = replan.new_timeline
        # 重建规则：周期规则重新注册到调度器；lookahead 规则按新时间重算 fire_at
        self.scheduler = MonitorScheduler()
        self._build_rules()
        return True

    def _significant(self, event: MonitorEvent) -> bool:
        """影响判定（可解释决策的第一步）：B 侧先做客观判定，详细评分由 A 完成。

        - 天气：降雨概率 >= 60%
        - 景点：排队 >= impact_threshold
        - 交通：延误 >= 30 分钟
        - 预订：含 hotel_id 且满房（hotel_full）或价格变动（price_delta）→ 放行
          （AB 合码方案 §三.6，与上面分支同一风格）
        """
        data = event.data or {}
        if event.event_type == EventType.WEATHER:
            return int(data.get("rain_probability", 0)) >= 60
        if event.event_type == EventType.SCENIC:
            return int(data.get("queue_min", 0)) >= int(self.impact_threshold)
        if event.event_type == EventType.TRAFFIC:
            return int(data.get("delay_min", 0)) >= 30
        if event.event_type == EventType.BOOKING:
            return bool(data.get("hotel_id")) and bool(
                data.get("hotel_full") or data.get("price_delta") is not None
            )
        return False

    # -- 驱动入口（Demo / 测试用异步驱动） ---------------------------------
    async def poll_once(self) -> List[MonitorEvent]:
        """异步轮询所有周期性规则一次，返回本次观测事件。"""
        events: List[MonitorEvent] = []
        for rule in self.periodic_rules:
            data = rule.call()
            event = self.scheduler.emit(rule, data)
            events.append(event)
            await self.handle_event(event)
        return events

    async def check_lookahead(self, now: datetime) -> List[MonitorEvent]:
        """触发已到 fire_at 的到达前规则（一次性）。

        生产环境由调度器周期性调用本方法；Demo/测试中直接调用。
        触发后自动为需要预约的景点/餐厅准备预约（若 booking_manager 已注入）。
        """
        events: List[MonitorEvent] = []
        for rule in self.lookahead_rules:
            if rule.fire_at is not None and rule.fire_at <= now and not rule.fired:
                rule.fired = True
                data = rule.call()
                event = self.scheduler.emit(rule, data)
                events.append(event)
                await self.handle_event(event)
                await self._maybe_auto_book(rule, event)
        return events

    # -- 自动预约 ----------------------------------------------------------
    async def _maybe_auto_book(self, rule: MonitorRule, event: MonitorEvent) -> None:
        """到达前触发后自动准备预约（若 booking_manager 已注入）。

        - 景点：仅当 ticket_required=True 时预约
        - 餐厅：一律自动预约
        - 同一地点不重复预约
        - 预约产出 PENDING 状态的 ActionItem，需用户通过 C 端确认
        """
        if self.booking_manager is None:
            return
        if rule.place in self._booked_places:
            return
        place_info = self._place_info.get(rule.place)
        if place_info is None:
            return
        # 根据事件类型决定是否预约及预约类型
        if rule.event_type == EventType.SCENIC:
            if not place_info.ticket_required:
                return
            booking_type = "scenic"
        elif rule.event_type == EventType.FOOD:
            booking_type = "food"
        else:
            return
        # 查找目标日期：遍历 timeline 找到包含该地点的 DayPlan.date
        target_date = ""
        for day in self.timeline.days:
            for item in day.items:
                if item.name == rule.place:
                    target_date = day.date.isoformat()
                    break
            if target_date:
                break
        if not target_date:
            return
        try:
            record = self.booking_manager.prepare(
                place=rule.place,
                target_date=target_date,
                party_size=self.default_party_size,
                booking_type=booking_type,
            )
            self._booked_places.add(rule.place)
            # 将 booking_id 附加到事件数据，供下游感知
            if isinstance(event.data, dict):
                event.data["auto_booking_id"] = record.booking_id
            logger.info("自动预约: %s (type=%s, id=%s)", rule.place, booking_type, record.booking_id)
        except Exception:  # noqa: BLE001
            logger.exception("自动预约失败: %s", rule.place)

    async def run_forever(self, on_event: Optional[Callable[[MonitorEvent], Any]] = None) -> None:
        """生产入口：启动异步调度器持续运行（阻塞）。

        修复点：
          - 补 asyncio.sleep（原代码缺 import asyncio 会 NameError）；
          - 用 get_running_loop 而非 get_event_loop（3.12+ 兼容）；
          - check_lookahead 用 now_fn 取当前时间，Demo 可注入模拟时钟与 MockWorld 同步。
        """
        handler = on_event or (lambda ev: None)
        for rule in self.periodic_rules:
            self.scheduler.register(rule)
        self.scheduler.start(handler)  # 内部用 get_running_loop
        try:
            while True:
                await self.check_lookahead(self.now_fn())
                await asyncio.sleep(1)
        finally:
            await self.scheduler.stop()
