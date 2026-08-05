"""高德地图 API 客户端：统一认证 + HTTP 请求封装。

所有 Live 版地图相关 Tool 共用同一个 AmapClient 实例，
认证方式为 URL query string 中传 ``key`` 参数（不同于和风天气的 header 认证）。

高德 API 基础域名：https://restapi.amap.com
坐标系：GCJ-02（国测局坐标，国内通用）
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("tools.amap")

_AMAP_BASE_URL = "https://restapi.amap.com"


class AmapClient:
    """高德地图 API 客户端（API Key 认证）。

    用法::

        client = AmapClient(api_key="your-key")
        lat, lng = client.geocode("故宫")                    # 地理编码
        pois = client.search_poi("故宫")                      # 关键词搜索
        route = client.get_route((39.91, 116.39), (39.92, 116.40), "transit")
    """

    def __init__(self, api_key: str, timeout: float = 10.0) -> None:
        self._api_key = api_key
        self._timeout = timeout
        self._geocode_cache: Dict[str, Tuple[float, float]] = {}  # 地址 → (lat, lng)

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def geocode(self, address: str, city: str = "") -> Tuple[float, float]:
        """地理编码：地址 → 坐标 (lat, lng)。

        调用 ``/v3/geocode/geo``，带缓存避免重复请求。
        """
        cache_key = f"{address}|{city}"
        if cache_key in self._geocode_cache:
            return self._geocode_cache[cache_key]

        params: Dict[str, str] = {"address": address}
        if city:
            params["city"] = city
        resp = self._get("/v3/geocode/geo", params)

        geocodes = resp.get("geocodes", [])
        if not geocodes:
            raise ValueError(f"高德地理编码未找到地址: {address}")

        location = geocodes[0].get("location", "")  # "116.397428,39.90923"
        if not location:
            raise ValueError(f"高德地理编码返回无坐标: {address}")

        lng_str, lat_str = location.split(",")
        lat, lng = float(lat_str), float(lng_str)
        self._geocode_cache[cache_key] = (lat, lng)
        logger.info("Geocode: %s → (%.6f, %.6f)", address, lat, lng)
        return lat, lng

    def search_poi(
        self,
        query: str,
        city: str = "",
        types: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """关键词搜索 POI。

        调用 ``/v3/place/text``，返回标准化 POI 列表。
        """
        params: Dict[str, str] = {
            "keywords": query,
            "offset": str(min(limit, 25)),
            "extensions": "all",
        }
        if city:
            params["city"] = city
        if types:
            params["types"] = types

        resp = self._get("/v3/place/text", params)
        pois = resp.get("pois", [])
        return [self._normalize_poi(p) for p in pois[:limit]]

    def search_poi_around(
        self,
        location: Tuple[float, float],
        radius: int = 1000,
        keywords: str = "",
        types: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """周边搜索 POI。

        调用 ``/v3/place/around``，返回标准化 POI 列表。

        Args:
            location: 中心点坐标 (lat, lng)
            radius: 搜索半径（米）
            keywords: 搜索关键词（可选）
            types: POI 类型编码（可选）
            limit: 返回数量上限
        """
        params: Dict[str, str] = {
            "location": f"{location[1]},{location[0]}",  # 高德格式: lng,lat
            "radius": str(radius),
            "offset": str(min(limit, 25)),
            "sortrule": "distance",
            "extensions": "all",
        }
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types

        resp = self._get("/v3/place/around", params)
        pois = resp.get("pois", [])
        return [self._normalize_poi(p) for p in pois[:limit]]

    def get_route(
        self,
        origin: Tuple[float, float],
        destination: Tuple[float, float],
        mode: str = "transit",
        city: str = "北京",
    ) -> Dict[str, Any]:
        """路线规划：返回距离（米）和耗时（秒）。

        根据 ``mode`` 调用不同端点：
        - ``transit``  → /v3/direction/transit/integrated（公交）
        - ``driving``  → /v3/direction/driving（驾车）
        - ``riding``   → /v4/direction/bicycling（骑行）
        - ``walk``     → /v3/direction/walking（步行）

        Returns:
            ``{"distance": int, "duration": int}`` — 距离（米）、耗时（秒）
        """
        origin_str = f"{origin[1]},{origin[0]}"          # lng,lat
        dest_str = f"{destination[1]},{destination[0]}"

        if mode == "transit":
            params = {
                "origin": origin_str,
                "destination": dest_str,
                "city": city,
            }
            resp = self._get("/v3/direction/transit/integrated", params)
            return self._extract_transit_route(resp)

        if mode == "driving":
            params = {"origin": origin_str, "destination": dest_str}
            resp = self._get("/v3/direction/driving", params)
            return self._extract_driving_route(resp)

        if mode == "riding":
            params = {"origin": origin_str, "destination": dest_str}
            resp = self._get("/v4/direction/bicycling", params)
            return self._extract_bicycling_route(resp)

        if mode == "walk":
            params = {"origin": origin_str, "destination": dest_str}
            resp = self._get("/v3/direction/walking", params)
            return self._extract_walking_route(resp)

        raise ValueError(f"不支持的路线模式: {mode}")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """发送 GET 请求，自动附加 ``key`` 参数，返回解析后的 JSON dict。

        v3 API 的错误通过 ``status != "1"`` 判断；
        v4 API（如骑行）的返回结构不同，数据在 ``data`` 字段中，
        错误通过 ``errcode`` 判断。
        """
        all_params = {"key": self._api_key}
        if params:
            all_params.update(params)

        url = f"{_AMAP_BASE_URL}{path}?{urlencode(all_params)}"
        req = Request(url)
        req.add_header("Accept-Encoding", "gzip")
        logger.debug("GET %s", url)

        with urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            data = json.loads(raw)

        # v4 API（如 /v4/direction/bicycling）返回结构不同
        if path.startswith("/v4/"):
            # v4 错误检查：errcode != 0 表示失败
            errcode = data.get("errcode", 0)
            if errcode != 0:
                err_msg = data.get("errmsg", "unknown")
                raise ValueError(f"高德 v4 API 错误 [{errcode}]: {err_msg}")
            # v4 数据在 data 字段中
            return data.get("data", {})

        # v3 API 错误检查：status != "1" 表示失败
        if data.get("status") != "1":
            err_code = data.get("infocode", "unknown")
            err_msg = data.get("info", "unknown")
            raise ValueError(f"高德 API 错误 [{err_code}]: {err_msg}")

        return data

    @staticmethod
    def _normalize_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
        """将高德 POI 原始返回标准化为项目内部结构。"""
        location = poi.get("location", "")  # "lng,lat"
        lat, lng = 0.0, 0.0
        if location and "," in location:
            parts = location.split(",")
            lng = float(parts[0])
            lat = float(parts[1])
        return {
            "name": poi.get("name", ""),
            "lat": lat,
            "lng": lng,
            "address": poi.get("address", "") or "",
            "tel": poi.get("tel", "") or "",
            "type": poi.get("type", "") or "",
        }

    @staticmethod
    def _extract_transit_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从公交路线规划响应中提取距离和耗时。"""
        route = resp.get("route", {})
        transit = route.get("transits", [])
        if not transit:
            raise ValueError("高德公交路线规划返回为空")
        first = transit[0]
        return {
            "distance": int(first.get("distance", 0)),
            "duration": int(first.get("duration", 0)),
        }

    @staticmethod
    def _extract_driving_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从驾车路线规划响应中提取距离和耗时。"""
        route = resp.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            raise ValueError("高德驾车路线规划返回为空")
        first = paths[0]
        return {
            "distance": int(first.get("distance", 0)),
            "duration": int(first.get("duration", 0)),
        }

    @staticmethod
    def _extract_bicycling_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从骑行路线规划响应中提取距离和耗时。

        v4 API 的数据已在 ``_get()`` 中解包到顶层，
        paths 直接在返回的 dict 中。
        """
        paths = resp.get("paths", [])
        if not paths:
            raise ValueError("高德骑行路线规划返回为空")
        first = paths[0]
        return {
            "distance": int(first.get("distance", 0)),
            "duration": int(first.get("duration", 0)),
        }

    @staticmethod
    def _extract_walking_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从步行路线规划响应中提取距离和耗时。"""
        route = resp.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            raise ValueError("高德步行路线规划返回为空")
        first = paths[0]
        return {
            "distance": int(first.get("distance", 0)),
            "duration": int(first.get("duration", 0)),
        }

    @property
    def api_key(self) -> str:
        return self._api_key
