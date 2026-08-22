"""Transport-time providers used by route planning."""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from dataclasses import dataclass
from itertools import combinations
from pathlib import Path
from typing import Dict, Iterable, Tuple

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
