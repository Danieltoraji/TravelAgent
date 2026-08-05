"""餐饮 Tool：评分、价格、营业状态、距离（对应 Food Agent 的 API 封装）。

Mock 版（FoodTool）：返回固定餐厅列表，Demo 用。
Live 版（FoodToolLive）：调高德 POI 搜索 API，返回真实评分/人均消费/特色菜。

切换方式：build_registry() 按 settings.use_real_map_api 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from tools.base_tool import BaseTool

logger = logging.getLogger("tools.food")


class FoodTool(BaseTool):
    name = "food"
    description = "餐厅推荐：评分、人均价格、营业状态、距离。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，如菜系"},
            "near": {"type": "string", "description": "附近地点"},
        },
    }

    def _run(self, query: str = "", near: str = "") -> List[Dict[str, Any]]:
        # Mock：固定餐厅列表；真实接入点评/美团后替换
        return [
            {"name": "全聚德(前门店)", "rating": 4.6, "price_per_person": 180,
             "open": True, "distance_km": 0.8, "cuisine": "京菜", "queue_min": 40},
            {"name": "护国寺小吃", "rating": 4.3, "price_per_person": 45,
             "open": True, "distance_km": 0.5, "cuisine": "小吃", "queue_min": 10},
        ]


class FoodToolLive(FoodTool):
    """高德 POI 搜索 API 实现版。

    调用链路：
      - 有 near 参数：geocode(near) → search_poi_around(coord, types="050000", radius=1000)
      - 无 near 参数：search_poi(query or "餐厅", city="北京")

    从 POI biz_ext 提取：
      - rating: 评分
      - cost: 人均消费
      - tag: 特色菜（如"烤鱼,麻辣香锅"）

    局限：
      - 营业状态无公开 API，默认 True
      - 排队时间无公开 API，固定 0
      - cuisine 从 POI type 字段推断（如"中式快餐"→"快餐"）

    返回与 Mock 版完全相同的 list[dict] 结构，调用方零改动。
    """

    source = "live"

    def __init__(self, client: Any) -> None:
        """初始化 Live 版餐饮 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
        """
        super().__init__()
        self._client = client

    def _run(self, query: str = "", near: str = "") -> List[Dict[str, Any]]:
        if near:
            # 有位置参数：先地理编码，再周边搜索餐饮
            coord = self._client.geocode(near)
            if coord is None:
                raise ValueError(f"无法定位: {near}")
            pois = self._client.search_poi_around(
                coord, types="050000", radius=1000,
                keywords=query or "", limit=5,
            )
        else:
            # 无位置参数：城市内搜索
            pois = self._client.search_poi(
                query or "餐厅", city="北京", limit=5,
            )

        if not pois:
            return []

        results: List[Dict[str, Any]] = []
        for poi in pois:
            # 从 POI type 字段推断菜系（如"中式快餐;餐饮"→"中式快餐"）
            cuisine = self._extract_cuisine(poi.get("type", ""))

            # distance_km：search_poi_around 返回 distance 字段（米），search_poi 无此字段
            distance_m = poi.get("distance", 0)
            distance_km = float(distance_m) / 1000.0 if distance_m else 0.0

            results.append({
                "name": poi.get("name", ""),
                "rating": poi.get("rating", 0),
                "price_per_person": poi.get("cost", 0),
                "open": True,               # 无公开 API
                "distance_km": round(distance_km, 2),
                "cuisine": cuisine,
                "queue_min": 0,              # 无公开 API
            })

        logger.info(
            "Food: query=%s near=%s → %d results, opentime=%s",
            query, near, len(results), results[0].get("opentime_today", "") if results else "",
        )
        return results

    @staticmethod
    def _extract_cuisine(type_str: str) -> str:
        """从高德 POI type 字段推断菜系。

        高德 type 格式如 "餐饮服务;中餐厅;清真菜馆"，
        取第二个分号后的部分作为菜系（跳过通用的"餐饮服务"前缀）。
        若无第二段则返回第一段。
        """
        if not type_str:
            return ""
        parts = [p.strip() for p in type_str.split(";") if p.strip()]
        if len(parts) >= 2:
            return parts[1]  # "中餐厅"、"清真菜馆" 等
        return parts[0] if parts else ""
