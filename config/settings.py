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
    demo_mode: bool = field(default_factory=lambda: os.environ.get(
        "DEMO_MODE", "false").lower() in ("1", "true", "yes"))  # 比赛 Demo 用 Mock 数据，不调真实 API

    polling: PollingConfig = field(default_factory=PollingConfig)
    scenic_lookahead_min: int = 20         # 景点：到达前 20 分钟查看
    food_lookahead_min: int = 30           # 餐厅：到达前 30 分钟查看

    # 真实 API Key 占位：通过环境变量注入，绝不硬编码
    amap_api_key: str = field(default_factory=lambda: os.environ.get("AMAP_API_KEY", ""))
    qweather_api_key: str = field(default_factory=lambda: os.environ.get("QWEATHER_API_KEY", ""))
    qweather_api_host: str = field(default_factory=lambda: os.environ.get(
        "QWEATHER_API_HOST", ""))  # 不带 https://，如 abc1234xyz.def.qweatherapi.com
    rollinggo_mcp_url: str = field(default_factory=lambda: os.environ.get(
        "ROLLINGGO_MCP_URL", "https://mcp.rollinggo.cn/mcp"))
    rollinggo_api_key: str = field(default_factory=lambda: os.environ.get(
        "ROLLINGGO_API_KEY", ""))

    calendar_tz: str = "Asia/Shanghai"

    # ── M5 生产化配置 ──────────────────────────────────────────────
    api_timeout: float = 10.0              # API 请求超时（秒）
    max_retries: int = 3                  # 最大重试次数（首次 + 重试）
    retry_backoff_base: float = 1.0       # 指数退避基数（秒），第 n 次重试等待 base * 2^n
    log_dir: str = "logs"                # 日志目录
    log_level: str = "INFO"              # 日志级别
    log_max_bytes: int = 10 * 1024 * 1024  # 单日志文件最大字节数（10MB）
    log_backup_count: int = 5             # 保留日志文件数

    @property
    def use_real_api(self) -> bool:
        """当前是否应使用真实天气 API（Demo 关闭且有 Key 时）。

        天气 Tool 只需 qweather_api_key + qweather_api_host。
        """
        return not self.demo_mode and bool(self.qweather_api_key and self.qweather_api_host)

    @property
    def use_real_map_api(self) -> bool:
        """当前是否应使用真实地图 API（Demo 关闭且有 Key 时）。

        地图 Tool 需 amap_api_key，与天气的 use_real_api 独立判断。
        """
        return not self.demo_mode and bool(self.amap_api_key)

    @property
    def use_real_hotel_api(self) -> bool:
        """当前是否应使用真实酒店 MCP（Demo 关闭且有 Key 时）。"""
        return not self.demo_mode and bool(self.rollinggo_api_key)

    @property
    def use_real_web(self) -> bool:
        """当前是否应使用真实网页抓取（Demo 关闭即启用）。

        网页抓取无需 API Key，只要非 Demo 模式就用真实抓取。
        """
        return not self.demo_mode

    def reload(self) -> None:
        """热更新：重新从环境变量读取 API Key + 重新加载 local_settings.py。

        不重置 polling / lookahead 等运行时配置（避免影响运行中的调度器）。
        """
        self.amap_api_key = os.environ.get("AMAP_API_KEY", "")
        self.qweather_api_key = os.environ.get("QWEATHER_API_KEY", "")
        self.qweather_api_host = os.environ.get("QWEATHER_API_HOST", "")
        self.rollinggo_mcp_url = os.environ.get(
            "ROLLINGGO_MCP_URL", "https://mcp.rollinggo.cn/mcp")
        self.rollinggo_api_key = os.environ.get("ROLLINGGO_API_KEY", "")
        try:
            from config.local_settings import apply_local_settings
            apply_local_settings(self)
        except ImportError:
            pass


settings = Settings()

# 尝试加载本地配置（不提交到 Git，放真实 API Key）
# 用法：复制 config/local_settings.example.py 为 config/local_settings.py
#       填入真实 Key 后自动生效，删除该文件则回退到 Mock 模式
try:
    from config.local_settings import apply_local_settings
    apply_local_settings(settings)
except ImportError:
    pass  # 无本地配置文件，走默认空值（Mock 模式）
