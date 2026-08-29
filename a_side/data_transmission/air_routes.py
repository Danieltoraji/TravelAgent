"""航空连通性拓扑（城际交通分阶段优化方案 §3.1/§4.9，Day 1）。

只回答「某航季/每周，城市/机场 A 是否通常存在直飞 B 的服务」，**不保存实时票价、
余票和状态**（慢变化字段仅作候选排序辅助，见 ``AirRouteHint``）。铁路不走此拓扑
（免费按需查询，见 §3.2）。航班付费验证只作用于拓扑确认过的航空边（§3.4）。

数据：``air_routes.json``（展示集：锦州→常州 等长期航线 + 南京→阿勒泰/郑州→长白山
等季节性/临时航线，用于演示班期/有效期字段）。

用法：:

    routes = load_air_routes()
    routes.out_cities("锦州")            # 锦州可直飞的城市
    routes.in_cities("上海")             # 直飞上海的城市
    routes.has("锦州", "常州")           # 是否存在直飞边
    routes.hint("锦州", "常州", "2026-09-05")  # 按日期班期过滤后的边（可能为 None）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import date
from typing import Dict, List, Optional, Tuple

DEFAULT_AIR_ROUTES_PATH = os.path.join(os.path.dirname(__file__), "air_routes.json")

# 星期号：ISO 8601，1=周一 .. 7=周日


@dataclass(frozen=True)
class AirRouteHint:
    """一条有向直飞航线提示（拓扑「慢变量」，非实时报价）。

    ``source`` 语义（§3.1 入边原则）：``schedule_catalog`` 航季目录确认存在的航线
    （可作确定事实）；``seasonal`` 季节性/临时航线（班期与有效期字段必须精确）；
    ``trial`` 待验证航线（真实无直飞或班期待核，如 西宁↔长春、银川↔福州）——
    拓扑只作"提名"，航班真源验证为空即淘汰，**不得作为确定事实展示**（§7.4）。
    """

    origin_city: str
    destination_city: str
    origin_airport: str
    destination_airport: str
    operating_days: Tuple[int, ...] = ()  # 1=周一..7=周日；空/全=每天
    valid_from: str = ""  # YYYY-MM-DD，含
    valid_to: str = ""  # YYYY-MM-DD，含
    typical_duration_min: int = 0
    frequency_per_week: int = 0
    source: str = "schedule_catalog"
    updated_at: str = ""

    def operates_on(self, date_str: str) -> bool:
        """给定 ``YYYY-MM-DD``（可为空=不问日期）该航线是否在班期/有效期内。

        只回答"通常是否运营"，不保证当日有班次/余票（那属于付费真源验证，
        §3.4）。空 ``operating_days`` 视为每天运营。
        """
        if not date_str:
            return True
        d = date.fromisoformat(date_str)
        if self.valid_from and date.fromisoformat(self.valid_from) > d:
            return False
        if self.valid_to and date.fromisoformat(self.valid_to) < d:
            return False
        if self.operating_days and d.isoweekday() not in self.operating_days:
            return False
        return True

    def to_dict(self) -> dict:
        return {
            "origin_city": self.origin_city,
            "destination_city": self.destination_city,
            "origin_airport": self.origin_airport,
            "destination_airport": self.destination_airport,
            "operating_days": list(self.operating_days),
            "valid_from": self.valid_from,
            "valid_to": self.valid_to,
            "typical_duration_min": self.typical_duration_min,
            "frequency_per_week": self.frequency_per_week,
            "source": self.source,
            "updated_at": self.updated_at,
        }


class AirRoutes:
    """有向直飞拓扑：正向（``out``）/反向（``in``）城市邻接 + 班期过滤。"""

    def __init__(self, hints: List[AirRouteHint]):
        self.hints = hints
        self._out: Dict[str, List[AirRouteHint]] = {}
        self._in: Dict[str, List[AirRouteHint]] = {}
        for h in hints:
            self._out.setdefault(h.origin_city, []).append(h)
            self._in.setdefault(h.destination_city, []).append(h)

    # -- 正向：从某城可直飞 ------------------------------------------------
    def out(self, city: str, date_str: str = "") -> List[AirRouteHint]:
        """某城可直飞的边（可按日期班期过滤）；未知城市 → []。"""
        return [h for h in self._out.get(city, ()) if h.operates_on(date_str)]

    def out_cities(self, city: str, date_str: str = "") -> List[str]:
        """某城可直飞的城市列表（保持数据文件顺序）。"""
        return [h.destination_city for h in self.out(city, date_str)]

    # -- 反向：可直飞到某城 ------------------------------------------------
    def in_cities(self, city: str, date_str: str = "") -> List[str]:
        """可直飞某城的出发城市列表。"""
        return [
            h.origin_city
            for h in self._in.get(city, ())
            if h.operates_on(date_str)
        ]

    # -- 单边查询 ----------------------------------------------------------
    def hint(self, origin: str, destination: str,
             date_str: str = "") -> Optional[AirRouteHint]:
        """指定有向城市对 + 日期的航线提示；无（或班期/有效期不符）→ None。"""
        for h in self._out.get(origin, ()):
            if h.destination_city == destination and h.operates_on(date_str):
                return h
        return None

    def has(self, origin: str, destination: str,
            date_str: str = "") -> bool:
        return self.hint(origin, destination, date_str) is not None


def load_air_routes(path: str = DEFAULT_AIR_ROUTES_PATH) -> AirRoutes:
    """加载 ``air_routes.json``（数组或 {\"routes\": [...]} 两种结构兼容）。"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    raw = data.get("routes") if isinstance(data, dict) else data
    hints = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        hints.append(AirRouteHint(
            origin_city=item["origin_city"],
            destination_city=item["destination_city"],
            origin_airport=item.get("origin_airport", ""),
            destination_airport=item.get("destination_airport", ""),
            operating_days=tuple(item.get("operating_days") or ()),
            valid_from=item.get("valid_from", ""),
            valid_to=item.get("valid_to", ""),
            typical_duration_min=int(item.get("typical_duration_min", 0) or 0),
            frequency_per_week=int(item.get("frequency_per_week", 0) or 0),
            source=item.get("source", "schedule_catalog"),
            updated_at=item.get("updated_at", ""),
        ))
    return AirRoutes(hints)