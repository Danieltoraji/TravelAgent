"""和风天气 API 客户端：共享认证 + Location ID 缓存。

所有 Live 版天气相关 Tool 共用同一个 QWeatherClient 实例，
避免重复调 GeoAPI 城市搜索（Location ID 缓存共享）。
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("tools.qweather")


class QWeatherClient:
    """和风天气 API 客户端（API KEY 认证）。

    用法：
        client = QWeatherClient(api_key="...", api_host="...")
        loc_id = client.get_location_id("北京")       # 带缓存
        data = client.get(f"/v7/weather/now?location={loc_id}")
    """

    def __init__(self, api_key: str, api_host: str, timeout: float | None = None) -> None:
        from config.settings import settings
        self._api_key = api_key
        self._api_host = api_host.rstrip("/")
        self._timeout = timeout if timeout is not None else settings.api_timeout
        self._location_cache: Dict[str, str] = {}  # 城市 → Location ID
        self._coord_cache: Dict[str, tuple] = {}   # 城市 → (lat, lon)

    def get_location_id(self, city: str) -> str:
        """调 GeoAPI 城市搜索，获取 Location ID（带缓存）。"""
        if city in self._location_cache:
            return self._location_cache[city]

        url = f"https://{self._api_host}/geo/v2/city/lookup?{urlencode({'location': city})}"
        resp = self.get(url)
        locations = resp.get("location", [])
        if not locations:
            raise ValueError(f"GeoAPI 未找到城市: {city}")
        loc = locations[0]
        loc_id = loc["id"]
        self._location_cache[city] = loc_id
        # 同时缓存坐标（v1 API 需要）
        self._coord_cache[city] = (float(loc["lat"]), float(loc["lon"]))
        logger.info("GeoAPI: %s → Location ID %s", city, loc_id)
        return loc_id

    def get_location_coord(self, city: str) -> tuple:
        """获取城市经纬度 (lat, lon)，带缓存。

        天气预警和空气质量的新 v1 API 需要经纬度路径参数。
        """
        if city in self._coord_cache:
            return self._coord_cache[city]
        # 先调 get_location_id 填充缓存
        self.get_location_id(city)
        return self._coord_cache[city]

    def get(self, path_or_url: str) -> Dict[str, Any]:
        """发送 GET 请求（API KEY 认证），返回解析后的 JSON dict。

        参数可以是完整 URL，也可以是 API 路径（如 /v7/weather/now?location=101010100）。
        """
        if path_or_url.startswith("http"):
            url = path_or_url
        else:
            url = f"https://{self._api_host}{path_or_url}"

        req = Request(url)
        req.add_header("X-QW-Api-Key", self._api_key)
        req.add_header("Accept-Encoding", "gzip")
        logger.debug("GET %s", url)
        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except URLError as exc:
            raise ConnectionError(f"和风天气 API 请求失败 [{url}]: {exc}") from exc

    @property
    def api_host(self) -> str:
        return self._api_host

    @property
    def api_key(self) -> str:
        return self._api_key
