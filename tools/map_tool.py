"""地图 Tool：POI 搜索 + 两点间路线（距离 / 预计耗时）。

对应地图 Agent 的 API 封装。

Mock 版（MapTool）：从 MockWorld 读取模拟数据，Demo 剧情用。
Live 版（MapToolLive）：调高德地图 API，返回真实 POI 和路线数据。

切换方式：build_registry() 按 settings.use_real_map_api 自动选择。

城际模式（train / air，批次 1a）：
- mode=train/air → ``_intercity``：查估算表（B 仓库 ``fake_spots/city_travel.json`` 的
  ``options``），返回 ``{mode, duration_min, cost_per_person, source:"estimate"}``；
  表缺失 / 城市对未收录 → 自动回退 driving（Mock 固定值 / Live 高德驾车真源），不报错。
- 高德开放平台无城际火车/航班时刻票价接口（真源走 12306MCP / 航班聚合，阶段二接入），
  估算表只保级不冒充真数据（``source=estimate`` 标注）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Optional, Tuple

from tools.base_tool import BaseTool
from tools.mock_data import PLACES

logger = logging.getLogger("tools.map")

# 路线模式 → 中文描述
_MODE_TEXT: Dict[str, str] = {
    "transit": "公交",
    "driving": "驾车",
    "riding": "骑行",
    "walk": "步行",
    "train": "高铁",
    "air": "飞机",
}

# 城际模式：走估算/兜底管线，不调高德 get_route（高德无对应端点，透传会 ValueError）
_INTERCITY_MODES = ("train", "air")

# 城际估算表：B 仓库 fake_spots/city_travel.json（与 A 侧 fake_spots/city_travel.json 单一来源同步）
_CITY_TRAVEL_ESTIMATE_JSON = (
    Path(__file__).resolve().parent.parent / "fake_spots" / "city_travel.json"
)


def _lookup_intercity_estimate(
    origin: str, destination: str, mode: str
) -> Optional[Dict[str, Any]]:
    """查城际估算表：命中返回该 mode 的 options 条目，否则 None。

    表缺失 / 损坏 / 城市对未收录 → ``None``，由调用方回退 driving（不报错）。
    """
    try:
        raw = json.loads(_CITY_TRAVEL_ESTIMATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for edge in raw.get("edges", []):
        if edge.get("origin") == origin and edge.get("destination") == destination:
            for opt in edge.get("options", []):
                if opt.get("mode") == mode:
                    return opt
    return None


def _parse_coord(text: Any) -> Optional[Tuple[float, float]]:
    """"lng,lat" 坐标字符串 → ``(lat, lng)``（与 geocode 返回口径一致）；非坐标 → None。

    8.25 B 档：让 A 侧把 scenic 已返回的真实坐标直接喂给路线规划，跳过地理编码，
    避免怪名 POI（如"LV巨轮"）点名地理编码失败（高德 30001）拖垮整矩阵。
    """
    if text is None:
        return None
    parts = [p.strip() for p in str(text).split(",")]
    if len(parts) != 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return (lat, lng)


def _resolve_coord(name: str, city: str, geocode) -> Tuple[float, float]:
    """坐标字符串 → 直接返回；否则地理编码（限定 city）。"""
    coord = _parse_coord(name)
    if coord is not None:
        return coord
    return geocode(name, city=city)


def _resolve_coord_fallback(name: str, city: str, geocode) -> Tuple[float, float]:
    """地理编码带全国搜索兜底：限定 ``city`` 失败（如跨城 driving 用统一默认 city
    解析他城城市名 → 高德 30001）→ 降级为不限城市全国搜索；仍失败才抛错。

    市内正常路径不受影响（限定 city 命中即返回，避免同名歧义）。
    """
    coord = _parse_coord(name)
    if coord is not None:
        return coord
    try:
        return geocode(name, city=city)
    except ValueError:
        logger.warning(
            "geocode(%r, city=%r) 失败，回退全国搜索（可能为城际 driving 跨城场景）", name, city
        )
        return geocode(name, city="")


class MapTool(BaseTool):
    name = "map"
    domain = "map"
    internal_actions = ["batch_route"]   # 内部管道：A 侧交通矩阵
    description = "地图服务：搜索景点位置、计算两点间路线距离与预计耗时。"
    source = "mock"
    input_schema: ClassVar[dict] = {
        "type": "object",
        "properties": {
            "action": {
                "enum": ["search_poi", "route", "batch_route"],
                "description": "search_poi 搜索地点；route 计算单对路线；"
                "batch_route 一次计算多对路线（矩阵用，驾车近似）",
            },
            "query": {"type": "string", "description": "搜索关键词"},
            "origin": {"type": "string", "description": "起点"},
            "destination": {"type": "string", "description": "终点"},
            "origins": {
                "type": "array",
                "items": {"type": "string"},
                "description": "batch_route 起点列表",
            },
            "destinations": {
                "type": "array",
                "items": {"type": "string"},
                "description": "batch_route 终点列表",
            },
            "city": {
                "type": "string",
                "description": "地理编码限定城市（默认北京，避免同名歧义）",
            },
            "mode": {
                "enum": ["transit", "driving", "riding", "walk", "train", "air"],
                "description": "路线模式：公交/驾车/骑行/步行/高铁/飞机，默认 transit；"
                "batch_route 仅支持 driving / walk；train/air 为城际模式"
                "（估算表兜底，source=estimate，批次 1a）",
            },
        },
        "required": ["action"],
    }

    def _run(self, action: str = "search_poi", query: str = "",
             origin: str = "", destination: str = "", mode: str = "transit",
             origins: Optional[List[str]] = None,
             destinations: Optional[List[str]] = None,
             city: str = "北京",
             **kwargs: Any) -> Any:
        if action == "search_poi":
            return self._search(query)
        if action == "route":
            return self._route(origin, destination, mode, city=city)
        if action == "batch_route":
            # 批量测量仅支持 driving / walk（v3/distance）；默认 transit 解析为驾车近似
            batch_mode = mode if mode in ("driving", "walk") else "driving"
            return self._batch_route(
                list(origins or []), list(destinations or []),
                mode=batch_mode, city=city,
            )
        raise ValueError(f"Unknown map action: {action}")

    def _search(self, query: str) -> List[Dict[str, Any]]:
        q = (query or "").strip()
        results: List[Dict[str, Any]] = []
        for name, info in PLACES.items():
            if q and q not in name:
                continue
            results.append({
                "name": name,
                "lat": info["lat"],
                "lng": info["lng"],
                "open": info["open"],
                "price": info["price"],
                "rating": 0,
                "tel": "",
                "type": "",
            })
        return results

    def _route(self, origin: str, destination: str, mode: str = "transit",
               city: str = "北京") -> Dict[str, Any]:
        """单对路线：train/air 走城际估算（``_intercity``），其余走市内固定值（Mock）。"""
        if mode in _INTERCITY_MODES:
            return self._intercity(origin, destination, mode, city=city)
        # Mock：固定行程参数；真实接入高德后按 API 返回替换
        return {
            "from": origin,
            "to": destination,
            "mode": mode,
            "distance_km": 3.5,
            "duration_min": 25,
            "transport_minutes": 25,   # 规范字段（A 侧适配层 parse 用）
            "transit": "地铁1号线 + 步行800m",
            "fare": 4.0,
            "source": "mock",
        }

    def _intercity(self, origin: str, destination: str, mode: str,
                   city: str = "北京") -> Dict[str, Any]:
        """城际模式（train/air）：查估算表 → 估算结果；表缺失回退 driving（不报错）。

        返回字段契约（阶段二 A 侧 live provider 直接消费）：
        ``mode / duration_min / transport_minutes / cost_per_person /
        distance_km / transit_text / source / legs``。
        """
        opt = _lookup_intercity_estimate(origin, destination, mode)
        if opt is not None:
            minutes = int(opt.get("transport_minutes") or 0)
            return {
                "mode": mode,
                "from": origin,
                "to": destination,
                "from_station": opt.get("from_station", ""),
                "to_station": opt.get("to_station", ""),
                "distance_km": float(opt.get("distance_km") or 0.0),
                "duration_min": minutes,
                "transport_minutes": minutes,   # 规范字段（A 侧适配层 parse 用）
                "cost_per_person": float(opt.get("cost_per_person") or 0.0),
                "transit_text": opt.get("transit_text")
                or f"{_MODE_TEXT.get(mode, mode)}（估算）",
                "transit": _MODE_TEXT.get(mode, mode),
                "fare": 0.0,
                "source": "estimate",          # 估算保级，不冒充真数据
                "legs": [],
            }
        logger.warning(
            "城际估算表缺失 %s→%s mode=%s，回退 driving", origin, destination, mode
        )
        return self._route_driving(origin, destination, city=city)

    def _route_driving(self, origin: str, destination: str,
                       city: str = "北京") -> Dict[str, Any]:
        """驾车兜底：Mock 固定值；MapToolLive 覆写为高德驾车真源。"""
        return self._route(origin, destination, mode="driving", city=city)

    def _batch_route(self, origins: List[str], destinations: List[str],
                     mode: str = "transit", city: str = "北京") -> List[Dict[str, Any]]:
        """Mock：对每一对 (起点, 终点) 复用单对结果，返回统一行结构。"""
        rows: List[Dict[str, Any]] = []
        for origin in origins:
            for destination in destinations:
                edge = self._route(origin, destination, mode, city=city)
                rows.append({
                    "origin": origin,
                    "destination": destination,
                    "distance_km": edge["distance_km"],
                    "transport_minutes": edge["transport_minutes"],
                    # C4：mode 统一英文模式名（与 Live 同构），中文描述挪 transit_text
                    "mode": mode,
                    "transit_text": edge.get("transit", ""),
                    "fare": edge.get("fare", 0.0),
                })
        return rows


class MapToolLive(MapTool):
    """高德地图 API 实现版。

    调用链路：
      1. search_poi → AmapClient.search_poi() → /v5/place/text
      2. route → AmapClient.geocode(origin/destination) 获取坐标
               → AmapClient.get_route() → /v3/direction/{mode}
      3. train/air（城际）→ ``_intercity`` 估算表兜底，**绝不透传 get_route**
         （高德无对应端点，直接透传会 ValueError「不支持的路线模式」）

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    name = "map"
    description = "地图服务：搜索景点位置、计算两点间路线距离与预计耗时。"
    source = "live"
    input_schema = MapTool.input_schema

    def __init__(self, client: Any) -> None:
        """初始化 Live 版地图 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
        """
        super().__init__()
        self._client = client

    def _search(self, query: str) -> List[Dict[str, Any]]:
        """调高德关键词搜索 API，返回标准化 POI 列表。"""
        pois = self._client.search_poi(query)
        return [
            {
                "name": p["name"],
                "lat": p["lat"],
                "lng": p["lng"],
                "open": p.get("opentime_today", ""),
                "price": p.get("cost", 0),
                "address": p.get("address", ""),
                "rating": p.get("rating", 0),
                "tel": p.get("tel", ""),
                "type": p.get("type", ""),
            }
            for p in pois
        ]

    def _route(self, origin: str, destination: str, mode: str = "transit",
               city: str = "北京") -> Dict[str, Any]:
        """调高德路线规划 API，返回距离和耗时。

        先地理编码获取起终点坐标，再调路线规划 API。
        地理编码时限定 ``city``（默认北京，避免同名地点歧义）。
        train/air 城际模式在调用高德前拦截（走 ``_intercity`` 估算/兜底）。
        """
        if mode in _INTERCITY_MODES:
            # 批次 1a：必须在 get_route 前拦截，否则 ValueError「不支持的路线模式」
            return self._intercity(origin, destination, mode, city=city)
        return self._route_live(origin, destination, mode, city=city)

    def _route_driving(self, origin: str, destination: str,
                       city: str = "北京") -> Dict[str, Any]:
        """城际估算表缺失时回退 → 高德驾车真源（跨城 driving 高德可用）。

        起终点是城市名：两端坐标按各自城市名限定地理编码——否则会用统一
        默认 ``city=北京`` 解析他城城市名（如「乌鲁木齐」）致 geocode 30001。
        """
        return self._route_live(
            origin, destination, "driving", city=city,
            origin_city=origin, dest_city=destination,
        )

    def _route_live(self, origin: str, destination: str, mode: str,
                    city: str = "北京",
                    origin_city: Optional[str] = None,
                    dest_city: Optional[str] = None) -> Dict[str, Any]:
        """高德市内/驾车路线真源实现（geocode + get_route + 字段映射）。

        ``origin_city`` / ``dest_city``：城际回退用（两端各自城市名限定编码，
        防 30001）；None 时退化为统一 ``city``（市内默认行为）。
        """
        if origin_city is None:
            origin_city = city
        if dest_city is None:
            dest_city = city
        # 地理编码：地址 → 坐标（限定城市，避免同名歧义；跨城场景自动全国搜索兜底）；
        # "lng,lat" 坐标直连跳过编码
        origin_coord: Tuple[float, float] = _resolve_coord_fallback(
            origin, origin_city, self._client.geocode
        )
        dest_coord: Tuple[float, float] = _resolve_coord_fallback(
            destination, dest_city, self._client.geocode
        )

        # 路线规划
        route_data = self._client.get_route(origin_coord, dest_coord, mode=mode, city=city)
        distance_m = route_data["distance"]
        duration_s = route_data["duration"]

        # 票价：公交取 cost，驾车取 tolls，骑行/步行无票价
        if mode == "transit":
            fare = float(route_data.get("cost", 0))
        elif mode == "driving":
            fare = float(route_data.get("tolls", 0))
        else:
            fare = 0.0

        duration_min = round(duration_s / 60)          # 秒 → 分钟
        result: Dict[str, Any] = {
            "from": origin,
            "to": destination,
            "mode": mode,
            "distance_km": round(distance_m / 1000, 2),    # 米 → 公里
            "duration_min": duration_min,
            "transport_minutes": duration_min,             # 规范字段（A 侧适配层 parse 用）
            "transit": _MODE_TEXT.get(mode, mode),
            "fare": fare,
            "source": "live",
        }
        # 2026-09-01：公交模式附带具体线路导航（amap_client 提取），
        # 供 C 端展示「地铁8号线 3站 → 步行300m」。
        if mode == "transit" and route_data.get("transit_text"):
            result["transit_text"] = route_data["transit_text"]
        if mode == "transit" and route_data.get("walking_m"):
            result["walking_m"] = route_data["walking_m"]
        return result

    def _batch_route(self, origins: List[str], destinations: List[str],
                     mode: str = "driving", city: str = "北京") -> List[Dict[str, Any]]:
        """批量路线（矩阵用）：地理编码全部点名后一次取多对距离/时长。

        批量测量走高德 ``/v3/distance``（驾车 / 步行近似，一次请求多起点×1终点，
        N 个终点共 N 次请求）；公交等其它模式请逐对走 ``route``。
        返回每行含规范字段 ``transport_minutes``（分钟），与 A 侧适配层契约一致。
        """
        origin_names = [str(origin) for origin in origins]
        dest_names = [str(destination) for destination in destinations]
        # "lng,lat" 坐标直连跳过地理编码（8.25 B 档），其余点名限定 city 编码
        origin_coords = [
            _resolve_coord(name, city, self._client.geocode) for name in origin_names
        ]
        dest_coords = [
            _resolve_coord(name, city, self._client.geocode) for name in dest_names
        ]

        distance_rows = self._client.get_distances(origin_coords, dest_coords, mode=mode)
        by_pair = {(d["origin"], d["destination"]): d for d in distance_rows}

        rows: List[Dict[str, Any]] = []
        for origin_name, origin_coord in zip(origin_names, origin_coords):
            for dest_name, dest_coord in zip(dest_names, dest_coords):
                row = by_pair.get((origin_coord, dest_coord))
                if row is None:
                    logger.warning(
                        "batch_route 缺 %s → %s 的距离数据（按 0 分钟占位）",
                        origin_name, dest_name,
                    )
                    distance_km, transport_minutes = 0.0, 0
                else:
                    distance_km = round(row["distance_m"] / 1000, 2)
                    transport_minutes = int(round(row["duration_s"] / 60))
                rows.append({
                    "origin": origin_name,
                    "destination": dest_name,
                    "distance_km": distance_km,
                    "transport_minutes": transport_minutes,
                    "mode": mode,
                    "fare": 0.0,
                })
        return rows