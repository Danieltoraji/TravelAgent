"""Mock 数据源 + 剧情模拟。

比赛 Demo 剧情（对应《任务整理.md》第十节）：
  1. 初始：故宫排队 20 分钟、天气晴
  2. 剧情触发：天气转暴雨 / 故宫排队 -> 120 分钟
  3. Decision Engine 据此评估影响，决定是否 Replan

真实 API 接入后，各 Tool 按相同签名改为 HTTP 调用，本文件仅保留为可选的模拟器。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

CITY_MOCK = "北京"


@dataclass
class WeatherData:
    condition: str
    temperature_c: float
    rain_probability: int      # 0-100
    uv_index: int
    wind_kmh: int


# ---------------- 初始种子数据 ----------------

PLACES: Dict[str, Dict[str, Any]] = {
    "故宫":       {"lat": 39.916, "lng": 116.397, "open": "08:30-17:00", "queue_min": 20,  "ticket": True,  "price": 60.0},
    "景山公园":   {"lat": 39.925, "lng": 116.396, "open": "06:30-21:00", "queue_min": 5,   "ticket": True,  "price": 2.0},
    "王府井":     {"lat": 39.912, "lng": 116.411, "open": "10:00-22:00", "queue_min": 0,   "ticket": False, "price": 0.0},
    "天坛":       {"lat": 39.882, "lng": 116.407, "open": "06:00-22:00", "queue_min": 15,  "ticket": True,  "price": 15.0},
    "全聚德(前门店)": {"lat": 39.900, "lng": 116.398, "open": "11:00-21:30", "queue_min": 40, "ticket": False, "price": 180.0},
}


class MockWorld:
    """带时间演变的模拟世界：驱动 Demo 剧情（时间推进 / 天气突变 / 排队暴涨）。

    Live 模式下，MockWorld 同时充当「突发事件 override 层」：
    - set_weather() 记录 override 字段，WeatherToolLive 在 API 数据上叠加覆盖
    - set_traffic_delay() 记录交通延误，TrafficToolLive 在 API 数据上叠加覆盖
    - set_queue() 记录排队时长，ScenicToolLive fallback 读取
    """

    def __init__(self) -> None:
        self._time = datetime.now()
        self._weather = WeatherData(condition="晴", temperature_c=28.0,
                                    rain_probability=10, uv_index=6, wind_kmh=12)
        self._queue_override: Dict[str, int] = {}
        self._weather_overrides: Dict[str, Any] = {}
        self._traffic_overrides: Dict[str, Dict[str, Any]] = {}

    @property
    def now(self) -> datetime:
        return self._time

    def advance(self, minutes: float = 5.0) -> None:
        """推进模拟时钟（Demo 用，生产环境由真实时间驱动）。"""
        self._time += timedelta(minutes=minutes)

    # -------- 天气 override --------

    def set_weather(self, **kw: Any) -> None:
        """修改天气字段以触发剧情，例如 set_weather(condition="暴雨", rain_probability=85)。

        同时记录到 _weather_overrides，供 WeatherToolLive 在 API 数据上叠加覆盖。
        """
        for k, v in kw.items():
            setattr(self._weather, k, v)
            self._weather_overrides[k] = v

    @property
    def weather_overrides(self) -> Dict[str, Any]:
        """返回当前天气 override 字段（Live 模式下叠加到 API 数据上）。"""
        return dict(self._weather_overrides)

    def clear_weather_overrides(self) -> None:
        """清空天气 override（恢复纯 API 数据）。"""
        self._weather_overrides.clear()

    # -------- 交通 override --------

    def set_traffic_delay(self, origin: str, destination: str,
                          delay_min: int, congestion: str = "拥堵") -> None:
        """设置交通延误 override，例如 set_traffic_delay("北京", "故宫", 45, "拥堵")。

        TrafficToolLive 在 API 数据上叠加此覆盖，用于 Demo 突发事件注入。
        """
        key = f"{origin}→{destination}"
        self._traffic_overrides[key] = {"delay_min": delay_min, "congestion": congestion}

    def get_traffic_override(self, origin: str, destination: str) -> Optional[Dict[str, Any]]:
        """返回交通 override dict（含 delay_min, congestion），无则 None。"""
        key = f"{origin}→{destination}"
        return self._traffic_overrides.get(key)

    def clear_traffic_overrides(self) -> None:
        """清空交通 override（恢复纯 API 数据）。"""
        self._traffic_overrides.clear()

    # -------- 排队 override --------

    def set_queue(self, place: str, minutes: int) -> None:
        """覆盖某景点排队时长，例如 set_queue("故宫", 120)。"""
        self._queue_override[place] = minutes

    def get_weather(self) -> WeatherData:
        return self._weather

    def get_queue(self, place: str) -> int:
        if place in self._queue_override:
            return self._queue_override[place]
        return PLACES.get(place, {}).get("queue_min", 0)

    def get_place(self, name: str) -> Optional[Dict[str, Any]]:
        base = PLACES.get(name)
        if base is None:
            return None
        return {**base, "queue_min": self.get_queue(name)}
