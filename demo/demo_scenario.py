"""Demo 剧情脚本：跑通"持续监控 → 事件 → 决策请求"闭环。

剧情（对应《任务整理.md》第十节比赛展示流程）：
  1. 用户提出需求（Planner 由 A 负责，本 Demo 直接构造初始行程时间轴）；
  2. 执行阶段：天气/交通轮询 + 到达前 20 分钟查看景点；
  3. 突发剧情：天气转暴雨、故宫排队 20 -> 120 分钟；
  4. Execution Agent 判定达到影响阈值 -> 组装 DecisionRequest -> 交给 Decision Engine 契约；
  5. 输出预约 Action Queue（不付款）与 .ics / Markdown 行程单。

运行（在项目根目录）：
    python -m demo.demo_scenario
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import List, Optional

# Windows 控制台默认 GBK，无法打印 UTF-8 emoji；统一重配置为 UTF-8（失败则忽略）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from booking.booking_manager import BookingManager
from core.schemas import DayPlan, DecisionRequest, MonitorEvent, Place, TripTimeline
from decision.decision_engine import DecisionEngine
from execution.execution_agent import ExecutionAgent
from itinerary.ics_exporter import write_ics
from itinerary.markdown_exporter import write_markdown
from tools import build_registry
from tools.mock_data import MockWorld

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(message)s")

D1 = date(2026, 8, 1)
D2 = date(2026, 8, 2)


def build_timeline() -> TripTimeline:
    """构造初始行程时间轴（正式版由 A 的 Route Planner 产出）。"""
    return TripTimeline(
        city="北京",
        start_date=D1,
        end_date=D2,
        days=[
            DayPlan(day=1, date=D1, items=[
                Place(name="故宫", category="scenic", arrival="09:00", queue_min=20,
                      ticket_required=True, price=60.0),
                Place(name="景山公园", category="scenic", arrival="14:00", queue_min=5),
                Place(name="全聚德(前门店)", category="food", arrival="18:00"),
            ]),
            DayPlan(day=2, date=D2, items=[
                Place(name="天坛", category="scenic", arrival="09:00", queue_min=15,
                      ticket_required=True, price=15.0),
            ]),
        ],
    )


def make_decision_printer():
    """打印 DecisionEngine 的重规划结果（Explainable 决策展示）。"""
    def printer(req: DecisionRequest, replan) -> None:
        ev = req.events[0]
        data = ev.data or {}
        print(f"\n  ⚡ [Decision Engine 收到请求] {req.events[0].event_id}")
        print(f"     事件: {ev.event_type.value} @ {ev.place}")
        print(f"     观测数据: {data}")
        if replan is None:
            print("     → 影响可忽略，不重规划")
        else:
            print(f"     → 重规划！原因: {replan.reason}")
            for d in replan.diff_summary:
                print(f"       • {d}")
    return printer


def make_event_printer():
    def printer(ev: MonitorEvent) -> None:
        data = ev.data or {}
        print(f"  📡 [{ev.event_type.value}] {ev.place} @ {ev.observed_at:%H:%M:%S} -> {data}")
    return printer


def run_demo() -> None:
    world = MockWorld()
    registry = build_registry(world)
    timeline = build_timeline()

    # 真正的 Decision Engine（stub 版，A 可后续替换为 LLM 驱动）
    engine = DecisionEngine(impact_threshold=50)
    decision_printer = make_decision_printer()

    # 包装：让 decision_hook 打印结果后再返回给 ExecutionAgent 应用
    def decision_hook(req: DecisionRequest):
        replan = engine(req)
        decision_printer(req, replan)
        return replan

    agent = ExecutionAgent(
        timeline=timeline,
        registry=registry,
        decision_hook=decision_hook,
        on_event=make_event_printer(),
    )

    print("=" * 72)
    print("TravelAgent Demo —— 持续执行 / 自主决策 / 人机协同")
    print("=" * 72)

    print("\n【1】初始行程时间轴（Route Planner 输出契约）")
    notes = ["初始方案：Day1 故宫→景山→全聚德；Day2 天坛。预算 800，喜欢历史，讨厌排队。"]
    write_markdown(timeline, "output/行程单.md", notes)
    write_ics(timeline, "output/行程.ics")
    print(render_snippet("output/行程单.md"))
    print("  （.ics 已生成：output/行程.ics，可导入日历）")

    print("\n【2】执行阶段：周期性轮询（天气 30min / 交通 5min，此处为同步快照）")
    agent.poll_once()

    print("\n【3】突发剧情：天气转暴雨 + 故宫排队 20 -> 120 分钟")
    world.set_weather(condition="暴雨", rain_probability=85, uv_index=2)
    world.set_queue("故宫", 120)

    print("  上午 08:45（故宫 09:00 到达前 20 分钟）到达前监控触发：")
    now = datetime(2026, 8, 1, 8, 45)
    agent.check_lookahead(now)

    print("\n  上午 08:50 再次轮询天气，确认影响：")
    agent.poll_once()

    print(f"\n【4】汇总：DecisionEngine 共处理 {len(engine.history)} 次决策，"
          f"其中 {sum(1 for h in engine.history if h is not None)} 次触发重规划")

    print("\n【5】预约闭环（Booking Agent：只准备，不付款）")
    bm = BookingManager(registry)
    rec = bm.prepare("故宫", target_date="2026-08-01", party_size=2)
    print(f"  ✔ 已准备预约 {rec.place}：id={rec.booking_id}，状态={rec.status.value}")
    pay = bm.payment_action(rec.booking_id)
    print(f"  ⚠ 付款提醒（人工执行）：{pay.title} [{pay.permission.value}]")
    for action in bm.actions():
        print(f"     - [{action.status.value}/{action.permission.value}] {action.title}")

    print("\n【6】更新后的行程单（DecisionEngine 重规划结果已应用）")
    write_markdown(agent.timeline, "output/行程单_final.md",
                   notes=[f"重规划原因：{h.reason}" for h in engine.history if h is not None])
    print("  ✔ output/行程单_final.md 已更新（含重规划后的时间轴）")
    print("\n" + "=" * 72)
    print("Demo 结束：架构闭环 = 持续监控 → 影响判定 → 决策请求 → 预约/导出")
    print("=" * 72)


def render_snippet(path: str, max_lines: int = 8) -> str:
    with open(path, encoding="utf-8") as fh:
        return "\n".join(fh.read().splitlines()[:max_lines])


if __name__ == "__main__":
    run_demo()
