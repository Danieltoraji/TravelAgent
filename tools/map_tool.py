"""地图 Tool：POI 搜索 + 两点间路线（距离 / 预计耗时）。

对应地图 Agent 的 API 封装。

Mock 版（MapTool）：从 MockWorld 读取模拟数据，Demo 剧情用。
Live 版（MapToolLive）：调高德地图 API，返回真实 POI 和路线数据。

切换方式：build_registry() 按 settings.use_real_map_api 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from tools.base_tool import BaseTool
from tools.mock_data import PLACES

logger = logging.getLogger("tools.map")

# 路线模式 → 中文描述
_MODE_TEXT: Dict[str, str] = {
    "transit": "公交",
    "driving": "驾车",
    "riding": "骑行",
    "walk": "步行",
}


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
            "mode": {
                "enum": ["transit", "driving", "riding", "walk"],
                "description": "路线模式：公交/驾车/骑行/步行，默认 transit",
            },
        },
        "required": ["action"],
    }

    def _run(self, action: str = "search_poi", query: str = "",
             origin: str = "", destination: str = "", mode: str = "transit",
             **kwargs: Any) -> Any:
        if action == "search_poi":
            return self._search(query)
        if action == "route":
            return self._route(origin, destination, mode)
        raise ValueError(f"Unknown map action: {action}")

    def _search(self, query: str) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        results: List[Dict[str, Any]] = []
        for name, info in PLACES.items():
            if q and q not in name:
                continue
            results.append({
                "name": name,
                "lat": info["lat"],
                "lng": info["lng"],
                "open": info["open"],
                "price": info["price"],
                "rating": 0,
                "tel": "",
                "type": "",
            })
        return results

    def _route(self, origin: str, destination: str, mode: str = "transit") -> Dict[str, Any]:
        # Mock：固定行程参数；真实接入高德后按 API 返回替换
        return {
            "from": origin,
            "to": destination,
            "distance_km": 3.5,
            "duration_min": 25,
            "transit": "地铁1号线 + 步行800m",
            "fare": 4.0,
        }


class MapToolLive(MapTool):
    """高德地图 API 实现版。

    调用链路：
      1. search_poi → AmapClient.search_poi() → /v5/place/text
      2. route → AmapClient.geocode(origin/destination) 获取坐标
               → AmapClient.get_route() → /v3/direction/{mode}

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    name = "map"
    description = "地图服务：搜索景点位置、计算两点间路线距离与预计耗时。"
    source = "live"
    input_schema = MapTool.input_schema

    def __init__(self, client: Any) -> None:
        """初始化 Live 版地图 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
        """
        super().__init__()
        self._client = client

    def _search(self, query: str) -> List[Dict[str, Any]]:
        """调高德关键词搜索 API，返回标准化 POI 列表。"""
        pois = self._client.search_poi(query)
        return [
            {
                "name": p["name"],
                "lat": p["lat"],
                "lng": p["lng"],
                "open": p.get("opentime_today", ""),
                "price": p.get("cost", 0),
                "address": p.get("address", ""),
                "rating": p.get("rating", 0),
                "tel": p.get("tel", ""),
                "type": p.get("type", ""),
            }
            for p in pois
        ]

    def _route(self, origin: str, destination: str, mode: str = "transit") -> Dict[str, Any]:
        """调高德路线规划 API，返回距离和耗时。

        先地理编码获取起终点坐标，再调路线规划 API。
        地理编码时限定 city="北京"，避免同名地点歧义。
        """
        # 地理编码：地址 → 坐标（限定北京，避免同名歧义）
        origin_coord: Tuple[float, float] = self._client.geocode(origin, city="北京")
        dest_coord: Tuple[float, float] = self._client.geocode(destination, city="北京")

        # 路线规划
        route_data = self._client.get_route(origin_coord, dest_coord, mode=mode)
        distance_m = route_data["distance"]
        duration_s = route_data["duration"]

        # 票价：公交取 cost，驾车取 tolls，骑行/步行无票价
        if mode == "transit":
            fare = float(route_data.get("cost", 0))
        elif mode == "driving":
            fare = float(route_data.get("tolls", 0))
        else:
            fare = 0.0

        return {
            "from": origin,
            "to": destination,
            "distance_km": round(distance_m / 1000, 2),    # 米 → 公里
            "duration_min": round(duration_s / 60),          # 秒 → 分钟
            "transit": _MODE_TEXT.get(mode, mode),
            "fare": fare,
        }
