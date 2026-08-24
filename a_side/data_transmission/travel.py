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

import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from data_transmission.city_travel import find_city_travel, load_city_travel_edges

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


def build_trip_segments(
    plan: Dict[str, Any],
    requirement: Dict[str, Any],
    travel_provider: Optional[Callable[[str, str], Optional[Any]]] = None,
) -> List[Dict[str, Any]]:
    """按 origin + travel_schedule 生成城际来去程行程段（时间轴头尾，不占每日时长）。

    ``travel_provider``：``fn(origin, dest) -> Optional[CityTravelEdge]``——真实数据
    接入时由 ``live_data.make_live_city_travel_provider`` 注入（见 ``find_city_travel``）。

    返回形如：:
        [
          {"type": "transport", "name": "天津 → 北京（高铁）",
           "day_label": "2026-08-21", "start_minutes": 1200, "end_minutes": 1240,
           "duration_minutes": 40, "details": {...kind: "outbound"}},
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

    edges = load_city_travel_edges()
    segments: List[Dict[str, Any]] = []

    def hhmm_to_minutes(text: str) -> Optional[int]:
        try:
            hour, minute = text.split(":", 1)
            return int(hour) * 60 + int(minute)
        except (ValueError, AttributeError):
            return None

    outbound = find_city_travel(
        origin, destination, edges=edges, provider=travel_provider
    )
    if outbound is not None and schedule.get("departure_date") and schedule.get("departure_time"):
        start = hhmm_to_minutes(schedule["departure_time"])
        if start is not None:
            segments.append(
                {
                    "type": "transport",
                    "name": f"{origin} → {destination}（{outbound.mode}）",
                    "day_label": schedule.get("departure_date") or "去程",
                    "start_minutes": start,
                    "end_minutes": start + outbound.transport_minutes,
                    "duration_minutes": outbound.transport_minutes,
                    "details": {
                        "from": origin,
                        "to": destination,
                        "mode": outbound.mode,
                        "kind": "outbound",
                    },
                }
            )

    homeward = (
        find_city_travel(destination, origin, edges=edges, provider=travel_provider)
        or find_city_travel(origin, destination, edges=edges, provider=travel_provider)
    )
    if homeward is not None and schedule.get("return_date") and schedule.get("return_time"):
        start = hhmm_to_minutes(schedule["return_time"])
        if start is not None:
            segments.append(
                {
                    "type": "transport",
                    "name": f"{destination} → {origin}（{homeward.mode}）",
                    "day_label": schedule.get("return_date") or "返程",
                    "start_minutes": start,
                    "end_minutes": start + homeward.transport_minutes,
                    "duration_minutes": homeward.transport_minutes,
                    "details": {
                        "from": destination,
                        "to": origin,
                        "mode": homeward.mode,
                        "kind": "return",
                    },
                }
            )
    return segments
