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
