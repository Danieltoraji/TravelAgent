"""ASGI 入口（预留；单用户 Demo 也可用 uvicorn 运行）。"""

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO_ROOT = os.path.dirname(BASE_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "travelagent.settings")

from django.core.asgi import get_asgi_application  # noqa: E402

application = get_asgi_application()
