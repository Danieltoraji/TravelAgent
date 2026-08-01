"""景点 Tool：开放状态、排队、预约、营业时间（对应 Scenic Agent 的 API 封装）。"""

from __future__ import annotations

from typing import Any, Dict, Optional

from tools.base_tool import BaseTool
from tools.mock_data import MockWorld


class ScenicTool(BaseTool):
    name = "scenic"
    description = "景点实时状态：是否开放、预计排队分钟数、是否需要预约、营业时间、票价。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "place": {"type": "string", "description": "景点名称"},
        },
        "required": ["place"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, place: str = "") -> Dict[str, Any]:
        info = self._world.get_place(place)
        if info is None:
            raise ValueError(f"Unknown place: {place}")
        return {
            "place": place,
            "open": True,
            "queue_min": info["queue_min"],
            "ticket_required": info["ticket"],
            "open_hours": info["open"],
            "price": info["price"],
        }
