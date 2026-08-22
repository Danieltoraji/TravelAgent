"""城际交通表：出发地 → 目的地 的时长与方式（模拟数据，跨城市查表）。

与 `spots_graph.json`（市内景点/餐厅图）互补：市内交通查图，跨城市查本表。
后续「动态压缩」（城际占用当日游玩时长）将基于这里的 `transport_minutes` 计算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Tuple

DEFAULT_CITY_TRAVEL_PATH = (
    Path(__file__).resolve().parent.parent / "fake_spots" / "city_travel.json"
)


@dataclass(frozen=True)
class CityTravelEdge:
    origin: str
    destination: str
    transport_minutes: int
    mode: str = "城际交通"


def load_city_travel_edges(
    path=None,
) -> Dict[Tuple[str, str], CityTravelEdge]:
    """加载城际表，返回 {(origin, destination): CityTravelEdge}。文件缺失返回空表。"""
    travel_path = Path(path) if path is not None else DEFAULT_CITY_TRAVEL_PATH
    if not travel_path.exists():
        return {}
    data = json.loads(travel_path.read_text(encoding="utf-8"))
    edges: Dict[Tuple[str, str], CityTravelEdge] = {}
    for raw in data.get("edges", []):
        key = (raw["origin"], raw["destination"])
        edges[key] = CityTravelEdge(
            origin=raw["origin"],
            destination=raw["destination"],
            transport_minutes=int(raw["transport_minutes"]),
            mode=raw.get("mode", "城际交通"),
        )
    return edges


def find_city_travel(
    origin: str,
    destination: str,
    edges: Optional[Dict[Tuple[str, str], CityTravelEdge]] = None,
) -> Optional[CityTravelEdge]:
    """查 (origin, destination) 的城际交通；无此边返回 None。"""
    edges = edges if edges is not None else load_city_travel_edges()
    return edges.get((origin, destination))
