"""天气 Tool：气温、降雨、紫外线、风力等（对应 Weather Agent 的 API 封装）。

Mock 版（WeatherTool）：从 MockWorld 读取模拟数据，Demo 剧情用。
Live 版（WeatherToolLive）：调和风天气 API，返回真实天气数据。

切换方式：build_registry() 按 settings.use_real_api 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from tools.base_tool import BaseTool
from tools.mock_data import CITY_MOCK, MockWorld

logger = logging.getLogger("tools.weather")


class WeatherTool(BaseTool):
    name = "weather"
    description = "查询城市实况天气：天气状况、气温、体感温度、降雨概率、紫外线、风力、湿度、能见度。"
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
            "feels_like": w.temperature_c,       # Mock 无体感温度，用气温代替
            "rain_probability": w.rain_probability,
            "uv_index": w.uv_index,
            "wind_kmh": w.wind_kmh,
            "humidity": 50,                       # Mock 默认湿度
            "visibility_km": 10,                  # Mock 默认能见度
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
      1. GeoAPI 城市搜索 → 获取 Location ID（通过 QWeatherClient 缓存）
      2. 实况天气 /v7/weather/now → temp/text/icon/windScale/humidity/precip/feelsLike/vis
      3. 天气指数 /v7/indices?type=5 → UV 指数（可选，失败默认 0）

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    name = "weather"
    description = "查询城市实况天气：天气状况、气温、体感温度、降雨概率、紫外线、风力、湿度、能见度。"
    source = "live"
    input_schema = WeatherTool.input_schema

    def __init__(self, client: Any) -> None:
        """初始化 Live 版天气 Tool。

        Args:
            client: QWeatherClient 实例（共享 API KEY + Host + Location ID 缓存）
        """
        super().__init__()  # 不实际使用 MockWorld
        self._client = client

    def _run(self, city: str = "", date: Optional[str] = None) -> Dict[str, Any]:
        from datetime import date as date_cls
        from urllib.parse import urlencode

        city = city or CITY_MOCK
        loc_id = self._client.get_location_id(city)
        now_data = self._fetch_now(loc_id)
        uv_index = self._fetch_uv_index(loc_id)

        # 字段映射
        icon = now_data.get("icon", "999")
        condition = _QWEATHER_ICON_TEXT.get(icon, now_data.get("text", "未知"))
        temp = float(now_data.get("temp", 0))
        feels_like = float(now_data.get("feelsLike", temp))
        wind_scale = now_data.get("windScale", "0")
        wind_kmh = _WIND_SCALE_KMH.get(wind_scale, 0)
        precip = float(now_data.get("precip", "0"))
        humidity = int(now_data.get("humidity", 0))
        visibility = float(now_data.get("vis", 10))
        # 和风无降雨概率字段，用降水量推断：>0mm → 80%，否则 10%
        rain_prob = 80 if precip > 0 else 10

        return {
            "city": city,
            "date": date or date_cls.today().isoformat(),
            "condition": condition,
            "temperature_c": temp,
            "feels_like": feels_like,
            "rain_probability": rain_prob,
            "uv_index": uv_index,
            "wind_kmh": wind_kmh,
            "humidity": humidity,
            "visibility_km": visibility,
        }

    def _fetch_now(self, loc_id: str) -> Dict[str, Any]:
        """调实况天气 API。"""
        from urllib.parse import urlencode
        url = f"/v7/weather/now?{urlencode({'location': loc_id})}"
        resp = self._client.get(url)
        now_list = resp.get("now")
        if not now_list:
            raise ValueError(f"实况天气 API 返回为空: {resp.get('code', 'unknown')}")
        return now_list

    def _fetch_uv_index(self, loc_id: str) -> int:
        """调天气指数 API 获取 UV 指数（type=5）。失败则默认 0。"""
        try:
            from urllib.parse import urlencode
            url = f"/v7/indices?{urlencode({'location': loc_id, 'type': '5'})}"
            resp = self._client.get(url)
            daily = resp.get("daily", [])
            if daily:
                return int(daily[0].get("category", 0))
        except Exception as exc:
            logger.warning("UV 指数获取失败，默认 0: %s", exc)
        return 0


class WeatherWarningTool(BaseTool):
    """天气预警 Tool：查询城市当前生效的天气预警（暴雨/台风/雷电等）。

    Mock 版：返回空预警列表（无预警）。
    Live 版：调 /v7/warning/now 获取官方发布的极端天气预警。
    """

    name = "weather_warning"
    description = "查询城市当前天气预警：暴雨、台风、雷电、大风等极端天气预警信息。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
        },
        "required": ["city"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, city: str = "") -> Dict[str, Any]:
        return {
            "city": city or CITY_MOCK,
            "warnings": [],  # Mock 默认无预警
            "has_warning": False,
        }


class WeatherWarningToolLive(WeatherWarningTool):
    """和风天气预警 API 实现版。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, city: str = "") -> Dict[str, Any]:
        city = city or CITY_MOCK
        lat, lon = self._client.get_location_coord(city)
        url = f"/weatheralert/v1/current/{lat}/{lon}"
        try:
            resp = self._client.get(url)
            alerts = resp.get("alerts", [])
        except Exception as exc:
            logger.warning("天气预警 API 调用失败，返回空预警: %s", exc)
            alerts = []
        # 映射 v1 响应格式为统一输出
        warnings = []
        for a in alerts:
            warnings.append({
                "title": a.get("headline", ""),
                "type": a.get("eventType", {}).get("name", ""),
                "level": a.get("color", {}).get("code", ""),
                "text": a.get("description", ""),
            })
        return {
            "city": city,
            "warnings": warnings,
            "has_warning": len(warnings) > 0,
        }


class AirQualityTool(BaseTool):
    """空气质量 Tool：查询城市当前 AQI、PM2.5、PM10 等。

    Mock 版：返回固定"优"数据。
    Live 版：调 /v7/air/now 获取实时空气质量。
    """

    name = "air_quality"
    description = "查询城市空气质量：AQI 指数、PM2.5、PM10、主要污染物。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
        },
        "required": ["city"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, city: str = "") -> Dict[str, Any]:
        return {
            "city": city or CITY_MOCK,
            "aqi": 35,
            "category": "优",
            "pm25": 15.0,
            "pm10": 30.0,
            "no2": 20.0,
            "so2": 5.0,
            "co": 0.5,
            "o3": 60.0,
        }


class AirQualityToolLive(AirQualityTool):
    """和风空气质量 API 实现版。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, city: str = "") -> Dict[str, Any]:
        city = city or CITY_MOCK
        lat, lon = self._client.get_location_coord(city)
        url = f"/airquality/v1/current/{lat}/{lon}"
        try:
            resp = self._client.get(url)
        except Exception as exc:
            logger.warning("空气质量 API 调用失败，返回默认值: %s", exc)
            return {
                "city": city,
                "aqi": 0,
                "category": "未知",
                "pm25": 0.0,
                "pm10": 0.0,
                "no2": 0.0,
                "so2": 0.0,
                "co": 0.0,
                "o3": 0.0,
            }
        # v1 响应: indexes 数组含 AQI/类别, pollutants 数组含各污染物浓度
        indexes = resp.get("indexes", [])
        pollutants = resp.get("pollutants", [])

        # 从 indexes 中取 AQI（优先 us-epa，否则取第一个）
        aqi = 0
        category = "未知"
        for idx in indexes:
            if idx.get("code") == "us-epa" or aqi == 0:
                aqi = int(idx.get("aqi", 0))
                category = idx.get("category", "未知")
                if idx.get("code") == "us-epa":
                    break

        # 从 pollutants 中提取各污染物浓度
        pollutant_map = {}
        for p in pollutants:
            code = p.get("code", "")
            conc = p.get("concentration", {})
            pollutant_map[code] = float(conc.get("value", 0))

        return {
            "city": city,
            "aqi": aqi,
            "category": category,
            "pm25": pollutant_map.get("pm2p5", 0.0),
            "pm10": pollutant_map.get("pm10", 0.0),
            "no2": pollutant_map.get("no2", 0.0),
            "so2": pollutant_map.get("so2", 0.0),
            "co": pollutant_map.get("co", 0.0),
            "o3": pollutant_map.get("o3", 0.0),
        }


class WeatherForecastTool(BaseTool):
    """逐小时天气预报 Tool：查询未来 24 小时天气变化趋势。

    Mock 版：返回固定"全天晴"数据。
    Live 版：调 /v7/weather/24h 获取逐小时预报。
    """

    name = "weather_forecast"
    description = "查询城市未来24小时逐小时天气预报：气温、天气状况、降雨概率变化趋势。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名"},
            "hours": {"type": "integer", "description": "返回小时数（默认24）"},
        },
        "required": ["city"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, city: str = "", hours: int = 24) -> Dict[str, Any]:
        w = self._world.get_weather()
        from datetime import datetime, timedelta
        hourly = []
        now = datetime.now()
        for i in range(min(hours, 24)):
            t = now + timedelta(hours=i)
            hourly.append({
                "time": t.strftime("%H:%M"),
                "temp": w.temperature_c,
                "condition": w.condition,
                "rain_probability": w.rain_probability,
            })
        return {
            "city": city or CITY_MOCK,
            "hours": hourly,
            "summary": f"未来{len(hourly)}小时{w.condition}",
        }


class WeatherForecastToolLive(WeatherForecastTool):
    """和风逐小时天气预报 API 实现版。"""

    source = "live"

    def __init__(self, client: Any) -> None:
        super().__init__()
        self._client = client

    def _run(self, city: str = "", hours: int = 24) -> Dict[str, Any]:
        from urllib.parse import urlencode

        city = city or CITY_MOCK
        loc_id = self._client.get_location_id(city)
        url = f"/v7/weather/24h?{urlencode({'location': loc_id})}"
        resp = self._client.get(url)
        hourly_raw = resp.get("hourly", [])
        hourly = []
        for h in hourly_raw[:hours]:
            icon = h.get("iconCode") or ""
            text = h.get("text", "")
            # 优先用 iconCode 映射，iconCode 为空时 fallback 到 text 字段
            condition = _QWEATHER_ICON_TEXT.get(icon, "") or text or "未知"
            temp = float(h.get("temp", 0))
            precip = float(h.get("precip", "0"))
            rain_prob = 80 if precip > 0 else 10
            hourly.append({
                "time": h.get("fxTime", "")[-5:],  # 取 HH:MM
                "temp": temp,
                "condition": condition,
                "rain_probability": rain_prob,
            })
        # 生成摘要
        rain_hours = sum(1 for h in hourly if h["rain_probability"] > 50)
        if rain_hours == 0:
            summary = f"未来{len(hourly)}小时无降雨"
        else:
            summary = f"未来{len(hourly)}小时中有{rain_hours}小时可能降雨"
        return {
            "city": city,
            "hours": hourly,
            "summary": summary,
        }
