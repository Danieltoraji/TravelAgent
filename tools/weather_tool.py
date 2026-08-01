"""天气 Tool：气温、降雨、紫外线、风力等（对应 Weather Agent 的 API 封装）。

Mock 版（WeatherTool）：从 MockWorld 读取模拟数据，Demo 剧情用。
Live 版（WeatherToolLive）：调和风天气 API，返回真实天气数据。

切换方式：build_registry() 按 settings.use_real_api 自动选择。
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from tools.base_tool import BaseTool
from tools.mock_data import CITY_MOCK, MockWorld

logger = logging.getLogger("tools.weather")


class WeatherTool(BaseTool):
    name = "weather"
    description = "查询城市天气：天气状况、气温、降雨概率、紫外线、风力。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
            "date": {"type": "string", "description": "日期 YYYY-MM-DD，缺省为当天"},
        },
        "required": ["city"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, city: str = "", date: Optional[str] = None) -> Dict[str, Any]:
        w = self._world.get_weather()
        return {
            "city": city or CITY_MOCK,
            "date": date or self._world.now.date().isoformat(),
            "condition": w.condition,
            "temperature_c": w.temperature_c,
            "rain_probability": w.rain_probability,
            "uv_index": w.uv_index,
            "wind_kmh": w.wind_kmh,
        }


# 和风天气现象代码 → 中文映射（用于 Live 版返回 condition 字段）
_QWEATHER_ICON_TEXT: Dict[str, str] = {
    "100": "晴", "101": "多云", "102": "少云", "103": "晴间多云", "104": "阴",
    "300": "阵雨", "301": "强阵雨", "302": "雷阵雨", "303": "强雷阵雨",
    "304": "雷阵雨伴有冰雹", "305": "小雨", "306": "中雨", "307": "大雨",
    "308": "极端降雨", "309": "毛毛雨", "310": "暴雨", "311": "大暴雨",
    "312": "特大暴雨", "313": "冻雨", "350": "阵雨", "399": "雨",
    "400": "小雪", "401": "中雪", "402": "大雪", "403": "暴雪",
    "404": "雨夹雪", "405": "雨雪天气", "406": "阵雨夹雪", "407": "阵雪",
    "499": "雪", "500": "薄雾", "501": "雾", "502": "霾",
    "503": "扬沙", "504": "浮尘", "507": "沙尘暴", "508": "强沙尘暴",
    "509": "浓雾", "510": "强浓雾", "511": "中度霾", "512": "重度霾",
    "513": "严重霾", "514": "大雾", "515": "特强浓雾",
    "900": "热", "901": "冷", "999": "未知",
}

# 风力等级 → km/h 近似换算表（蒲福风级）
_WIND_SCALE_KMH: Dict[str, int] = {
    "0": 0, "1": 3, "2": 10, "3": 17, "4": 25, "5": 33,
    "6": 42, "7": 51, "8": 61, "9": 71, "10": 85, "11": 100, "12": 120,
}


class WeatherToolLive(WeatherTool):
    """和风天气 API 实现版（API KEY 认证）。

    调用链路：
      1. GeoAPI 城市搜索 → 获取 Location ID（类实例级缓存）
      2. 实况天气 /v7/weather/now → temp/text/icon/windScale/humidity/precip
      3. 天气指数 /v7/indices?type=5 → UV 指数（可选，失败默认 0）

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    name = "weather"
    description = "查询城市天气：天气状况、气温、降雨概率、紫外线、风力。"
    source = "live"
    input_schema = WeatherTool.input_schema

    def __init__(self, api_key: str, api_host: str, timeout: float = 10.0) -> None:
        super().__init__()  # 不实际使用 MockWorld，但保持构造签名一致
        self._api_key = api_key
        self._api_host = api_host.rstrip("/")
        self._timeout = timeout
        self._location_cache: Dict[str, str] = {}  # 城市 → Location ID

    def _run(self, city: str = "", date: Optional[str] = None) -> Dict[str, Any]:
        city = city or CITY_MOCK
        from datetime import date as date_cls

        loc_id = self._get_location_id(city)
        now_data = self._fetch_now(loc_id)
        uv_index = self._fetch_uv_index(loc_id)

        # 字段映射
        icon = now_data.get("icon", "999")
        condition = _QWEATHER_ICON_TEXT.get(icon, now_data.get("text", "未知"))
        temp = float(now_data.get("temp", 0))
        wind_scale = now_data.get("windScale", "0")
        wind_kmh = _WIND_SCALE_KMH.get(wind_scale, 0)
        precip = float(now_data.get("precip", "0"))
        # 和风无降雨概率字段，用降水量推断：>0mm → 80%，否则 10%
        rain_prob = 80 if precip > 0 else 10

        return {
            "city": city,
            "date": date or date_cls.today().isoformat(),
            "condition": condition,
            "temperature_c": temp,
            "rain_probability": rain_prob,
            "uv_index": uv_index,
            "wind_kmh": wind_kmh,
        }

    def _get_location_id(self, city: str) -> str:
        """调 GeoAPI 城市搜索，获取 Location ID（带缓存）。"""
        if city in self._location_cache:
            return self._location_cache[city]

        url = f"https://{self._api_host}/geo/v2/city/lookup?{urlencode({'location': city})}"
        resp = self._http_get(url)
        locations = resp.get("location", [])
        if not locations:
            raise ValueError(f"GeoAPI 未找到城市: {city}")
        loc_id = locations[0]["id"]
        self._location_cache[city] = loc_id
        logger.info("GeoAPI: %s → Location ID %s", city, loc_id)
        return loc_id

    def _fetch_now(self, loc_id: str) -> Dict[str, Any]:
        """调实况天气 API。"""
        url = f"https://{self._api_host}/v7/weather/now?{urlencode({'location': loc_id})}"
        resp = self._http_get(url)
        now_list = resp.get("now")
        if not now_list:
            raise ValueError(f"实况天气 API 返回为空: {resp.get('code', 'unknown')}")
        return now_list

    def _fetch_uv_index(self, loc_id: str) -> int:
        """调天气指数 API 获取 UV 指数（type=5）。失败则默认 0。"""
        try:
            url = (f"https://{self._api_host}/v7/indices?"
                   f"{urlencode({'location': loc_id, 'type': '5'})}")
            resp = self._http_get(url)
            daily = resp.get("daily", [])
            if daily:
                return int(daily[0].get("category", 0))
        except Exception as exc:
            logger.warning("UV 指数获取失败，默认 0: %s", exc)
        return 0

    def _http_get(self, url: str) -> Dict[str, Any]:
        """发送 GET 请求（API KEY 认证），返回解析后的 JSON dict。"""
        req = Request(url)
        req.add_header("X-QW-Api-Key", self._api_key)
        req.add_header("Accept-Encoding", "gzip")
        logger.debug("GET %s", url)
        with urlopen(req, timeout=self._timeout) as resp:
            raw = resp.read()
            # 处理 gzip 压缩响应
            if resp.headers.get("Content-Encoding") == "gzip":
                import gzip
                raw = gzip.decompress(raw)
            return json.loads(raw)
