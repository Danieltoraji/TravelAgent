import os

GLM_API_KEY = os.environ.get("GLM_API_KEY")
GLM_BASE_URL = os.environ.get(
    "GLM_BASE_URL",
    "https://llmapi.paratera.com/v1/",
)
GLM_MODEL = os.environ.get("GLM_MODEL", "GLM-5.2")

# DeepSeek 官方 API（https://api.deepseek.com）
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY")
DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")

# 统一切换大模型提供商：glm / deepseek
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "deepseek").strip().lower()

if LLM_PROVIDER == "deepseek":
    _required_key = DEEPSEEK_API_KEY
    _required_name = "DEEPSEEK_API_KEY"
else:
    _required_key = GLM_API_KEY
    _required_name = "GLM_API_KEY"

if not _required_key:
    raise RuntimeError(f"缺少环境变量 {_required_name}（LLM_PROVIDER={LLM_PROVIDER}）")
