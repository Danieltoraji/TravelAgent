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
    domain = "food"
    description = "餐厅推荐：评分、人均价格、营业状态、距离。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "关键词，如菜系"},
            "near": {"type": "string", "description": "附近地点（文字地名，需地理编码）"},
            "location": {"type": "string", "description": "锚点坐标 \"lng,lat\"（直连周边搜索，免地理编码；优先于 near）"},
            "radius": {"type": "integer", "description": "周边搜索半径（米），默认 2000"},
            "city": {"type": "string", "description": "搜索城市（无 near 时按城市搜索）"},
            "limit": {"type": "integer", "description": "返回数量上限"},
        },
    }

    def _run(self, query: str = "", near: str = "", city: str = "", limit: int = 5) -> List[Dict[str, Any]]:
        # Mock：固定餐厅列表；真实接入点评/美团后替换
        # location 为 "lng,lat" 串（与 FoodToolLive 输出同构，C2）——
        # A 侧 live_data normalize 无坐标即丢弃餐厅，Mock 必须带坐标才能参与通勤
        return [
            {"name": "全聚德(前门店)", "rating": 4.6, "price_per_person": 180,
             "location": "116.397,39.899",
             "open": True, "distance_km": 0.8, "cuisine": "京菜", "queue_min": 40,
             "open_hours": "10:00-22:00", "specialty": "烤鸭", "address": "前门大街", "tel": "010-65112418"},
            {"name": "护国寺小吃", "rating": 4.3, "price_per_person": 45,
             "location": "116.383,39.933",
             "open": True, "distance_km": 0.5, "cuisine": "小吃", "queue_min": 10,
             "open_hours": "06:00-20:00", "specialty": "", "address": "护国寺大街", "tel": ""},
        ]


class FoodToolLive(FoodTool):
    """高德 POI 搜索 API 实现版。

    调用链路：
      - 有 location 参数：search_poi_around(coord, types="050000", radius)（坐标直连，免 geocode）
      - 有 near 参数：geocode(near) → search_poi_around(coord, types="050000", radius=1000)
      - 无位置参数：search_poi(query or "餐厅", city="北京")

    从 POI biz_ext 提取：
      - rating: 评分
      - cost: 人均消费
      - tag: 特色菜（如"烤鱼,麻辣香锅"）

    局限：
      - 营业状态无公开 API，默认 True
      - 排队时间无公开 API，固定 0
      - cuisine 从 POI type 字段推断（如"中式快餐"→"快餐"）

    8.31 P0（锚点附近搜索）：新增 ``location``（"lng,lat" 坐标串，A 侧
    附近餐厅池用）与 ``radius``（米，默认 2000）参数——与 ``near``（文字地名，
    需 geocode）互斥；location 优先。返回结构不变，调用方零改动。

    返回与 Mock 版完全相同的 list[dict] 结构，调用方零改动。
    """

    source = "live"
    DEFAULT_RADIUS = 2000

    def __init__(self, client: Any) -> None:
        """初始化 Live 版餐饮 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
        """
        super().__init__()
        self._client = client

    def _run(
        self,
        query: str = "",
        near: str = "",
        city: str = "",
        limit: int = 5,
        location: str = "",
        radius: int = 0,
    ) -> List[Dict[str, Any]]:
        if location:
            # 8.31：坐标直连周边搜索（A 侧 nearby_pool 用；免 geocode）。
            coord = self._parse_location(location)
            if coord is None:
                raise ValueError(f"location 坐标格式非法: {location}")
            pois = self._client.search_poi_around(
                coord,
                types="050000",
                radius=radius or self.DEFAULT_RADIUS,
                keywords=query or "",
                limit=limit,
            )
        elif near:
            # 有位置参数：先地理编码，再周边搜索餐饮
            coord = self._client.geocode(near)
            if coord is None:
                raise ValueError(f"无法定位: {near}")
            pois = self._client.search_poi_around(
                coord, types="050000", radius=1000,
                keywords=query or "", limit=limit,
            )
        else:
            # 无位置参数：城市内搜索（8.28：city 参数化，原硬编码"北京"）
            pois = self._client.search_poi(
                query or "餐厅", city=city or "北京", limit=limit,
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

            # 8.28：坐标——_normalize_poi 输出拆为 lat/lng 两字段（无 location 键）；
            # 兼容个别带 location（"lng,lat"）的来源。无坐标则留空（A 侧丢弃该餐厅）
            location = (
                poi.get("location")
                or (
                    f"{poi.get('lng')},{poi.get('lat')}"
                    if poi.get("lat") or poi.get("lng")
                    else ""
                )
            )

            results.append({
                "name": poi.get("name", ""),
                "location": location,   # "lng,lat" 坐标（真源通勤用）
                "rating": poi.get("rating", 0),
                "price_per_person": poi.get("cost", 0),
                "open": True,               # 无公开 API
                "distance_km": round(distance_km, 2),
                "cuisine": cuisine,
                "queue_min": 0,              # 无公开 API
                "open_hours": poi.get("opentime_today", ""),
                "specialty": poi.get("tag", ""),
                "address": poi.get("address", ""),
                "tel": poi.get("tel", ""),
            })

        logger.info(
            "Food: query=%s near=%s → %d results, opentime=%s",
            query, near, len(results), results[0].get("opentime_today", "") if results else "",
        )
        return results

    @staticmethod
    def _parse_location(location: str) -> Optional[Tuple[float, float]]:
        """解析 "lng,lat" 坐标串 → (lat, lng)；非法 → None。"""
        parts = str(location or "").split(",")
        if len(parts) != 2:
            return None
        try:
            lng, lat = float(parts[0]), float(parts[1])
        except ValueError:
            return None
        if not lng and not lat:
            return None
        return (lat, lng)

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
