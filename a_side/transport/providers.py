"""Transport-time providers used by route planning."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Callable, Dict, Iterable, Optional, Tuple

try:
    from data_transmission.city_graph import DEFAULT_GRAPH_DIR, match_city_graph
except ModuleNotFoundError:
    from ..data_transmission.city_graph import DEFAULT_GRAPH_DIR, match_city_graph


@dataclass(frozen=True)
class TravelEdge:
    origin_id: str
    destination_id: str
    distance_km: float
    transport_minutes: int


class TravelTimeMatrix:
    """A route-local matrix containing only requested attraction pairs."""

    def __init__(self, spot_ids: Iterable[str], edges: Dict[Tuple[str, str], TravelEdge]):
        self.spot_ids = frozenset(str(spot_id) for spot_id in spot_ids)
        self._edges = dict(edges)

    @property
    def pair_count(self) -> int:
        return len(self._edges) // 2

    def get(self, origin_id: str, destination_id: str) -> TravelEdge:
        origin_id, destination_id = str(origin_id), str(destination_id)
        if origin_id == destination_id:
            return TravelEdge(origin_id, destination_id, 0.0, 0)
        try:
            return self._edges[(origin_id, destination_id)]
        except KeyError as exc:
            raise ValueError(
                f"候选交通矩阵缺少 {origin_id} -> {destination_id}"
            ) from exc


class TravelTimeProvider(ABC):
    """Abstract source of travel time and distance between attractions."""

    source_name = "unknown"

    @abstractmethod
    def get_edge(self, origin_id: str, destination_id: str) -> TravelEdge:
        raise NotImplementedError

    def get_travel_minutes(self, origin_id: str, destination_id: str) -> int:
        return self.get_edge(origin_id, destination_id).transport_minutes

    def get_matrix(self, spot_ids: Iterable[str]) -> TravelTimeMatrix:
        unique_ids = list(dict.fromkeys(str(spot_id) for spot_id in spot_ids))
        edges: Dict[Tuple[str, str], TravelEdge] = {}
        for origin_id, destination_id in combinations(unique_ids, 2):
            forward = self.get_edge(origin_id, destination_id)
            reverse = self.get_edge(destination_id, origin_id)
            edges[(origin_id, destination_id)] = forward
            edges[(destination_id, origin_id)] = reverse
        return TravelTimeMatrix(unique_ids, edges)


class JsonTravelTimeProvider(TravelTimeProvider):
    """Travel-time provider backed by one city's current mock JSON graph."""

    source_name = "city_json_graph"

    def __init__(self, city: str, graph_dir: Path = DEFAULT_GRAPH_DIR):
        self.city = city
        self.graph_path = match_city_graph(city, graph_dir)
        graph = json.loads(self.graph_path.read_text(encoding="utf-8"))
        self._edges: Dict[Tuple[str, str], TravelEdge] = {}
        for raw_edge in graph.get("edges", []):
            if "transport_minutes" not in raw_edge:
                raise ValueError(
                    f"图中的边 {raw_edge['start']} -> {raw_edge['end']} "
                    "缺少 transport_minutes"
                )
            start, end = str(raw_edge["start"]), str(raw_edge["end"])
            distance = float(raw_edge["distance"])
            minutes = int(raw_edge["transport_minutes"])
            self._edges[(start, end)] = TravelEdge(start, end, distance, minutes)
            self._edges[(end, start)] = TravelEdge(end, start, distance, minutes)

    def get_edge(self, origin_id: str, destination_id: str) -> TravelEdge:
        origin_id, destination_id = str(origin_id), str(destination_id)
        if origin_id == destination_id:
            return TravelEdge(origin_id, destination_id, 0.0, 0)
        try:
            return self._edges[(origin_id, destination_id)]
        except KeyError as exc:
            raise ValueError(
                f"{self.graph_path.name} 中缺少 {origin_id} -> {destination_id}"
            ) from exc


class LiveTravelTimeProvider(TravelTimeProvider):
    """真实地图 ETA 驱动的交通提供器（消费 B 的 map 工具；工具未就绪时由上层回退假源）。

    - ``eta_fn(origin_name, destination_name) -> (distance_km, transport_minutes)``，
      由 ``data_transmission.live_data.make_live_eta_fn`` 生产（B 侧 map 工具封装）；
    - id → 点名映射 ``name_by_id``（可用 ``set_name_map`` 增量补充）决定 ETA 调用用什么
      地名；未知 id 直接用 id 本身（地图查不到时 eta_fn 会抛 LiveDataError 交给上层回退）；
    - 结果按 (origin, destination) **双向缓存**——``get_matrix`` 对每对无序节点只产生
      两次 ``get_edge`` 调用，第二次命中缓存；B3（批量 ETA）落地后在此加批量路径，契约不变。
    """

    source_name = "live_map_api"

    def __init__(
        self,
        eta_fn: Callable[[str, str], Tuple[float, int]],
        name_by_id: Optional[Dict[str, str]] = None,
    ):
        self._eta_fn = eta_fn
        self._name_by_id: Dict[str, str] = dict(name_by_id or {})
        self._cache: Dict[Tuple[str, str], TravelEdge] = {}

    def set_name_map(self, name_by_id: Dict[str, str]) -> None:
        """增量补充 id → 点名映射（如从候选池/餐厅列表构建）。"""
        self._name_by_id.update(name_by_id or {})

    def _display_name(self, node_id: str) -> str:
        return self._name_by_id.get(str(node_id), str(node_id))

    def get_edge(self, origin_id: str, destination_id: str) -> TravelEdge:
        origin_id, destination_id = str(origin_id), str(destination_id)
        if origin_id == destination_id:
            return TravelEdge(origin_id, destination_id, 0.0, 0)
        cached = self._cache.get((origin_id, destination_id))
        if cached is not None:
            return cached
        if (
            origin_id not in self._name_by_id
            or destination_id not in self._name_by_id
        ):
            # 无名称映射的节点（如本地假数据里的餐厅 id）不发真实地图请求：
            # 抛 ValueError 由 `RestaurantResolver.travel_edge` 等调用方降级为
            # 0 通勤（餐厅边），不打断 live 主链路。
            raise ValueError(
                f"live 地图缺少节点名称映射：{origin_id} / {destination_id}"
            )
        distance_km, minutes = self._eta_fn(
            self._display_name(origin_id), self._display_name(destination_id)
        )
        edge = TravelEdge(
            origin_id,
            destination_id,
            float(distance_km),
            int(round(minutes)),
        )
        self._cache[(origin_id, destination_id)] = edge
        self._cache[(destination_id, origin_id)] = TravelEdge(
            destination_id, origin_id, float(distance_km), int(round(minutes))
        )
        return edge
