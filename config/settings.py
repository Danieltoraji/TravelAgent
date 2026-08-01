"""全局配置：轮询频率、超时、API Key 占位、Demo 开关。

对应《任务整理.md》第八节 Monitor 的频率设计：
- 天气：30 分钟轮询
- 交通：5 分钟轮询
- 景点：到达前 20 分钟查看
- 餐厅：到达前 30 分钟查看
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass
class PollingConfig:
    """各类事件的轮询间隔（秒）。"""
    weather_interval_s: int = 30 * 60      # 天气：30 分钟
    traffic_interval_s: int = 5 * 60       # 交通：5 分钟
    scenic_interval_s: int = 15 * 60       # 景点：15 分钟（另有到达前触发）
    food_interval_s: int = 30 * 60         # 餐厅：30 分钟（另有到达前触发）


@dataclass
class Settings:
    project_name: str = "TravelAgent"
    version: str = "0.1.0"
    demo_mode: bool = False                 # 比赛 Demo 用 Mock 数据，不调真实 API

    polling: PollingConfig = field(default_factory=PollingConfig)
    scenic_lookahead_min: int = 20         # 景点：到达前 20 分钟查看
    food_lookahead_min: int = 30           # 餐厅：到达前 30 分钟查看

    # 真实 API Key 占位：通过环境变量注入，绝不硬编码
    amap_api_key: str = field(default_factory=lambda: os.environ.get("AMAP_API_KEY", ""))
    qweather_api_key: str = field(default_factory=lambda: os.environ.get("QWEATHER_API_KEY", ""))
    qweather_api_host: str = field(default_factory=lambda: os.environ.get(
        "QWEATHER_API_HOST", ""))  # 不带 https://，如 abc1234xyz.def.qweatherapi.com

    calendar_tz: str = "Asia/Shanghai"

    @property
    def use_real_api(self) -> bool:
        """当前是否应使用真实 API（Demo 关闭且有 Key 时）。

        天气 Tool 只需 qweather_api_key + qweather_api_host；
        地图/交通 Tool 需 amap_api_key（后续接入时启用）。
        """
        return not self.demo_mode and bool(self.qweather_api_key and self.qweather_api_host)


settings = Settings()

# 尝试加载本地配置（不提交到 Git，放真实 API Key）
# 用法：复制 config/local_settings.example.py 为 config/local_settings.py
#       填入真实 Key 后自动生效，删除该文件则回退到 Mock 模式
try:
    from config.local_settings import apply_local_settings
    apply_local_settings(settings)
except ImportError:
    pass  # 无本地配置文件，走默认空值（Mock 模式）
