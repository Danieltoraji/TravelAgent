"""城际交通表：出发地 → 目的地 的时长与方式（模拟数据，跨城市查表）。

与 `spots_graph.json`（市内景点/餐厅图）互补：市内交通查图，跨城市查本表。

批次 2（8.28）：估算表升级车站粒度（stations 表 + options 站点对）——
- ``CityTravelEdge`` 扩展 ``cost_per_person / from_station / to_station / source``
  （source: "" 假表旧边 / "estimate" 估算表条目 / "live" 真源）；
- ``load_city_travel_options`` 返回 ``{(o,d): {mode: Edge}}`` 全 options；
  ``load_city_travel_edges`` 取 options 默认边（train 优先；旧表无 options 回退
  顶层字段）保持旧读法兼容；
- ``find_city_travel_preferred`` 按偏好链（高铁→飞机→自驾）逐个方式查——对应
  用户 8.28 建议「先定城际方式」，再规划市内衔接（阶段三 legs）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

DEFAULT_CITY_TRAVEL_PATH = (
    Path(__file__).resolve().parent.parent / "fake_spots" / "city_travel.json"
)

# 城际方式偏好（来去程段生成时的选择顺序：高铁优先，逐项回退到命中为止）
MODE_PREFERENCE: Tuple[str, ...] = ("train", "air", "driving")

# mode → 中文方式名（段展示用）
_MODE_TEXT: Dict[str, str] = {
    "train": "高铁",
    "air": "飞机",
    "driving": "自驾",
}


def mode_text(mode: str) -> str:
    """城际方式中文名（train/air/driving；其它原样返回）。"""
    return _MODE_TEXT.get(mode, mode)


@dataclass(frozen=True)
class CityTravelEdge:
    origin: str
    destination: str
    transport_minutes: int
    mode: str = "城际交通"
    cost_per_person: float = 0.0     # 人均费用（元；估算/真源票价口径）
    from_station: str = ""           # 出发站（train/air；driving 无站点为空）
    to_station: str = ""             # 到达站（train/air）
    source: str = ""                 # "estimate"（估算表）| "live"（真源）| ""（假表旧边）


def _load_raw_edges(path: Optional[Any] = None) -> list:
    """读表原始 ``edges`` 列表；文件缺失 / 损坏 → []（不报错）。"""
    travel_path = Path(path) if path is not None else DEFAULT_CITY_TRAVEL_PATH
    if not travel_path.exists():
        return []
    try:
        data = json.loads(travel_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data.get("edges", []) or []


def _edge_from_raw(raw: Dict[str, Any]) -> CityTravelEdge:
    """旧表顶层字段 → Edge（mode 为顶层方式名，如「高铁」）。"""
    return CityTravelEdge(
        origin=raw["origin"],
        destination=raw["destination"],
        transport_minutes=int(raw["transport_minutes"]),
        mode=raw.get("mode", "城际交通"),
    )


def _edge_from_option(
    opt: Dict[str, Any],
    origin: str,
    destination: str,
    mode: str,
    default_edge: CityTravelEdge,
) -> CityTravelEdge:
    """options 条目 → Edge（缺字段回退默认边，保持旧读法兼容）。"""
    return CityTravelEdge(
        origin=origin,
        destination=destination,
        transport_minutes=int(
            opt.get("transport_minutes") or default_edge.transport_minutes
        ),
        mode=mode,
        cost_per_person=float(opt.get("cost_per_person") or 0.0),
        from_station=str(opt.get("from_station") or ""),
        to_station=str(opt.get("to_station") or ""),
        source=str(opt.get("source") or "estimate"),
    )


def load_city_travel_options(
    path: Optional[Any] = None,
) -> Dict[Tuple[str, str], Dict[str, CityTravelEdge]]:
    """完整 options 表：``{(origin, dest): {mode: CityTravelEdge}}``。

    旧表（无 ``options``）合成单元素 dict（顶层字段）；文件缺失返回空表。
    """
    out: Dict[Tuple[str, str], Dict[str, CityTravelEdge]] = {}
    for raw in _load_raw_edges(path):
        origin, destination = raw.get("origin", ""), raw.get("destination", "")
        if not origin or not destination:
            continue
        options = raw.get("options") or []
        by_mode: Dict[str, CityTravelEdge] = {}
        default_edge = _edge_from_raw(raw)
        if options:
            for opt in options:
                mode = str(opt.get("mode") or "").strip()
                if not mode:
                    continue
                by_mode[mode] = _edge_from_option(
                    opt, origin, destination, mode, default_edge
                )
        else:
            by_mode[default_edge.mode] = default_edge
        out[(origin, destination)] = by_mode
    return out


def load_city_travel_edges(
    path: Optional[Any] = None,
) -> Dict[Tuple[str, str], CityTravelEdge]:
    """默认边（options 的 train 条目优先，其次任意首条目；旧表用顶层字段）。

    返回 ``{(origin, destination): CityTravelEdge}``——兼容旧调用方
    （``find_city_travel`` 等）；文件缺失返回空表。
    """
    edges: Dict[Tuple[str, str], CityTravelEdge] = {}
    for key, by_mode in load_city_travel_options(path).items():
        first = by_mode.get("train") or next(iter(by_mode.values()), None)
        if first is not None:
            edges[key] = first
    return edges


def find_city_travel(
    origin: str,
    destination: str,
    edges: Optional[Dict[Tuple[str, str], CityTravelEdge]] = None,
    provider: Optional[Callable[[str, str], Optional[CityTravelEdge]]] = None,
) -> Optional[CityTravelEdge]:
    """查 (origin, destination) 的默认城际边；无此边返回 None。

    ``provider``：``fn(origin, dest) -> Optional[CityTravelEdge]``——真实数据接入时由
    ``data_transmission.live_data.make_live_city_travel_provider`` 注入，给定时优先于
    本地表（查不到再回落本地表，保持缺边语义一致）。
    """
    if provider is not None:
        edge = provider(origin, destination)
        if edge is not None:
            return edge
    edges = edges if edges is not None else load_city_travel_edges()
    return edges.get((origin, destination))


def find_city_travel_for_mode(
    origin: str,
    destination: str,
    mode: str,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
) -> Optional[CityTravelEdge]:
    """按指定方式查本地表 options 条目；无此边 / 无该方式 → None。"""
    options = options if options is not None else load_city_travel_options()
    by_mode = options.get((origin, destination)) or {}
    return by_mode.get(mode)


def find_city_travel_preferred(
    origin: str,
    destination: str,
    modes: Tuple[str, ...] = MODE_PREFERENCE,
    options: Optional[Dict[Tuple[str, str], Dict[str, CityTravelEdge]]] = None,
    provider: Optional[Callable[[str, str], Optional[CityTravelEdge]]] = None,
) -> Optional[CityTravelEdge]:
    """按偏好链逐个方式查，返回第一个命中（先定城际方式）。

    - ``provider`` 非空：直接单调用（live map 工具内部自带
      train→表外→driving 真源的降级，无需 A 侧重试链）；
    - 否则本地 options：高铁 → 飞机 → 自驾 逐个方式查。
    """
    if provider is not None:
        return provider(origin, destination)
    options = options if options is not None else load_city_travel_options()
    by_mode = options.get((origin, destination)) or {}
    for mode in modes:
        if mode in by_mode:
            return by_mode[mode]
    return None