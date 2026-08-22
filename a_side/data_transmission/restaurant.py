"""餐厅数据模型与加载：读取一个城市的 ``restaurants.json``。"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .city_graph import DEFAULT_GRAPH_DIR, match_city_restaurants


@dataclass(frozen=True)
class Restaurant:
    id: str
    name: str
    location: Tuple[float, float]
    cuisine_tags: Tuple[str, ...]
    signature_tags: Tuple[str, ...]
    average_cost: float
    nearby_spot_ids: Tuple[str, ...]


def load_restaurants(
    city: str, data_dir: Optional[Path] = None
) -> List[Restaurant]:
    """读取指定城市的餐厅列表；文件不存在时返回空列表。"""
    try:
        path = match_city_restaurants(city, data_dir or DEFAULT_GRAPH_DIR)
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []

    restaurants: List[Restaurant] = []
    for item in raw.get("restaurants", []):
        try:
            tags = item.get("tags", {}) or {}
            location = item.get("location", {}) or {}
            restaurants.append(
                Restaurant(
                    id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    location=(
                        float(location.get("lat", 0.0)),
                        float(location.get("lng", 0.0)),
                    ),
                    cuisine_tags=tuple(tags.get("cuisine", [])),
                    signature_tags=tuple(tags.get("signature", [])),
                    average_cost=float(item.get("average_cost", 0)),
                    nearby_spot_ids=tuple(item.get("nearby_spot_ids", [])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return restaurants
