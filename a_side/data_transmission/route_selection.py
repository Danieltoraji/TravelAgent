"""旅行路线筛选：候选路线的 JSON Schema 与格式化工具。

对应「把几条合理路线交给大模型筛选并解释」这一能力：
- ``route_selection_schema`` 约束大模型输出（选中序号、排序、优缺点、理由、说明）。
- ``format_requirement_context`` / ``format_routes_text`` 把结构化数据渲染成
  大模型易读的中文，供 prompt 使用，均为纯函数、可离线测试。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

route_selection_schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "selected_route_index": {
            "type": "integer",
            "title": "最终选中的路线序号",
            "description": "从 1 开始的候选路线序号，应等于 ranking 中评分最高的一项",
        },
        "ranking": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "route_index": {
                        "type": "integer",
                        "title": "路线序号",
                        "minimum": 1,
                    },
                    "summary": {"type": "string", "title": "一句话概括"},
                    "score": {
                        "type": "number",
                        "title": "综合评分",
                        "minimum": 0,
                        "maximum": 10,
                    },
                    "pros": {
                        "type": "array",
                        "items": {"type": "string"},
                        "title": "优点",
                    },
                    "cons": {
                        "type": "array",
                        "items": {"type": "string"},
                        "title": "缺点",
                    },
                },
                "required": ["route_index", "summary", "score", "pros", "cons"],
            },
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "title": "选中理由",
            "description": "逐条说明为什么选中该路线",
        },
        "explanation": {
            "type": "string",
            "title": "可解释说明",
            "description": "面向用户的一段完整说明，讲清楚选了什么、为什么",
        },
    },
    "required": ["selected_route_index", "ranking", "reasons", "explanation"],
}


def format_requirement_context(requirement: Dict[str, Any]) -> str:
    """把结构化需求转成给大模型看的中文摘要（忽略空值）。"""
    content = requirement.get("content", {}) or {}
    lines: List[str] = []

    if content.get("destination"):
        lines.append(f"目的地：{content['destination']}")
    if content.get("start_date"):
        lines.append(f"出发日期：{content['start_date']}")
    if content.get("days"):
        lines.append(f"天数：{content['days']} 天")
    if content.get("visitor_number"):
        lines.append(f"人数：{content['visitor_number']} 人")

    constraints = content.get("constraints", {}) or {}
    if constraints.get("budget") is not None:
        lines.append(f"总预算：{constraints['budget']} 元")
    must_visit = constraints.get("must_visit") or []
    if must_visit:
        lines.append(f"必去景点：{'、'.join(must_visit)}")
    if constraints.get("daily_travel_time"):
        lines.append(f"每日出游时长：{constraints['daily_travel_time']} 分钟")
    include_meal = constraints.get("include_meal_time_in_daily_limit")
    if include_meal is not None:
        lines.append(f"用餐时间计入每日时长：{'是' if include_meal else '否'}")

    preferences = content.get("preferences", {}) or {}
    preferred = preferences.get("preferred_tags") or []
    if preferred:
        lines.append(f"偏好标签：{'、'.join(preferred)}")
    avoid = preferences.get("avoid_tags") or []
    if avoid:
        lines.append(f"回避标签：{'、'.join(avoid)}")

    if not lines:
        return "- （无明确需求）"
    return "\n".join(f"- {line}" for line in lines)


def format_routes_text(routes: Sequence[Dict[str, Any]]) -> str:
    """把候选路线列表渲染成带序号的易读文本，供大模型比较。

    输入形如 ``generate_route_candidates`` 的 ``routes`` 字段：
    ``[{"days": [{"day": 1, "spots": [{"name", "time_period"}]}]}]``。
    """
    if not routes:
        return "（无候选路线）"

    blocks: List[str] = []
    for index, route in enumerate(routes, start=1):
        lines = [f"路线 {index}："]
        for day in route.get("days", []):
            day_number = day.get("day", "?")
            spots = day.get("spots", [])
            meals = day.get("meals", [])
            if not spots:
                lines.append(f"  第 {day_number} 天：（无景点）")
            else:
                segments = []
                for spot in spots:
                    name = spot.get("name", "?")
                    time_period = spot.get("time_period", "")
                    segments.append(f"{name}({time_period})" if time_period else name)
                lines.append(f"  第 {day_number} 天：{' → '.join(segments)}")
            for meal in meals:
                name = meal.get("name", "")
                restaurant = meal.get("restaurant", "")
                time_period = meal.get("time_period", "")
                label = f"{name} @ {restaurant}" if restaurant else name
                lines.append(
                    f"    {label}({time_period})" if time_period else f"    {label}"
                )
        blocks.append("\n".join(lines))

    return "\n\n".join(blocks)
