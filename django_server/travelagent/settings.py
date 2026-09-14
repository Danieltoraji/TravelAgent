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
