"""Markdown 行程单导出：把 TripTimeline 渲染为易读的 Markdown 行程单。"""

from __future__ import annotations

import os
from typing import List, Optional

from core.schemas import TripTimeline

_CATEGORY_ICON = {
    "scenic": "🏛️",
    "food": "🍽️",
    "hotel": "🏨",
    "transport": "🚇",
    "shopping": "🛍️",
}


def render_markdown(timeline: TripTimeline, notes: Optional[List[str]] = None) -> str:
    """渲染行程单 Markdown 文本。"""
    lines: List[str] = [
        f"# {timeline.city} 行程单",
        "",
        f"**行程时间**：{timeline.start_date.isoformat()} ~ {timeline.end_date.isoformat()}",
        "",
    ]
    for day in timeline.days:
        lines.append(f"## Day {day.day} · {day.date.isoformat()}")
        lines.append("")
        for item in day.items:
            icon = _CATEGORY_ICON.get(item.category, "📍")
            ticket = " 🔖需预约" if item.ticket_required else ""
            lines.append(
                f"- {icon} {item.arrival} **{item.name}**"
                f"（{item.category}，预计排队 {item.queue_min} 分钟）{ticket}"
            )
        lines.append("")
    if notes:
        lines.append("## 说明")
        lines.append("")
        for note in notes:
            lines.append(f"- {note}")
        lines.append("")
    return "\n".join(lines)


def write_markdown(timeline: TripTimeline, path: str,
                   notes: Optional[List[str]] = None) -> str:
    """生成 Markdown 行程单文件并返回内容。自动创建父目录。"""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    content = render_markdown(timeline, notes)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content)
    return content
