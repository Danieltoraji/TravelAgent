"""地图 Tool：POI 搜索 + 两点间路线（距离 / 预计耗时）。

对应地图 Agent 的 API 封装。真实接入高德地图后，仅需替换 _run 内部实现。
"""

from __future__ import annotations

from typing import Any, ClassVar

from tools.base_tool import BaseTool
from tools.mock_data import PLACES


class MapTool(BaseTool):
    name = "map"
    description = "地图服务：搜索景点位置、计算两点间路线距离与预计耗时。"
    source = "mock"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "enum": ["search_poi", "route"],
                "description": "search_poi 搜索地点；route 计算路线",
            },
            "query": {"type": "string", "description": "搜索关键词"},
            "origin": {"type": "string", "description": "起点"},
            "destination": {"type": "string", "description": "终点"},
        },
        "required": ["action"],
    }

    def _run(self, action: str = "search_poi", query: str = "",
             origin: str = "", destination: str = "", **kwargs: Any) -> Any:
        if action == "search_poi":
            return self._search(query)
        if action == "route":
            return self._route(origin, destination)
        raise ValueError(f"Unknown map action: {action}")

    def _search(self, query: str) -> list[dict[str, Any]]:
        q = (query or "").strip()
        results: list[dict[str, Any]] = []
        for name, info in PLACES.items():
            if q and q not in name:
                continue
            results.append({
                "name": name,
                "lat": info["lat"],
                "lng": info["lng"],
                "open": info["open"],
                "price": info["price"],
            })
        return results

    def _route(self, origin: str, destination: str) -> dict[str, Any]:
        # Mock：固定行程参数；真实接入高德后按 API 返回替换
        return {
            "from": origin,
            "to": destination,
            "distance_km": 3.5,
            "duration_min": 25,
            "transit": "地铁1号线 + 步行800m",
            "fare": 4.0,
        }
