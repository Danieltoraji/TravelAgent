"""酒店数据模型与加载：读取一个城市的 ``hotel.json``。

与餐厅（``restaurant.py``）的形态对齐：id / 名称 / 位置 / 每晚价格 / 评分 /
星级 / 标签 / 附近景点。每晚价格取该酒店最便宜房型的价格（规划按保守口径）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .city_graph import DEFAULT_GRAPH_DIR, match_city_data_dir


@dataclass(frozen=True)
class Hotel:
    id: str
    name: str
    location: Tuple[float, float]
    price_per_night: float
    rating: float
    star: int
    tags: Tuple[str, ...]
    nearby_spot_ids: Tuple[str, ...]


def _flatten_tags(tags: object) -> Tuple[str, ...]:
    """把 tags 对象（dict 或 list）展平成标签字符串元组。"""
    if isinstance(tags, dict):
        values: List[object] = []
        for group in tags.values():
            if isinstance(group, (list, tuple)):
                values.extend(group)
            elif group is not None:
                values.append(group)
        return tuple(str(value) for value in values)
    if isinstance(tags, (list, tuple)):
        return tuple(str(value) for value in tags)
    return ()


def load_hotels(city: str, data_dir: Optional[Path] = None) -> List[Hotel]:
    """读取指定城市的酒店列表；文件不存在或解析失败返回空列表。"""
    try:
        directory = match_city_data_dir(city, data_dir or DEFAULT_GRAPH_DIR)
        path = directory / "hotel.json"
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []

    hotels: List[Hotel] = []
    for item in raw.get("hotels", []):
        try:
            location = item.get("location", {}) or {}
            room_types = item.get("room_types") or []
            prices = [
                float(room.get("price", 0))
                for room in room_types
                if isinstance(room, dict) and room.get("price") is not None
            ]
            night_price = min(prices) if prices else 0.0
            rating_value = float(item.get("rating", 0))
            star_value = int(item.get("star") or 0) or (
                5 if rating_value >= 4.7 else 4 if rating_value >= 4.4 else 3
            )
            hotels.append(
                Hotel(
                    id=str(item["id"]),
                    name=str(item.get("name", item["id"])),
                    location=(
                        float(location.get("lat", 0.0)),
                        float(location.get("lng", 0.0)),
                    ),
                    price_per_night=night_price,
                    rating=rating_value,
                    star=star_value,
                    tags=_flatten_tags(item.get("tags")),
                    nearby_spot_ids=tuple(item.get("nearby_spot_ids", [])),
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return hotels