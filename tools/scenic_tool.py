"""景点 Tool：开放状态、排队、预约、营业时间（对应 Scenic Agent 的 API 封装）。

Mock 版（ScenicTool）：从 MockWorld 读取模拟数据，Demo 剧情用。
Live 版（ScenicToolLive）：调高德 POI 搜索 API，返回真实评分/地址/电话。

切换方式：build_registry() 按 settings.use_real_map_api 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from tools.base_tool import BaseTool
from tools.mock_data import MockWorld

logger = logging.getLogger("tools.scenic")


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


class ScenicToolLive(ScenicTool):
    """高德 POI 搜索 API 实现版。

    调用链路：
      1. search_poi(place, city="北京") → 获取景点 POI 信息
      2. 从 POI 提取 rating（评分）、address（地址）、tel（电话）

    局限：
      - 排队时间无公开 API，从 MockWorld 取（Demo 剧情关键变量）
      - 营业时间从 v5 API opentime_today 获取，API 无数据时 fallback MockWorld
      - 票价高德无此字段，从 MockWorld 取或默认 0

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    source = "live"

    def __init__(self, client: Any, world: Optional[MockWorld] = None) -> None:
        """初始化 Live 版景点 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
            world: MockWorld 实例（用于排队/票价等无 API 字段的 fallback）
        """
        super().__init__(world)
        self._client = client

    def _run(self, place: str = "") -> Dict[str, Any]:
        pois = self._client.search_poi(place, city="北京", limit=1)
        if not pois:
            raise ValueError(f"未找到景点: {place}")
        poi = pois[0]

        # 从 MockWorld 获取排队/票价（无公开 API）
        info = self._world.get_place(place)
        queue_min = info["queue_min"] if info else 20
        ticket_required = info["ticket"] if info else True
        price = info["price"] if info else 0.0

        # 营业时间：优先用 v5 API 返回的 opentime_today，空时 fallback MockWorld
        open_hours = poi.get("opentime_today", "") or (info["open"] if info else "")

        logger.info(
            "Scenic: %s → rating=%s, opentime=%s, address=%s",
            place, poi.get("rating", 0), open_hours, poi.get("address", ""),
        )

        return {
            "place": place,
            "open": True,               # 高德基础 API 无法判断是否开放
            "queue_min": queue_min,      # 无公开 API，从 MockWorld 取
            "ticket_required": ticket_required,
            "open_hours": open_hours,    # v5 API opentime_today，fallback MockWorld
            "price": price,
        }
