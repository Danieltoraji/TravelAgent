"""交通 Tool：公交 / 地铁 / 打车预计耗时与拥堵（对应 Traffic Agent 的 API 封装）。"""

from __future__ import annotations

from typing import Any, Dict

from tools.base_tool import BaseTool


class TrafficTool(BaseTool):
    name = "traffic"
    description = "交通状态：公交/地铁/打车预计耗时与拥堵程度。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "mode": {"enum": ["transit", "taxi", "walk"]},
        },
        "required": ["origin", "destination"],
    }

    def _run(self, origin: str = "", destination: str = "", mode: str = "transit") -> Dict[str, Any]:
        # Mock：固定畅通状态；真实接入后可返回 delay_min 变化以触发剧情
        return {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "duration_min": 30,
            "congestion": "畅通",
            "delay_min": 0,
            "note": "地铁1号线运行正常",
        }
