"""Shared data format for itinerary timeline nodes."""

from __future__ import annotations

from typing import Any, Dict, Literal, TypedDict


ItineraryNodeType = Literal["spot", "transport", "meal", "waiting"]


class ItineraryNode(TypedDict):
    """Canonical internal node used by route planners.

    Times are integer minutes from 00:00. ``details`` carries type-specific
    data without changing the common scheduling interface.
    """

    type: ItineraryNodeType
    name: str
    start_minutes: int
    end_minutes: int
    duration_minutes: int
    details: Dict[str, Any]


itinerary_node_schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "type": {
            "type": "string",
            "enum": ["spot", "transport", "meal", "waiting"],
        },
        "name": {"type": "string"},
        "start_minutes": {"type": "integer", "minimum": 0},
        "end_minutes": {"type": "integer", "minimum": 0},
        "duration_minutes": {"type": "integer", "minimum": 0},
        "details": {"type": "object"},
    },
    "required": [
        "type",
        "name",
        "start_minutes",
        "end_minutes",
        "duration_minutes",
        "details",
    ],
}


def format_minutes(minutes: int) -> str:
    if not isinstance(minutes, int) or isinstance(minutes, bool) or minutes < 0:
        raise ValueError("minutes 必须是非负整数")
    hour, minute = divmod(minutes, 60)
    return f"{hour:02d}:{minute:02d}"


def build_itinerary_node(
    node_type: ItineraryNodeType,
    name: str,
    start_minutes: int,
    end_minutes: int,
    details: Dict[str, Any] | None = None,
) -> ItineraryNode:
    if node_type not in {"spot", "transport", "meal", "waiting"}:
        raise ValueError(f"不支持的行程节点类型：{node_type}")
    if not name:
        raise ValueError("行程节点 name 不能为空")
    if start_minutes < 0 or end_minutes < start_minutes:
        raise ValueError("行程节点时间范围无效")
    return {
        "type": node_type,
        "name": name,
        "start_minutes": start_minutes,
        "end_minutes": end_minutes,
        "duration_minutes": end_minutes - start_minutes,
        "details": details or {},
    }


def node_time_period(node: ItineraryNode) -> str:
    return f"{format_minutes(node['start_minutes'])}-{format_minutes(node['end_minutes'])}"


def node_to_readable(node: ItineraryNode) -> Dict[str, str]:
    """Convert a canonical node to the concise user-facing representation."""
    return {"place": node["name"], "time_period": node_time_period(node)}
