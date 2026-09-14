"""pytest 共享配置（R5）。

A 侧可选依赖（rapidfuzz/openai，见 requirements.txt）缺失时，
test_a_interface / test_replan_actions 会在导入 a_side 规划链路时炸出
ModuleNotFoundError，表现为一堆"timeline.days 为空"式假失败
（2026-08-28 实际发生，曾误判为代码回归）。

这里在收集期显式跳过这两个模块，把假失败变成带原因的显式 skip。
"""

from __future__ import annotations

collect_ignore: list[str] = []

try:
    import rapidfuzz  # noqa: F401
except ImportError:
    collect_ignore += ["test_a_interface.py", "test_replan_actions.py"]

try:
    import openai  # noqa: F401
except ImportError:
    if "test_a_interface.py" not in collect_ignore:
        collect_ignore += ["test_a_interface.py", "test_replan_actions.py"]


def pytest_configure(config):
    """Django 引导（多用户改造 2026-09）：auth/multiuser/persist 测试需要
    完整 INSTALLED_APPS + sqlite（Bearer 中间件、ORM、django.test.Client）。

    在收集期（任何测试模块 import 前）完成 setup + migrate，使各测试文件里
    ``if not settings.configured: settings.configure(空配置)`` 的旧分支自然
    跳过（Django 全进程只允许 configure 一次）。DB 用独立临时目录（进程号
    命名，互不干扰）；目录用 ``os.makedirs``（0o777）而非
    ``tempfile.mkdtemp``（0o700，见根 conftest 的沙箱说明）。
    """
    import os
    import sys
    import tempfile

    os.environ.setdefault(
        "TRAVELAGENT_DB_PATH",
        os.path.join(
            os.path.join(tempfile.gettempdir(), f"ta_django_{os.getpid()}"),
            "db.sqlite3",
        ),
    )
    os.makedirs(os.path.dirname(os.environ["TRAVELAGENT_DB_PATH"]), exist_ok=True)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for _p in (os.path.join(repo_root, "django_server"), repo_root):
        if _p not in sys.path:
            sys.path.insert(0, _p)

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "travelagent.settings")

    from django.conf import settings

    if settings.configured:
        return
    import django

    django.setup()
    from django.core.management import call_command

    call_command("migrate", run_syncdb=True, verbosity=0)
