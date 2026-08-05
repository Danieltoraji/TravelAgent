"""Demo 剧情脚本：跑通"持续监控 → 事件 → 决策请求"闭环。

混合模式（真实 API + 模拟突发事件）：
  1. 用户提出需求（Planner 由 A 负责，本 Demo 直接构造初始行程时间轴）；
  2. 执行阶段：真实 API 轮询天气/交通 + 到达前查看景点/餐饮（展示真实数据）；
  3. 突发剧情注入：天气转暴雨 + 故宫排队 20→120 分钟 + 交通拥堵延误 45 分钟；
  4. Execution Agent 判定达到影响阈值 → 组装 DecisionRequest → 交给 Decision Engine；
  5. 输出预约 Action Queue（不付款）与 .ics / Markdown 行程单。

运行（在项目根目录）：
    python -m demo.demo_scenario

注意：混合模式依赖真实 API Key（见 config/local_settings.py）。
      若无 Key 或网络不可用，设置 demo_mode=True 可回退纯 Mock 模式。
"""

from __future__ import annotations

import logging
import sys
from datetime import date, datetime
from typing import Any, Dict, List, Optional

# Windows 控制台默认 GBK，无法打印 UTF-8 emoji；统一重配置为 UTF-8（失败则忽略）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

from booking.booking_manager import BookingManager
from config.settings import settings
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

# ── 输出美化工具 ────────────────────────────────────────────────────────

def _sep(title: str = "", char: str = "─", width: int = 72) -> str:
    """生成分隔线，可选标题居中。"""
    if title:
        pad = max(0, (width - len(title) - 4) // 2)
        return f"{char * pad}  {title}  {char * (width - len(title) - 4 - pad)}"
    return char * width


def _fmt_weather(data: Dict[str, Any]) -> str:
    """格式化天气数据为单行摘要。"""
    cond = data.get("condition", "?")
    temp = data.get("temperature_c", 0)
    rain = data.get("rain_probability", 0)
    uv = data.get("uv_index", 0)
    wind = data.get("wind_kmh", 0)
    wind_dir = data.get("wind_dir", "")
    wind_str = f"{wind_dir}风 {wind}km/h" if wind_dir else f"{wind}km/h"
    return f"{cond} {temp}°C | 降雨概率 {rain}% | UV {uv} | {wind_str}"


def _fmt_traffic(data: Dict[str, Any]) -> str:
    """格式化交通数据为单行摘要。"""
    dur = data.get("duration_min", 0)
    cong = data.get("congestion", "?")
    delay = data.get("delay_min", 0)
    mode = data.get("mode", "?")
    note = data.get("note", "")
    dist = data.get("distance_km", 0)
    delay_str = f" ⚠延误 {delay}min" if delay > 0 else ""
    return f"{mode} {dur}min | {dist}km | {cong}{delay_str} | {note}"


def _fmt_scenic(data: Dict[str, Any]) -> str:
    """格式化景点数据为单行摘要。"""
    place = data.get("place", "?")
    queue = data.get("queue_min", 0)
    hours = data.get("open_hours", "未知")
    price = data.get("price", 0)
    rating = data.get("rating", 0)
    ticket = "需预约" if data.get("ticket_required") else "免预约"
    queue_str = f"排队 {queue}min" if queue > 0 else "无需排队"
    rating_str = f" ⭐{rating}" if rating > 0 else ""
    return f"{place}{rating_str} | {queue_str} | {ticket} ¥{price} | 营业 {hours}"


def _fmt_food(results: List[Dict[str, Any]]) -> str:
    """格式化餐饮推荐结果。"""
    if not results:
        return "  （无推荐结果）"
    lines = []
    for r in results[:3]:
        name = r.get("name", "?")
        rating = r.get("rating", 0)
        price = r.get("price_per_person", 0)
        cuisine = r.get("cuisine", "")
        dist = r.get("distance_km", 0)
        open_hours = r.get("open_hours", "")
        specialty = r.get("specialty", "")
        parts = [f"  🍽️  {name} | ⭐{rating} | ¥{price}/人 | {cuisine} | {dist}km"]
        if open_hours:
            parts.append(f"营业 {open_hours}")
        if specialty:
            parts.append(f"特色 {specialty}")
        lines.append(" | ".join(parts))
    return "\n".join(lines)


# ── 行程构造 ────────────────────────────────────────────────────────────

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


# ── 回调工厂 ────────────────────────────────────────────────────────────

def make_decision_printer():
    """打印 DecisionEngine 的重规划结果（Explainable 决策展示）。"""
    def printer(req: DecisionRequest, replan) -> None:
        ev = req.events[0]
        data = ev.data or {}
        print(f"\n  ⚡ [Decision Engine 收到请求] {ev.event_id}")
        print(f"     事件: {ev.event_type.value} @ {ev.place}")
        print(f"     观测数据: {data}")
        if replan is None:
            print("     → ❌ 影响可忽略，不重规划")
        else:
            print(f"     → ✅ 重规划！原因: {replan.reason}")
            for d in replan.diff_summary:
                print(f"       • {d}")
    return printer


def make_event_printer():
    """打印监控事件，按事件类型格式化输出。"""
    def printer(ev: MonitorEvent) -> None:
        data = ev.data or {}
        etype = ev.event_type.value
        ts = ev.observed_at.strftime("%H:%M:%S")
        if etype == "weather":
            print(f"  🌤️  [天气] {ev.place} @ {ts} → {_fmt_weather(data)}")
        elif etype == "traffic":
            print(f"  🚗 [交通] {ev.place} @ {ts} → {_fmt_traffic(data)}")
        elif etype == "scenic":
            print(f"  🏛️  [景点] {ev.place} @ {ts} → {_fmt_scenic(data)}")
        elif etype == "food":
            print(f"  🍴 [餐饮] {ev.place} @ {ts}")
            print(_fmt_food(data) if isinstance(data, list) else f"     {data}")
        else:
            print(f"  📡 [{etype}] {ev.place} @ {ts} → {data}")
    return printer


# ── Demo 主流程 ─────────────────────────────────────────────────────────

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

    mode_label = "混合模式（真实 API + 模拟突发事件）" if settings.use_real_api else "纯 Mock 模式"

    print("=" * 72)
    print(f"TravelAgent Demo —— 持续执行 / 自主决策 / 人机协同")
    print(f"  数据模式：{mode_label}")
    print("=" * 72)

    # ── 【1】初始行程 ─────────────────────────────────────────────
    print(f"\n{_sep('【1】初始行程时间轴', '═')}")
    notes = ["初始方案：Day1 故宫→景山→全聚德；Day2 天坛。预算 800，喜欢历史，讨厌排队。"]
    write_markdown(timeline, "output/行程单.md", notes)
    write_ics(timeline, "output/行程.ics")
    print(render_snippet("output/行程单.md"))
    print("  📅 .ics 已生成：output/行程.ics（可导入日历）")

    # ── 【2】首次轮询（真实 API） ─────────────────────────────────
    print(f"\n{_sep('【2】首次轮询：天气 + 交通（真实 API 快照）', '═')}")
    events = agent.poll_once()
    for ev in events:
        # on_event 回调已打印格式化输出，这里补充说明
        if ev.event_type.value == "weather":
            print(f"  → 当前天气数据来自{'真实 API' if settings.use_real_api else 'Mock'}")

    # ── 【2b】到达前检查：景点 + 餐饮（真实 API） ─────────────────
    print(f"\n{_sep('【2b】到达前检查：景点 + 餐饮（真实 API 深度信息）', '═')}")
    print("  模拟时间 08:30（故宫 09:00 到达前 30 分钟）触发到达前监控：")
    now_early = datetime(2026, 8, 1, 8, 30)
    # 先触发景点规则（fire_at = 08:40，08:30 还没到，手动提前触发）
    # 直接调用工具展示真实数据
    if settings.use_real_map_api:
        print("\n  📊 故宫真实信息（高德 v5 POI API）：")
        scenic_result = registry.call("scenic", place="故宫")
        if scenic_result.status.value == "ok" and scenic_result.data:
            print(f"     {_fmt_scenic(scenic_result.data)}")

        print("\n  🍽️  故宫附近餐厅推荐（高德 v5 周边搜索 API）：")
        food_result = registry.call("food", near="故宫")
        if food_result.status.value == "ok" and food_result.data:
            print(_fmt_food(food_result.data))
    else:
        print("  （Mock 模式，跳过真实 API 展示）")

    # ── 【3】突发剧情注入 ─────────────────────────────────────────
    print(f"\n{_sep('【3】突发剧情注入：3 个模拟事件', '═')}")
    print("  ⚡ 事件 1：天气转暴雨（rain_probability 10% → 85%）")
    world.set_weather(condition="暴雨", rain_probability=85, uv_index=2)

    print("  ⚡ 事件 2：故宫排队暴涨（20min → 120min）")
    world.set_queue("故宫", 120)

    print("  ⚡ 事件 3：交通拥堵（北京→故宫 延误 45 分钟）")
    world.set_traffic_delay("北京", "故宫", delay_min=45, congestion="拥堵")

    # ── 【3b】到达前触发 + 再次轮询 ───────────────────────────────
    print(f"\n{_sep('【3b】突发事件后：到达前触发 + 再次轮询', '═')}")
    print("  模拟时间 08:45（故宫 09:00 到达前 15 分钟）到达前监控触发：")
    now = datetime(2026, 8, 1, 8, 45)
    agent.check_lookahead(now)

    print("\n  模拟时间 08:50 再次轮询天气 + 交通，确认影响：")
    agent.poll_once()

    # ── 【4】决策汇总 ─────────────────────────────────────────────
    print(f"\n{_sep('【4】决策汇总', '═')}")
    total = len(engine.history)
    triggered = sum(1 for h in engine.history if h is not None)
    print(f"  DecisionEngine 共处理 {total} 次决策，其中 {triggered} 次触发重规划")
    if total > 0:
        print(f"  影响评分表：天气=40 / 景点排队=80 / 交通=20 / 餐饮=5（阈值=50）")

    # ── 【5】预约闭环 ─────────────────────────────────────────────
    print(f"\n{_sep('【5】预约闭环（Booking Agent：准备→提交→确认→付款提醒）', '═')}")
    bm = BookingManager(registry)
    # Step 1: prepare — 自动调用 scenic Tool 填充景点信息
    rec = bm.prepare("故宫", target_date="2026-08-01", party_size=2)
    print(f"  ✔ 已准备预约 {rec.place}：id={rec.booking_id}，状态={rec.status.value}")
    print(f"    类型={rec.booking_type}，票价=¥{rec.price}，电话={rec.tel or '无'}")
    print(f"    地址={rec.address or '无'}，营业时间={rec.open_hours or '无'}")
    # Step 2: confirm — 用户确认后调用 submit 模拟提交
    rec = bm.confirm(rec.booking_id)
    print(f"  ✔ 用户已确认，提交成功：确认码={rec.confirm_code}，状态={rec.status.value}")
    # Step 3: mark_confirmed — 服务方确认
    rec = bm.mark_confirmed(rec.booking_id)
    print(f"  ✔ 服务方已确认：状态={rec.status.value}")
    # Step 4: payment_action — 付款提醒（人工执行）
    pay = bm.payment_action(rec.booking_id)
    print(f"  ⚠ 付款提醒（人工执行）：{pay.title} [{pay.permission.value}]")
    print(f"  {'─' * 50}")
    print(f"  Action Queue（{len(bm.actions())} 项）：")
    for action in bm.actions():
        print(f"     - [{action.status.value}/{action.permission.value}] {action.title}")

    # ── 【6】最终行程单 ───────────────────────────────────────────
    print(f"\n{_sep('【6】更新后的行程单（重规划结果已应用）', '═')}")
    replan_notes = [f"重规划原因：{h.reason}" for h in engine.history if h is not None]
    write_markdown(agent.timeline, "output/行程单_final.md", notes=replan_notes)
    print("  ✔ output/行程单_final.md 已更新（含重规划后的时间轴）")

    print(f"\n{'=' * 72}")
    print("Demo 结束：架构闭环 = 持续监控 → 影响判定 → 决策请求 → 预约/导出")
    print(f"{'=' * 72}")


def render_snippet(path: str, max_lines: int = 8) -> str:
    with open(path, encoding="utf-8") as fh:
        return "\n".join(fh.read().splitlines()[:max_lines])


if __name__ == "__main__":
    run_demo()
