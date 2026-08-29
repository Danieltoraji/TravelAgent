"""交通 Tool：公交 / 地铁 / 打车预计耗时与拥堵（对应 Traffic Agent 的 API 封装）。

Mock 版（TrafficTool）：返回固定畅通状态，Demo 剧情用。
Live 版（TrafficToolLive）：调高德路线规划 API，返回真实耗时与路况。

切换方式：build_registry() 按 settings.use_real_map_api 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional, Tuple

from tools.base_tool import BaseTool
from tools.mock_data import MockWorld

logger = logging.getLogger("tools.traffic")

# TrafficTool mode → AmapClient mode 映射
_MODE_MAP: Dict[str, str] = {
    "transit": "transit",  # 公交
    "taxi": "driving",      # 打车 ≈ 驾车
    "walk": "walk",         # 步行
}

# mode → 中文描述
_MODE_TEXT: Dict[str, str] = {
    "transit": "公交",
    "taxi": "打车",
    "walk": "步行",
}


class TrafficTool(BaseTool):
    name = "traffic"
    domain = "traffic"
    description = "交通状态：公交/地铁/打车预计耗时与拥堵程度。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "mode": {"enum": ["transit", "taxi", "walk"]},
            # T1（0829）：POI 所在城市——不传则全国范围地理编码（同名歧义风险），
            # 公交路线规划回退北京（transit 端点 city 必填）；调用方应显式传
            "city": {"type": "string", "description": "起终点所在城市（地理编码与公交规划限定）"},
        },
        "required": ["origin", "destination"],
    }

    def _run(self, origin: str = "", destination: str = "", mode: str = "transit",
             city: str = "") -> Dict[str, Any]:
        # Mock：固定畅通状态；真实接入后可返回 delay_min 变化以触发剧情
        return {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "duration_min": 30,
            "congestion": "畅通",
            "delay_min": 0,
            "note": "地铁1号线运行正常",
            "distance_km": 3.5,
        }


class TrafficToolLive(TrafficTool):
    """高德地图路线规划 API 实现版。

    调用链路：
      1. geocode(origin) + geocode(destination) 获取起终点坐标
      2. get_route(origin_coord, dest_coord, amap_mode) 获取距离和耗时
      3. 驾车模式下根据平均速度推断拥堵程度

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    source = "live"

    def __init__(self, client: Any, world: Optional[MockWorld] = None) -> None:
        """初始化 Live 版交通 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
            world: 可选 MockWorld，用于 Demo 突发事件 override（如 set_traffic_delay()）
        """
        super().__init__()
        self._client = client
        self._world = world

    def _run(self, origin: str = "", destination: str = "", mode: str = "transit",
             city: str = "") -> Dict[str, Any]:
        amap_mode = _MODE_MAP.get(mode, "transit")

        # T1（0829）：地理编码与路线规划按 POI 所在城市限定（此前硬编码"北京"，
        # 非北京目的地的交通监控必现失败）。city 为空 → geocode 全国查询、
        # transit 规划回退北京（调用方应显式传 city）。
        origin_coord: Tuple[float, float] = self._client.geocode(origin, city=city)
        dest_coord: Tuple[float, float] = self._client.geocode(destination, city=city)

        # 路线规划
        route_data = self._client.get_route(origin_coord, dest_coord, mode=amap_mode,
                                            city=city or "北京")
        distance_m = route_data["distance"]
        duration_s = route_data["duration"]

        duration_min = round(duration_s / 60)
        distance_km = distance_m / 1000

        # 拥堵推断：仅驾车模式（打车），根据平均速度判断
        congestion = "畅通"
        delay_min = 0
        if amap_mode == "driving" and duration_min > 0:
            speed = distance_km / (duration_min / 60)  # km/h
            if speed < 15:
                congestion = "拥堵"
            elif speed < 30:
                congestion = "缓行"
            # 延迟 = 实际耗时 - 畅通耗时（按 40km/h 基准估算）
            free_flow_min = round(distance_km / 40 * 60)
            delay_min = max(0, duration_min - free_flow_min)

        mode_text = _MODE_TEXT.get(mode, mode)
        note = f"距离 {distance_km:.1f}km"
        if congestion != "畅通":
            note += f"，{congestion}延误约 {delay_min} 分钟"

        # Demo 突发事件 override：MockWorld.set_traffic_delay() 覆盖 API 计算的延误数据
        if self._world is not None:
            override = self._world.get_traffic_override(origin, destination)
            if override:
                congestion = override.get("congestion", congestion)
                delay_min = override.get("delay_min", delay_min)
                note += f"（⚠ {congestion}，延误 {delay_min} 分钟）"
                logger.info("Traffic override applied: %s→%s delay=%dmin %s",
                            origin, destination, delay_min, congestion)

        logger.info("Traffic: %s→%s (%s) %dmin %s", origin, destination, mode_text, duration_min, congestion)

        return {
            "origin": origin,
            "destination": destination,
            "mode": mode,
            "duration_min": duration_min,
            "congestion": congestion,
            "delay_min": delay_min,
            "note": note,
            "distance_km": round(distance_km, 2),
        }
