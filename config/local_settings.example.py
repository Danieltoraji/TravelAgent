"""本地配置模板：放真实 API Key，不提交到 Git。

用法：
  1. 复制本文件为 config/local_settings.py（注意不是 .example）
  2. 填入你的真实 API Key 和 Host
  3. settings.py 启动时会自动加载，删除该文件则回退到 Mock 模式

获取和风天气 API Key：
  1. 注册 https://id.qweather.com/
  2. 控制台 → 项目管理 → 创建项目 → 添加 API KEY 凭据
  3. 控制台 → 设置 → 查看 API Host（形如 abc1234xyz.def.qweatherapi.com）

获取高德地图 API Key：
  1. 注册 https://lbs.amap.com/
  2. 控制台 → 应用管理 → 创建新应用 → 添加 Key（选择 Web 服务）
"""


def apply_local_settings(settings):
    """覆盖 settings 实例的真实 API 配置。"""
    settings.demo_mode = False
    settings.qweather_api_key = "填写你的和风天气 API Key"
    settings.qweather_api_host = "填写你的 API Host"  # 不带 https://
    settings.amap_api_key = "填写你的高德地图 Key"

    # M5 生产化配置（可选，以下为默认值）
    # settings.api_timeout = 10.0          # API 请求超时（秒）
    # settings.max_retries = 3            # 最大重试次数
    # settings.retry_backoff_base = 1.0   # 指数退避基数（秒）
    # settings.log_dir = "logs"           # 日志目录
    # settings.log_level = "INFO"         # 日志级别