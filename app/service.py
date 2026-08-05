"""轻量服务层（可选）：供 C 的 Web 前端调用。

需要安装 fastapi + uvicorn（见 requirements.txt）：
    pip install -r requirements.txt
    uvicorn app.service:app --reload --port 8000

核心代码零依赖；未安装 fastapi 时本模块的 app 为 None，不影响其他功能。

端点一览：
  基础:
    GET  /health                         健康检查
    GET  /tools                          列出所有已注册工具
    POST /tools/{name}/invoke            调用指定工具

  行程时间轴:
    GET  /timeline                       获取当前行程时间轴
    POST /timeline                       设置/替换行程时间轴

  预约管理:
    POST /booking/prepare                准备预约（自动填充景点信息）
    POST /booking/{booking_id}/confirm   用户确认 → 提交预约
    POST /booking/{booking_id}/cancel     取消预约
    POST /booking/{booking_id}/payment    生成付款提醒（人工执行）
    GET  /booking                        列出所有预约记录
    GET  /booking/{booking_id}           查询单条预约记录

  Action Queue:
    GET  /actions                        列出所有 ActionItem
    POST /actions/{action_id}/approve    标记为已确认
    POST /actions/{action_id}/reject     标记为已拒绝

  监控事件:
    GET  /events                         获取事件历史（可选 ?since=index 增量查询）
    POST /execution/poll                 手动触发一次轮询（Demo/调试）
    POST /execution/lookahead            手动触发到达前检查（Demo/调试）

  导出:
    GET  /export/ics                     导出 .ics 日历文件
    GET  /export/markdown                导出 Markdown 行程单
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from booking.booking_manager import BookingManager, BookingRecord
from core.schemas import (
    ActionItem,
    ActionStatus,
    MonitorEvent,
    TripTimeline,
    to_dict,
)
from execution.execution_agent import ExecutionAgent
from itinerary.ics_exporter import build_ics
from itinerary.markdown_exporter import render_markdown
from tools import ToolRegistry, build_registry, default_registry

logger = logging.getLogger("app.service")

try:  # 可选依赖
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import PlainTextResponse
    _HAS_FASTAPI = True
except ImportError:  # pragma: no cover
    _HAS_FASTAPI = False
    FastAPI = None  # type: ignore[assignment]
    HTTPException = None  # type: ignore[assignment]
    Query = None  # type: ignore[assignment]
    CORSMiddleware = None  # type: ignore[assignment]
    PlainTextResponse = None  # type: ignore[assignment]


# ──────────────────────────────────────────────────────────────────────
# AppState — 服务层共享状态单例
# ──────────────────────────────────────────────────────────────────────


class AppState:
    """服务层共享状态：registry / booking_manager / execution_agent / timeline / events。

    C 的前端通过 HTTP 端点间接操作这些状态。模块级 ``state`` 单例保证全应用共享。
    """

    def __init__(self) -> None:
        self.registry: ToolRegistry = default_registry
        self.booking_manager: BookingManager = BookingManager(self.registry)
        self.execution_agent: Optional[ExecutionAgent] = None
        self.timeline: Optional[TripTimeline] = None
        self.events: List[MonitorEvent] = []

    def init_timeline(self, timeline: TripTimeline) -> None:
        """设置行程时间轴并初始化 ExecutionAgent。

        将 booking_manager 注入 ExecutionAgent，使到达前检查触发时自动准备预约。
        """
        self.timeline = timeline
        self.execution_agent = ExecutionAgent(
            timeline=timeline,
            registry=self.registry,
            on_event=self._on_event,
            booking_manager=self.booking_manager,
        )
        logger.info("Timeline set: city=%s, days=%d", timeline.city, len(timeline.days))

    def _on_event(self, event: MonitorEvent) -> None:
        """ExecutionAgent 的 on_event 回调：将事件存入缓冲供 C 轮询。"""
        self.events.append(event)
        logger.info("Event buffered: %s @ %s", event.event_type.value, event.place)

    def require_timeline(self) -> TripTimeline:
        """获取当前时间轴，未设置时抛 400。"""
        if self.timeline is None:
            raise HTTPException(status_code=400, detail="No timeline set. POST /timeline first.")
        return self.timeline

    def require_execution_agent(self) -> ExecutionAgent:
        """获取 ExecutionAgent，未初始化时抛 400。"""
        if self.execution_agent is None:
            raise HTTPException(status_code=400, detail="ExecutionAgent not initialized. POST /timeline first.")
        return self.execution_agent


state = AppState()


# ──────────────────────────────────────────────────────────────────────
# FastAPI 应用构建
# ──────────────────────────────────────────────────────────────────────


def create_app() -> Any:
    """构建 FastAPI 应用。未安装 fastapi 时抛出 RuntimeError。"""
    if not _HAS_FASTAPI:
        raise RuntimeError("fastapi not installed. Run: pip install -r requirements.txt")

    app = FastAPI(
        title="TravelAgent Service",
        version="0.2.0",
        description="B 侧服务层：为 C 的 Web 前端提供行程/预约/事件/导出 API",
    )

    # CORS：允许 C 的前端跨域调用（开发环境允许所有源）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── 基础 ────────────────────────────────────────────────────────

    @app.get("/health")
    def health() -> Dict[str, str]:
        return {"status": "ok", "project": "TravelAgent"}

    @app.get("/tools")
    def list_tools() -> Dict[str, Any]:
        return {"tools": state.registry.names()}

    @app.post("/tools/{name}/invoke")
    def invoke_tool(name: str, payload: Dict[str, Any] = {}) -> Dict[str, Any]:
        try:
            return state.registry.call(name, **payload).to_dict()
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── 行程时间轴 ──────────────────────────────────────────────────

    @app.get("/timeline")
    def get_timeline() -> Dict[str, Any]:
        tl = state.require_timeline()
        return to_dict(tl)

    @app.post("/timeline")
    def set_timeline(payload: Dict[str, Any]) -> Dict[str, Any]:
        """设置/替换行程时间轴。

        请求体示例:
        {
            "city": "北京",
            "start_date": "2026-08-01",
            "end_date": "2026-08-02",
            "days": [
                {
                    "day": 1,
                    "date": "2026-08-01",
                    "items": [
                        {"name": "故宫", "category": "scenic", "arrival": "09:00",
                         "ticket_required": true, "price": 60.0}
                    ]
                }
            ]
        }
        """
        try:
            from datetime import date as date_cls
            from core.schemas import DayPlan, Place

            city = payload.get("city", "")
            start = payload.get("start_date", "")
            end = payload.get("end_date", "")
            days_data = payload.get("days", [])

            # 解析日期
            start_date = date_cls.fromisoformat(start) if isinstance(start, str) else start
            end_date = date_cls.fromisoformat(end) if isinstance(end, str) else end

            days: List[Any] = []
            for d in days_data:
                d_date = d.get("date", "")
                d_date_val = date_cls.fromisoformat(d_date) if isinstance(d_date, str) else d_date
                items = []
                for it in d.get("items", []):
                    items.append(Place(
                        name=it.get("name", ""),
                        lat=it.get("lat", 0.0),
                        lng=it.get("lng", 0.0),
                        category=it.get("category", "scenic"),
                        arrival=it.get("arrival", "09:00"),
                        open_time=it.get("open_time", "09:00-17:00"),
                        queue_min=it.get("queue_min", 0),
                        ticket_required=it.get("ticket_required", False),
                        price=it.get("price", 0.0),
                    ))
                days.append(DayPlan(day=d.get("day", 1), date=d_date_val, items=items))

            timeline = TripTimeline(
                city=city,
                start_date=start_date,
                end_date=end_date,
                days=days,
            )
            state.init_timeline(timeline)
            return {"status": "ok", "message": "Timeline set", "timeline": to_dict(timeline)}
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"Invalid timeline: {exc}") from exc

    # ── 预约管理 ────────────────────────────────────────────────────

    @app.post("/booking/prepare")
    def booking_prepare(payload: Dict[str, Any] = {}) -> Dict[str, Any]:
        """准备预约。参数: place, target_date, party_size, booking_type。"""
        place = payload.get("place", "")
        target_date = payload.get("target_date", "")
        party_size = int(payload.get("party_size", 1))
        booking_type = payload.get("booking_type", "scenic")
        try:
            rec = state.booking_manager.prepare(
                place=place, target_date=target_date,
                party_size=party_size, booking_type=booking_type,
            )
            return to_dict(rec)
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/booking/{booking_id}/confirm")
    def booking_confirm(booking_id: str) -> Dict[str, Any]:
        try:
            rec = state.booking_manager.confirm(booking_id)
            return to_dict(rec)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.post("/booking/{booking_id}/cancel")
    def booking_cancel(booking_id: str) -> Dict[str, Any]:
        try:
            rec = state.booking_manager.cancel(booking_id)
            return to_dict(rec)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/booking/{booking_id}/payment")
    def booking_payment(booking_id: str) -> Dict[str, Any]:
        try:
            item = state.booking_manager.payment_action(booking_id)
            return to_dict(item)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/booking")
    def list_bookings() -> Dict[str, Any]:
        records = state.booking_manager.records()
        return {"bookings": [to_dict(r) for r in records], "count": len(records)}

    @app.get("/booking/{booking_id}")
    def get_booking(booking_id: str) -> Dict[str, Any]:
        try:
            rec = state.booking_manager.get(booking_id)
            return to_dict(rec)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    # ── Action Queue ─────────────────────────────────────────────────

    @app.get("/actions")
    def list_actions() -> Dict[str, Any]:
        actions = state.booking_manager.actions()
        return {"actions": [to_dict(a) for a in actions], "count": len(actions)}

    @app.post("/actions/{action_id}/approve")
    def approve_action(action_id: str) -> Dict[str, Any]:
        """C 用户确认 ActionItem → 标记为 APPROVED。"""
        for a in state.booking_manager.actions():
            if a.action_id == action_id:
                a.status = ActionStatus.APPROVED
                return to_dict(a)
        raise HTTPException(status_code=404, detail=f"Action not found: {action_id}")

    @app.post("/actions/{action_id}/reject")
    def reject_action(action_id: str) -> Dict[str, Any]:
        """C 用户拒绝 ActionItem → 标记为 REJECTED。"""
        for a in state.booking_manager.actions():
            if a.action_id == action_id:
                a.status = ActionStatus.REJECTED
                return to_dict(a)
        raise HTTPException(status_code=404, detail=f"Action not found: {action_id}")

    # ── 监控事件 ────────────────────────────────────────────────────

    @app.get("/events")
    def list_events(since: int = Query(default=0, ge=0)) -> Dict[str, Any]:
        """获取事件历史。可选 ?since=index 增量查询。"""
        events = state.events[since:]
        return {
            "events": [to_dict(e) for e in events],
            "count": len(events),
            "total": len(state.events),
        }

    @app.post("/execution/poll")
    def execution_poll() -> Dict[str, Any]:
        """手动触发一次轮询（Demo/调试用）。"""
        agent = state.require_execution_agent()
        events = agent.poll_once()
        return {
            "status": "ok",
            "events": [to_dict(e) for e in events],
            "count": len(events),
        }

    @app.post("/execution/lookahead")
    def execution_lookahead(payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """手动触发到达前检查（Demo/调试用）。

        可选 body: {"now": "2026-08-01T08:45:00"} 指定模拟时间；
        不传则用当前时间。
        """
        agent = state.require_execution_agent()
        if payload and "now" in payload:
            now = datetime.fromisoformat(payload["now"])
        else:
            now = datetime.now()
        events = agent.check_lookahead(now)
        return {
            "status": "ok",
            "events": [to_dict(e) for e in events],
            "count": len(events),
        }

    # ── 导出 ────────────────────────────────────────────────────────

    @app.get("/export/ics", response_class=PlainTextResponse)
    def export_ics() -> str:
        tl = state.require_timeline()
        return build_ics(tl)

    @app.get("/export/markdown", response_class=PlainTextResponse)
    def export_markdown() -> str:
        tl = state.require_timeline()
        return render_markdown(tl)

    return app


app = create_app() if _HAS_FASTAPI else None
