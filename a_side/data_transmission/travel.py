"""出行信息：出发地 / 出行时段的澄清追问 + 城际来去程行程段生成。

用户输入的出行时段**只接受标准日期**（YYYY-MM-DD）+ 24 小时制时刻；
「周五」「周末」等星期 / 模糊输入不被允许，由 ``clarify_travel`` 追问到标准日期。

- ``parse_travel_answer``：把「2026-08-21 20:00 出发，2026-08-23 20:00 返回」
  解析成 travel_schedule；不确定（星期 / 模糊 / 非法日期 / 返程早于去程）返回 None。
- ``clarify_travel``：缺出发地 / 出行时段时询问用户，模糊则追问，力求精确到日期和时刻。
- ``build_trip_segments``：按 origin + travel_schedule 生成城际来去程行程段
  （时间轴头尾的 transport 事件，**不占每日游玩时长**；后续动态压缩基于这些段扩展）。
"""

from __future__ import annotations

import logging
import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Tuple

from data_transmission.city_travel import (
    AIR_BUFFER_MIN,
    IntercityRoute,
    load_city_travel_options,
    mode_text,
)
from data_transmission.enums import Mode, Source
from data_transmission.intercity_strategy import (
    AirRailStrategy,
    DirectFallbackStrategy,
    DirectStrategy,
    GraphBfsStrategy,
    IntercityStrategyContext,
    resolve_intercity_chain,
)

logger = logging.getLogger("data_transmission.travel")

# 城际预算份额（8.30 预算贯通）：总预算中划给「来去城际交通」的比例——
# 四项大头（城际/住宿/餐饮/门票）里城际约占 40%；单程上限 = budget × 40% ÷ 2。
TRANSIT_BUDGET_SHARE = 0.4

# 标准日期：2026-08-21 / 2026/8/21 / 2026年8月21日（必须带 4 位年份）
_DATE_PATTERN = r"(\d{4})\s*[-/年]\s*(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"

# 「2026-08-21 20:00 出发，2026-08-23 20:00 返回」或「2026年8月21日20点出发，8月23日20点回」
_TRAVEL_ANSWER_PATTERN = re.compile(
    rf"{_DATE_PATTERN}\s*(\d{{1,2}})\s*(?:[:：点时])\s*(\d{{1,2}})?\s*分?"
    rf"\s*(?:出发|去|走)?\s*(?:，|,|。|和|然后|再)?\s*"
    rf"{_DATE_PATTERN}\s*(\d{{1,2}})\s*(?:[:：点时])\s*(\d{{1,2}})?\s*分?"
    rf"\s*(?:返回|回|到家)?"
)


def _norm_time(hour_text: str, minute_text: Optional[str]) -> Optional[str]:
    """24 小时制 HH:MM；越界（hour>23 / minute>59）返回 None 表示不合法。"""
    hour = int(hour_text)
    minute = int(minute_text) if minute_text else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return f"{hour:02d}:{minute:02d}"


def _norm_date(year_text: str, month_text: str, day_text: str) -> Optional[str]:
    """标准 YYYY-MM-DD；年份超出 2000–2100 或日期不存在（如 2026-02-30）→ None。"""
    try:
        year, month, day = int(year_text), int(month_text), int(day_text)
        if not (2000 <= year <= 2100):
            return None
        return date(year, month, day).isoformat()
    except ValueError:
        return None


def parse_travel_answer(text: str) -> Optional[Dict[str, str]]:
    """把「2026-08-21 20:00 出发，2026-08-23 20:00 返回」解析成 travel_schedule。

    只接受标准日期（YYYY-MM-DD，兼容 YYYY/MM/DD、YYYY年M月D日）+ 24 小时制时刻；
    「周五」「周末」「周五晚上」等星期 / 模糊输入一律返回 None——由调用方追问。
    日期不存在、时间越界、以及返程日期早于去程日期也返回 None。
    """
    if not text:
        return None
    match = _TRAVEL_ANSWER_PATTERN.search(text)
    if not match:
        return None
    departure_date = _norm_date(match.group(1), match.group(2), match.group(3))
    departure_time = _norm_time(match.group(4), match.group(5))
    return_date = _norm_date(match.group(6), match.group(7), match.group(8))
    return_time = _norm_time(match.group(9), match.group(10))
    if None in (departure_date, departure_time, return_date, return_time):
        return None
    # 返程不得早于或等于去程（按「日期 + 时刻」整体比较，同日早回也算非法）
    def _minutes(text: str) -> int:
        hour, minute = text.split(":", 1)
        return int(hour) * 60 + int(minute)

    if (return_date, _minutes(return_time)) <= (
        departure_date,
        _minutes(departure_time),
    ):
        return None
    return {
        "departure_date": departure_date,
        "departure_time": departure_time,
        "return_date": return_date,
        "return_time": return_time,
    }


def clarify_travel(
    requirement: Dict[str, Any],
    user_input_fn: Optional[Callable[[str], str]] = None,
) -> Dict[str, Any]:
    """询问出发地与可出行时间；模糊则追问，力求精确到标准日期和时刻。原地更新 content。"""
    user_input_fn = user_input_fn or input
    content = requirement["content"]

    if not content.get("origin"):
        answer = user_input_fn("您从哪里出发？（例如：天津）").strip()
        if answer:
            content["origin"] = answer

    schedule = content.get("travel_schedule") or {}
    schedule_keys = ("departure_date", "departure_time", "return_date", "return_time")
    if not all(schedule.get(key) for key in schedule_keys):
        prompts = [
            "您计划什么时间出行？请给出具体的去程和返程日期与时刻"
            "（例如：2026-08-21 20:00 出发，2026-08-23 20:00 返回）：",
            "抱歉，只写星期（如「周五」）或模糊时间（如「周末」）是没法安排的。"
            "请按「YYYY-MM-DD HH:MM 出发，YYYY-MM-DD HH:MM 返回」的格式"
            "给出具体的日期和时刻（例如：2026-08-21 20:00 出发，"
            "2026-08-23 20:00 返回）：",
        ]
        for prompt in prompts:
            answer = user_input_fn(prompt).strip()
            parsed = parse_travel_answer(answer)
            if parsed:
                content["travel_schedule"] = parsed
                break
    return requirement


def _build_route_legs(route: Any) -> List[Dict[str, Any]]:
    """段级 legs（批次 2 两段式 + 2.5 联运扩展）：**首尾城市端点取自链本身**
    （``edges[0].origin`` / ``edges[-1].destination``），保证去程/返程方向一致
    （返程段 head 应为「张掖→甘州机场」而非去程方向的「天津→甘州机场」）。

    - 单段（train/air 站点对）→ ``[local, intercity, local]``；
    - 多段联运 → 每段一条 intercity leg，**段间插入同城转场 local 占位**
      （如 北京南站→大兴 的站际衔接，阶段三用 map 真源填充），首尾各一条
      local 接驳占位；
    - driving / 无站点 → 单条 intercity leg（城市对直达，无 local 骨架）。
    """
    edges = route.edges
    first = edges[0]
    last = edges[-1]
    head_city = first.origin          # 段起点城市（返程段 = 张掖）
    tail_city = last.destination     # 段终点城市（返程段 = 天津）

    def intercity_leg(e: Any) -> Dict[str, Any]:
        return {
            "kind": "intercity",
            "from": e.from_station or e.origin,
            "to": e.to_station or e.destination,
            "mode": e.mode,
            "duration_min": int(e.transport_minutes),
            "buffer_min": AIR_BUFFER_MIN if e.mode == Mode.AIR.value else 0,
            "cost_per_person": float(e.cost_per_person),
            "source": e.source,
            "note": "城际主段（方式已定）",
            "candidates": list(e.candidates),  # 真源候选列表（多条车次/航班），估算边为空
        }

    def local_leg(a: str, b: str, note: str) -> Dict[str, Any]:
        return {
            "kind": "local", "from": a, "to": b,
            "duration_min": None, "mode": "", "source": "", "note": note,
        }

    if not (first.from_station and first.to_station):
        # 城市级直达（driving 等）：无车站骨架，仅一条 intercity leg
        return [intercity_leg(first)]

    legs: List[Dict[str, Any]] = [
        local_leg(head_city, first.from_station, "市内衔接（阶段三 map 真源填充）")
    ]
    prev_to = first.to_station or first.destination
    for i, e in enumerate(edges):
        e_from = e.from_station or e.origin
        if i > 0 and e_from and prev_to and e_from != prev_to:
            legs.append(local_leg(
                prev_to, e_from, "同城转场（车站/机场衔接，阶段三 map 真源填充）"
            ))
        legs.append(intercity_leg(e))
        prev_to = e.to_station or e.destination
    legs.append(local_leg(
        last.to_station or prev_to or tail_city, tail_city,
        "市内衔接（阶段三 map 真源填充）",
    ))
    return legs


def _route_name(
    origin: str, destination: str, route: Any, schedule_name: str
) -> str:
    """段名：单段站点对 → 「天津站 → 北京南站（高铁）」；单段城市级 →
    「天津 → 北京（高铁）」；多段联运 → 「天津 → 北京 → 张掖（联运）」（中转城市串联）。"""
    if not route.is_chain:
        e = route.edges[0]
        if e.from_station and e.to_station:
            return f"{e.from_station} → {e.to_station}（{mode_text(e.mode)}）"
        return f"{origin} → {destination}（{mode_text(e.mode)}）"
    via = " → ".join(
        (e.destination) for e in route.edges[:-1]
    )
    return f"{origin} → {via} → {destination}（联运）"


def _resolve_intercity_route(
    origin: str,
    destination: str,
    provider: Optional[Callable[[str, str], Optional[Any]]],
    options: Dict[Tuple[str, str], Dict[str, Any]],
    priority: Optional[str] = None,
    date_str: str = "",
    budget_per_leg: Optional[float] = None,
) -> Optional[IntercityRoute]:
    """城际路线解析（P2：显式策略链，行为与旧四级级联一致），返回
    ``IntercityRoute`` 或 None：

    1. ``DirectStrategy``   直达（provider 真源 + 本地 options 按 priority
       偏好选方式）且完整耗时 ≤ 12h → 单段 route（I-11：air 含值机缓冲）；
    2. ``AirRailStrategy``  直达超 12h/不存在 → 空铁联运候选（航空拓扑正反
       向邻居 + 免费铁路过滤；铁路段 live、航段拓扑提示 estimated）+
       top 候选航段 juhe 真价验证；
    3. ``GraphBfsStrategy`` 候选无果 → 老 BFS（估算表邻接 + 段级真源升级）；
    4. ``DirectFallbackStrategy`` 全部无解 → 直达如实给出（如表外 driving
       19h；超 12h 的兜底边不再参与「软直达优先」基准——被迫选项非优选）。

    ``budget_per_leg``（8.30 预算贯通）：单程人均城际预算上限（由调用方按
    总预算分摊，如 budget × 40% ÷ 2 程）。提供时**不影响候选排序**（偏好
    优先），由链上的预算后处理器（``apply_budget_fallback``）按场景处理：
    直达超预算 → 维持首选 + warning（建议上调预算）；联运候选超预算 →
    回落预算内最便宜候选并打 warning；预算内无可行 → 维持首选 + warning
    （速度偏好超支时如实给出而非静默降级，用户可见可改）。注：航段 Day 3
    前 cost=0（拓扑无价格），纯航段候选的费用被低估——贯通只对含铁路段的
    候选精确。
    """
    ctx = IntercityStrategyContext(
        origin=origin,
        destination=destination,
        options=options,
        provider=provider,
        priority=priority,
        date_str=date_str,
        budget_per_leg=budget_per_leg,
    )
    return resolve_intercity_chain(
        ctx,
        [
            DirectStrategy(),
            AirRailStrategy(),
            GraphBfsStrategy(),
            DirectFallbackStrategy(),
        ],
    )


def build_trip_segments(
    plan: Dict[str, Any],
    requirement: Dict[str, Any],
    travel_provider: Optional[Callable[[str, str], Optional[Any]]] = None,
) -> List[Dict[str, Any]]:
    """按 origin + travel_schedule 生成城际来去程行程段（时间轴头尾，不占每日时长）。

    ``travel_provider``：``fn(origin, dest) -> Optional[CityTravelEdge]``——真实数据
    接入时由 ``live_data.make_live_city_travel_provider`` 注入；给定优先。

    路线选择（批次 2.5 区域模板）：直达（≤12h）优先 → 多段联运（区域枢纽候选
    枚举取最短，含 air 值机缓冲）→ truant 直达兜底如实给出。段 details 透传
    mode/车站对/费用/source/legs（两段式结构：城际方式已定，市内衔接预留）。

    返回形如：:
        [
          {"type": "transport", "name": "天津 → 北京 → 张掖（联运）",
           "day_label": "2026-08-04", "start_minutes": 540, "end_minutes": 780,
           "duration_minutes": 240,
           "details": {"from": "天津", "to": "张掖", "mode": "联运",
                       "from_station": "天津站", "to_station": "张掖甘州机场",
                       "cost_per_person": 655.0, "source": "estimated",
                       "kind": "outbound",
                       "legs": [local, intercity, 转场local, intercity, local]}},
          ...
        ]
    无 origin / 缺城际数据 / 缺对应日期或时刻 → 返回 []（向后兼容：纯目的地游无来去程）。
    """
    content = requirement.get("content", {})
    origin = content.get("origin")
    destination = content.get("destination")
    schedule = content.get("travel_schedule") or {}
    if not origin or not destination:
        return []

    # 城际交通偏好（批次「偏好驱动选方式」）：rail/air/speed/earliest/cost/None。
    # 影响直达方式选择（find_city_travel_preferred）与联运各段方式
    # （_pick_segment）：rail=高铁优先、air=飞机优先（链式命中）、speed=总耗时最短、
    # earliest=最早到达（当前与 speed 等价）、cost=人均费用最低、缺省=高铁优先链。
    priority = (content.get("preferences") or {}).get("travel_priority")
    options = load_city_travel_options()
    segments: List[Dict[str, Any]] = []

    # 预算贯通（8.30）：总预算 × 城际份额 ÷ 2 程 = 单程人均城际预算上限。
    # 份额取 40%（来去程城际是四项大头之一；住/吃/门票占 60%）。缺预算
    # （兼容旧调用方/测试）→ None 不约束。
    budget = (content.get("constraints") or {}).get("budget")
    budget_per_leg = (
        float(budget) * TRANSIT_BUDGET_SHARE / 2
        if isinstance(budget, (int, float)) and not isinstance(budget, bool) and budget > 0
        else None
    )

    def hhmm_to_minutes(text: str) -> Optional[int]:
        try:
            hour, minute = text.split(":", 1)
            return int(hour) * 60 + int(minute)
        except (ValueError, AttributeError):
            return None

    def make_segment(route: IntercityRoute, name: str, day_label: str,
                     start_minutes: int, kind: str) -> Dict[str, Any]:
        edges = route.edges
        first, last = edges[0], edges[-1]
        chain = route.is_chain
        return {
            "type": "transport",
            "name": name,
            "day_label": day_label,
            "start_minutes": start_minutes,
            "end_minutes": start_minutes + route.total_minutes,
            "duration_minutes": route.total_minutes,
            "details": {
                "from": origin if kind == "outbound" else destination,
                "to": destination if kind == "outbound" else origin,
                "mode": "联运" if chain else first.mode,
                "from_station": first.from_station or origin,
                "to_station": last.to_station or destination,
                "cost_per_person": route.total_cost,
                "source": Source.ESTIMATED.value if chain else first.source,
                "kind": kind,
                "stops": (
                    [e.destination for e in edges[:-1]] if chain else []
                ),
                "legs": _build_route_legs(route),
            },
        }

    outbound = _resolve_intercity_route(
        origin, destination, travel_provider, options, priority,
        date_str=(schedule.get("departure_date") or "").strip(),
        budget_per_leg=budget_per_leg,
    )
    if outbound is not None and schedule.get("departure_date") and schedule.get("departure_time"):
        start = hhmm_to_minutes(schedule["departure_time"])
        if start is not None:
            segments.append(make_segment(
                outbound,
                _route_name(origin, destination, outbound, "去程"),
                schedule.get("departure_date") or "去程",
                start,
                "outbound",
            ))

    # I-08：返程**独立解析** destination→origin，查不到就诚实无返程方案
    # （绝不复用去程方向 origin→destination 伪造反向段；去程可能只有单向数据）
    homeward = _resolve_intercity_route(
        destination, origin, travel_provider, options, priority,
        date_str=(schedule.get("return_date") or "").strip(),
        budget_per_leg=budget_per_leg,
    )
    if homeward is not None and schedule.get("return_date") and schedule.get("return_time"):
        # ⚠️ travel_schedule 时刻语义（9.2 用户拍板，勿再混淆）：
        #   departure_time = **从家出发**时刻（去程段 = [离家, 到目的地]）；
        #   return_time   = **最晚到家**时刻（返程段 = [离开目的地, 到家]）——
        #   绝不是「返程出发时刻」！把 return_time 直接当出发是常见错误
        #   （时间轴会显示 [20:00, 24:50] 而用户 20:00 就该到家，违反约束）。
        # 此处反推出发：start = return_time − 总耗时（真源历时含值机/转场缓冲）。
        # 班次级精排（从 intercity leg 的 candidates 里按「末段到家 ≤ return_time」
        # 且出发最晚选真实班次）由上层 b_planner_hook 在**末日行程排完后**按实际
        # 离开时间重建 return 段——本段是占位/兜底（无候选或候选不可达时使用）。
        start = hhmm_to_minutes(schedule["return_time"])
        if start is not None:
            start -= homeward.total_minutes
            if start < 0:
                # 到家时间早于「出发+历时」反推值 → 当天出发即超约束：置 0 点
                # 占位，让「返程日无游玩窗口」由后续窗口约束（last_day_end）
                # 显式暴露，不在这里静默抹平。
                start = 0
            segments.append(make_segment(
                homeward,
                _route_name(destination, origin, homeward, "返程"),
                schedule.get("return_date") or "返程",
                start,
                "return",
            ))
    return segments
