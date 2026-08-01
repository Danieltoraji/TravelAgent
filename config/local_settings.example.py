"""本地配置模板：放真实 API Key，不提交到 Git。

用法：
  1. 复制本文件为 config/local_settings.py（注意不是 .example）
  2. 填入你的真实 API Key 和 Host
  3. settings.py 启动时会自动加载，删除该文件则回退到 Mock 模式

获取和风天气 API Key：
  1. 注册 https://id.qweather.com/
  2. 控制台 → 项目管理 → 创建项目 → 添加 API KEY 凭据
  3. 控制台 → 设置 → 查看 API Host（形如 abc1234xyz.def.qweatherapi.com）
"""


def apply_local_settings(settings):
    """覆盖 settings 实例的真实 API 配置。"""
    settings.demo_mode = False
    settings.qweather_api_key = "填写你的和风天气 API Key"
    settings.qweather_api_host = "填写你的 API Host"  # 不带 https://
    # settings.amap_api_key = "高德地图 Key（后续接入时用）"