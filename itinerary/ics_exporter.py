"""日历导出：生成 .ics（RFC 5545），供 Google Calendar / Outlook / iOS 导入。

核心零依赖：直接用标准库拼装 iCalendar 文本。
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta
from typing import Any, List

from core.schemas import TripTimeline

PRODID = "-//TravelAgent//行程单//CN"


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y%m%dT%H%M%S")


def _parse_hhmm(arrival: str) -> str:
    parts = arrival.split(":")
    return f"{int(parts[0]):02d}:{int(parts[1]):02d}" if len(parts) >= 2 else "09:00"


def build_ics(timeline: TripTimeline, duration_min: int = 90) -> str:
    """把行程时间轴渲染为 .ics 文本。每个地点生成一个 VEVENT。"""
    lines: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
    ]
    now = datetime.now()
    for day in timeline.days:
        for item in day.items:
            hhmm = _parse_hhmm(item.arrival)
            start = datetime.combine(day.date, datetime.strptime(hhmm, "%H:%M").time())
            end = start + timedelta(minutes=duration_min)
            lines += [
                "BEGIN:VEVENT",
                f"UID:{uuid.uuid4()}@travelagent",
                f"DTSTAMP:{_fmt_dt(now)}Z",
                f"DTSTART:{_fmt_dt(start)}",
                f"DTEND:{_fmt_dt(end)}",
                f"SUMMARY:{item.name}",
                f"LOCATION:{timeline.city}",
                f"DESCRIPTION:{item.category} / 预计排队 {item.queue_min} 分钟",
                "END:VEVENT",
            ]
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


def write_ics(timeline: TripTimeline, path: str, duration_min: int = 90) -> str:
    """生成 .ics 文件并返回内容。自动创建父目录。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    content = build_ics(timeline, duration_min)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return content
