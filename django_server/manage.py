#!/usr/bin/env python
"""Django 管理入口（单用户 Demo）。

把仓库根目录加入 sys.path，以便 Django 进程能直接 import
core / tools / execution / booking / monitor / decision / itinerary 等 B 侧代码。
"""

import os
import sys

if __name__ == "__main__":
    # django_server/ 的上一级是仓库根目录
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))
    REPO_ROOT = os.path.dirname(BASE_DIR)
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "travelagent.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django 未安装。请先执行: pip install -r django_server/requirements.txt"
        ) from exc
    execute_from_command_line(sys.argv)
