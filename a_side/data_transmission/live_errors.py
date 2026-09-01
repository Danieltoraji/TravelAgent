"""工具层统一异常（P3 抽取：原定义在 ``live_data.py``，供额度管家复用避免循环 import）。

``live_data.py`` 顶部 re-export（``from data_transmission.live_errors import
LiveDataError``），历史 ``from data_transmission.live_data import LiveDataError``
路径保持可用；``quota_manager.py`` 的 ``QuotaExceeded`` 继承本类，保证
「预算超限 → 该模式该段回落估算」的传播语义与旧 ``_BudgetExceeded`` 一致。
"""

from __future__ import annotations


class LiveDataError(RuntimeError):
    """真源工具层统一异常：工具缺失 / 返回不可解析 / 网络失败一律抛本类。

    由 BPlannerHook 捕获后回退假源（live → fake 降级链），属预期行为。
    """