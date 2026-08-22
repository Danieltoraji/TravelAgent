"""旅行决策（Decision Engine）的数据结构：事件与影响评分的 JSON Schema 与格式化工具。

对应「持续执行中发现变化 → 判断是否值得重规划」这一能力：
- ``event_schema`` 约束注入的变化事件（排队 / 关闭 / 天气等）。
- ``decision_score_schema`` 约束大模型输出（影响分 0-100 + 判断依据）。
- ``format_events_text`` 把事件列表渲染成易读中文，供 prompt 使用（纯函数、可离线测试）。
- ``DECISION_THRESHOLD`` 是触发重规划的默认影响分阈值（沿用计划文档的 40）。

决策口径（8.19 确认，替换计划文档中的纯规则评分）：
由大模型结合【用户需求】与【变化情况】给出影响分，``triggered = score >= DECISION_THRESHOLD``；
景点关闭（closed）属于行程硬不可行，由规则直接触发（见 ``call_llm/decision_engine.py``
的 ``hard_rule_decision``），不调 LLM。
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

from data_transmission.requirement import nullable_integer, nullable_string

# 触发重规划的默认影响分阈值
DECISION_THRESHOLD = 40

# 事件类型 → 中文标签
EVENT_TYPE_LABELS = {
    "queue": "排队激增",
    "closed": "景点关闭",
    "weather": "天气变化",
    "traffic": "交通延误",
    "budget": "预算变化",
    "hotel": "酒店变化",
}

# 严重程度 → 中文标签
SEVERITY_LABELS = {"high": "高", "medium": "中", "low": "低"}

# 结构化数值键 → 中文标签（metrics 渲染用）
METRICS_LABELS = {
    "queue_minutes": "目标排队时长（分钟）",
    "queue_delta_minutes": "排队时长增量（分钟）",
    "travel_time_delta_minutes": "交通耗时增量（分钟）",
    "from": "交通起点",
    "to": "交通终点",
    "budget_delta": "预算变化（元）",
    "hotel_id": "酒店 ID",
    "hotel_full": "酒店满房",
    "price_delta": "房价变化（元/晚）",
}

# 事件携带的结构化数值（动态状态）。各事件类型按需取键：
#   queue   → queue_minutes（目标排队分钟数）/ queue_delta_minutes（相对基线增量）
#   traffic → travel_time_delta_minutes + from / to
#   budget  → budget_delta
#   hotel   → hotel_id（目标酒店）+ hotel_full（满房标记）/ price_delta（每晚价格增量）
# 这是 RePlanner 的输入契约：直接用数值计算，不需要解析 detail 自然语言。
event_metrics_schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "queue_minutes": {**nullable_integer, "title": "目标排队时长（分钟）"},
        "queue_delta_minutes": {**nullable_integer, "title": "排队时长增量（分钟）"},
        "travel_time_delta_minutes": {
            **nullable_integer,
            "title": "交通耗时增量（分钟）",
        },
        "from": {**nullable_string, "title": "交通起点"},
        "to": {**nullable_string, "title": "交通终点"},
        "budget_delta": {**nullable_integer, "title": "预算变化（元）"},
        "hotel_id": {**nullable_string, "title": "酒店 ID"},
        "hotel_full": {
            **nullable_integer,
            "title": "酒店满房",
            "description": "1 表示目标酒店满房（硬不可行），0 或 null 表示未满房",
        },
        "price_delta": {**nullable_integer, "title": "房价变化（元/晚）"},
    },
}

event_schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "event_type": {
            "type": "string",
            "enum": ["queue", "closed", "weather", "traffic", "budget", "hotel"],
            "title": "事件类型",
            "description": (
                "queue 排队激增 / closed 景点关闭 / weather 天气变化 / "
                "traffic 交通延误 / budget 预算变化 / hotel 酒店变化（满房、涨价）"
            ),
        },
        "spot": {
            **nullable_string,
            "title": "关联景点",
            "description": "事件涉及的景点名称；不涉及具体景点（如整体预算变化）可为 null",
        },
        "severity": {
            "type": "string",
            "enum": ["high", "medium", "low"],
            "title": "严重程度",
        },
        "detail": {
            "type": "string",
            "title": "事件详情",
            "description": "自然语言描述，例如「预计排队时间从 20 分钟增至 120 分钟」",
        },
        "metrics": {
            **event_metrics_schema,
            "title": "结构化数值",
            "description": "可选；RePlanner 直接消费的数值（如排队分钟数），不要求与 detail 严格一致",
        },
    },
    "required": ["event_type", "severity", "detail"],
}

decision_score_schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "score": {
            "type": "integer",
            "title": "影响分",
            "description": "0-100 的整数：0 表示完全无影响，100 表示行程完全不可行",
            "minimum": 0,
            "maximum": 100,
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "title": "判断依据",
            "description": (
                "逐条列出打分依据，必须具体、可解释（引用景点、事件与用户需求），"
                "供直接展示给用户"
            ),
        },
    },
    "required": ["score", "reasons"],
}


def format_events_text(events: Sequence[Dict[str, Any]]) -> str:
    """把变化事件列表渲染成给大模型看的中文文本。"""
    if not events:
        return "（无变化事件）"

    blocks: List[str] = []
    for index, event in enumerate(events, start=1):
        event_type = event.get("event_type", "unknown")
        severity = event.get("severity", "unknown")
        lines = [
            f"事件 {index}：{EVENT_TYPE_LABELS.get(event_type, event_type)}"
            f"（严重程度：{SEVERITY_LABELS.get(severity, severity)}）"
        ]
        spot = event.get("spot")
        if spot:
            lines.append(f"  关联景点：{spot}")
        detail = event.get("detail")
        if detail:
            lines.append(f"  详情：{detail}")
        metrics = event.get("metrics") or {}
        if isinstance(metrics, dict):
            rendered = "；".join(
                f"{METRICS_LABELS.get(key, key)}={value}"
                for key, value in metrics.items()
                if value is not None
            )
            if rendered:
                lines.append(f"  结构化数据：{rendered}")
        blocks.append("\n".join(lines))
    return "\n".join(blocks)
