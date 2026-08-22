"""Resolve a destination city to its local mock-data directory and files."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


DEFAULT_CITY_DATA_DIR = Path(__file__).resolve().parent.parent / "fake_spots"
# Kept as an alias because route and transport callers already expose
# ``graph_dir`` as an override parameter.
DEFAULT_GRAPH_DIR = DEFAULT_CITY_DATA_DIR

CITY_DIRECTORY_ALIASES = {
    "北京": "beijing",
    "上海": "shanghai",
}


def normalize_city_name(city: str) -> str:
    if not isinstance(city, str) or not city.strip():
        raise ValueError("城市名称不能为空")
    normalized = city.strip()
    for suffix in ("特别行政区", "自治州", "自治区", "市"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return normalized


def match_city_data_dir(city: str, data_dir: Optional[Path] = None) -> Path:
    """Find the directory whose JSON files declare the requested city."""
    target_city = normalize_city_name(city)
    root = Path(data_dir) if data_dir is not None else DEFAULT_CITY_DATA_DIR
    if not root.is_dir():
        raise ValueError(f"城市数据目录不存在：{root}")

    # Accept either the common fake_spots root or one concrete city directory.
    direct_graph = root / "spots_graph.json"
    direct_spots = root / "spots.json"
    if direct_graph.is_file() and direct_spots.is_file():
        try:
            graph = json.loads(direct_graph.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取城市地图：{direct_graph}") from exc
        if normalize_city_name(graph.get("city", "")) == target_city:
            return root

    alias = CITY_DIRECTORY_ALIASES.get(target_city)
    if alias:
        candidate = root / alias
        graph_path = candidate / "spots_graph.json"
        spots_path = candidate / "spots.json"
        if graph_path.is_file() and spots_path.is_file():
            return candidate

    matches = []
    for directory in sorted(path for path in root.iterdir() if path.is_dir()):
        path = directory / "spots_graph.json"
        if not path.is_file() or not (directory / "spots.json").is_file():
            continue
        try:
            graph = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"无法读取城市地图：{path}") from exc
        graph_city = graph.get("city")
        if graph_city and normalize_city_name(graph_city) == target_city:
            matches.append(directory)

    if not matches:
        available = []
        for directory in sorted(path for path in root.iterdir() if path.is_dir()):
            path = directory / "spots_graph.json"
            if not path.is_file():
                continue
            try:
                available.append(
                    json.loads(path.read_text(encoding="utf-8")).get("city", path.stem)
                )
            except (OSError, json.JSONDecodeError):
                continue
        raise ValueError(
            f"未找到城市“{city}”对应的地图；当前可用城市：{', '.join(available) or '无'}"
        )
    if len(matches) > 1:
        raise ValueError(f"城市“{city}”匹配到多个数据目录：{matches}")
    return matches[0]


def match_city_graph(city: str, graph_dir: Optional[Path] = None) -> Path:
    """Return one city's ``spots_graph.json`` path."""
    return match_city_data_dir(city, graph_dir) / "spots_graph.json"


def match_city_spots(city: str, data_dir: Optional[Path] = None) -> Path:
    """Return one city's ``spots.json`` path."""
    return match_city_data_dir(city, data_dir) / "spots.json"


def match_city_restaurants(city: str, data_dir: Optional[Path] = None) -> Path:
    """Return one city's ``restaurants.json`` path."""
    return match_city_data_dir(city, data_dir) / "restaurants.json"
