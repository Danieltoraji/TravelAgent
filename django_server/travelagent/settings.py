"""Django 单用户 Demo 配置。"""

import os
import sys

# 把仓库根目录加入 sys.path，保证 core/tools/execution 等 B 侧包可被 import。
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BASE_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# AB 合码方案 §二铁律 2：a_side 用 append 不用 insert——B 的模块永远优先，
# A 的顶层导入（from algorithoms.xxx 等）仍可解析；禁止新增顶层 config 模块遮蔽 B 的 config/ 包。
A_SIDE_ROOT = os.path.join(REPO_ROOT, "a_side")
if A_SIDE_ROOT not in sys.path:
    sys.path.append(A_SIDE_ROOT)

SECRET_KEY = "django-insecure-travelagent-demo"
DEBUG = True
ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.contenttypes",
    "django.contrib.auth",
    "api",
]

MIDDLEWARE = [
    "django.middleware.common.CommonMiddleware",
    "api.middleware.TokenAuthRuntimeMiddleware",
]

ROOT_URLCONF = "travelagent.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [],
        },
    },
]

WSGI_APPLICATION = "travelagent.wsgi.application"
ASGI_APPLICATION = "travelagent.asgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        # 多用户改造（2026-09）：路径可注入（docker-compose 挂 named volume
        # 到 /app/django_server/data/，容器重建数据不丢）；测试用临时文件。
        "NAME": os.environ.get("TRAVELAGENT_DB_PATH")
        or os.path.join(BASE_DIR, "db.sqlite3"),
        # gthread 多线程下 sqlite 偶发锁竞争，放宽默认 5s 等待
        "OPTIONS": {"timeout": 20},
    }
}

# 多用户改造（2026-09）：Bearer token 认证 + 每用户运行时隔离，
# 见 api/middleware.py 与 docs/sync_notes_multiuser_for_ac_*.md。
LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

# ── 服务端日志（server_log 2026-09-15）────────────────────────────────────
# stdout（docker logs）+ 轮转文件双通道：文件落在 data/logs/（容器内与
# db.sqlite3 同一 named volume，宿主机可直查），10MB×5 自动轮转。
# TRAVELAGENT_LOG_DISABLE=1 关闭文件通道（测试用）；
# TRAVELAGENT_LOG_DIR 覆盖目录。根 logger 收口：api/runtime/a_side 的
# 命名 logger 与未处理异常（django.request ERROR）统一落双通道，
# 每条带 api.middleware 生成的 request_id（见 api/logging_utils.py）。
_LOG_DIR = os.environ.get("TRAVELAGENT_LOG_DIR") or os.path.join(
    BASE_DIR, "data", "logs"
)
_LOG_HANDLERS = ["console"]
_LOG_HANDLERS_CONF = {
    "console": {
        "class": "logging.StreamHandler",
        "filters": ["request_id"],
        "formatter": "verbose",
    },
}
if not os.environ.get("TRAVELAGENT_LOG_DISABLE"):
    # dictConfig 会实例化 handlers 段里的全部条目（不论是否被 logger 引用），
    # 故禁用文件通道时必须整个不定义，而非仅从 root 摘掉
    os.makedirs(_LOG_DIR, exist_ok=True)
    _LOG_HANDLERS.append("file")
    _LOG_HANDLERS_CONF["file"] = {
        "class": "logging.handlers.RotatingFileHandler",
        "filename": os.path.join(_LOG_DIR, "server.log"),
        "maxBytes": 10 * 1024 * 1024,
        "backupCount": 5,
        "encoding": "utf-8",
        "filters": ["request_id"],
        "formatter": "verbose",
    }

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {
        "request_id": {"()": "api.logging_utils.RequestIdFilter"},
    },
    "formatters": {
        "verbose": {
            "format": "{asctime} {levelname} {name} [{request_id}] {message}",
            "style": "{",
        },
    },
    "handlers": _LOG_HANDLERS_CONF,
    "root": {"handlers": _LOG_HANDLERS, "level": "INFO"},
    "loggers": {
        # runserver 专用（gunicorn 部署走自身 access log）；不配置会因
        # 替换默认 LOGGING 而失去请求行输出
        "django.server": {
            "handlers": _LOG_HANDLERS,
            "level": "INFO",
            "propagate": False,
        },
    },
}
