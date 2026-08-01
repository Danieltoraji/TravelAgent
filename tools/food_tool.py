"""餐饮 Tool：评分、价格、营业状态、距离（对应 Food Agent 的 API 封装）。"""

from __future__ import annotations

from typing import Any, Dict, List

from tools.base_tool import BaseTool


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
