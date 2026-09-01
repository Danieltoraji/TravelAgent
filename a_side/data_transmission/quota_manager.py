"""额度管家 QuotaManager（架构整理方案 P3：``_BudgetedToolProvider`` 泛化）。

把「真源调用纪律」从 make_live_intercity_provider 内联逻辑中抽出为工具层
公共组件——**全部真源调用都过它**（P3-D1 组装入口收敛：BPlannerHook 构造处
包一层无预算 QuotaManager，spots/eta/矩阵/酒店/餐厅/城际全部经它计数；
预算语义仍由 make_live_intercity_provider 内层 QuotaManager 承载）：

1. **per-mode 预算**：按工具名计数，达上限抛 ``QuotaExceeded``（额度纪律：
   超限该模式走估算，不超额硬查）。``stats`` 为可选外部 dict，实时记录各
   工具累计调用数（探针/验收用）。计数先自增再放行（**失败也计费**，与旧
   ``_BudgetedToolProvider`` 语义一致——防失败重试打空预算窗口）。
2. **节律间隔**：相邻真源调用间隔 ≥ ``unbudgeted_pace`` 秒（默认 0.35，
   AGENTS.md「免费 key 查询循环加 0.3~0.4s 间隔」纪律；8.31 贵港→北京
   实测：无间隔连发触发 12306 限流风暴 → 候选全 None → 静默 driving 兜底）。
3. **调用计数**：``stats`` 可观察（探针/验收），全部真源调用计数（含无预算
   工具），是**全量真源调用账本**。
4. **``(name, o, d, date)`` 缓存**（P3-D2a）：可选 ``cache`` dict，主链城际
   真源查询（train_trip/train_ticket/flight_search）经 ``cached_call`` 自动
   共享——**命中不计数、正负都缓存**（同对同日期只查一次真源；None 结果
   同样缓存，BFS 重复展开同一无班次对不再反复查询）。

双通道设计（行为与现状完全等价）：
- ``call(name, **kwargs)``——**预算计数通道**（主链路直达 / BFS / 航班验证
  用；超限抛 ``QuotaExceeded`` 由各子 provider 的异常兜底转 None/估算）；
- ``call_unbudgeted(name, **kwargs)``——**穿透通道**（候选生成器专用：自带
  总量纪律 MAX_TRAIN_CALLS=24 + 同对缓存，与主链路共享 per-mode 预算会互相
  饿死——8.30 demo1 返程实测双杀）；带节律但不计数、不预算。

旧 ``train_edge_unbudgeted`` 语义由 ``unbudgeted()`` 代理保持（见下）。

为什么候选生成通道必须穿透预算（2026-08-30/31 教训）：
``generate_intercity_candidates`` 对 AirIn(北京) 24 个邻居连续查询
（train_trip + train_ticket 双通道），若与主链路共享 train 预算，去程邻居
查询吃光 6 次预算后返程方向 train 级全跳过 → 只剩 flight 0 条 → 退化
driving。候选生成器的纪律是它自己的 ``MAX_TRAIN_CALLS``，不归本管家管。
"""

from __future__ import annotations

import time
from typing import Any, Callable, Dict, Optional

from data_transmission.live_errors import LiveDataError

logger = None  # 惰性初始化，避免模块加载期抢锁


def _log() -> Any:
    global logger
    if logger is None:
        import logging

        logger = logging.getLogger("data_transmission.quota_manager")
    return logger


class QuotaExceeded(LiveDataError):
    """工具调用达预算上限：超限该模式走估算，不超额硬查（额度纪律）。"""


class QuotaManager:
    """按工具名计数的 tool_provider 包装 + 节律 + 统计 + 城际查询缓存。

    用法：把 ``mode_budget`` 传入构造；BFS 一次规划内 train_trip /
    train_ticket / flight_search 各 ≤6 次（交接文档 §4 额度纪律），超限被
    各子 provider 的异常兜底转成 None/LiveDataError → 该段回落估算。
    ``stats`` 为可选外部 dict，实时记录各工具累计调用数（探针/验收用）。
    ``unbudgeted_pace`` 为穿透通道的节律间隔（默认 0.35s）。
    ``cache``（P3-D2a）为可选外部 dict：传了才启用 ``cached_call`` 的
    ``(name, o, d, date)`` 缓存（缺省 None = 不缓存，行为零变化）。
    """

    def __init__(
        self,
        inner: Any,
        budget: Optional[Dict[str, int]] = None,
        stats: Optional[Dict[str, int]] = None,
        unbudgeted_pace: float = 0.35,
        cache: Optional[Dict[Any, Any]] = None,
    ):
        self._inner = inner
        self._budget = dict(budget or {})
        self._stats = stats if stats is not None else {}
        self._unbudgeted_pace = unbudgeted_pace
        self._cache = cache
        # 穿透通道的共享节律时钟（跨去程/返程同一次生成共用，与旧
        # _train_query_pace 的 _last_train_query_at 语义一致）
        self._last_unbudgeted_at = 0.0

    # ------------------------------------------------------------------
    # 预算计数通道（主链路）
    # ------------------------------------------------------------------

    def call(self, name: str, **kwargs: Any) -> Any:
        # 所有真源调用都计数（stats 可观察，探针/验收用）；仅当该工具配了
        # 预算上限（budget[name]）且已用满时才抛 QuotaExceeded。计数先自增
        # 再放行（失败也计费，与旧 _BudgetedToolProvider 语义一致——防失败
        # 重试打空预算窗口）。未配预算的工具计数不限流（组装入口收敛后
        # 主链路所有调用都过管家，stats 即全量真源调用账本）。
        used = self._stats.get(name, 0)
        cap = self._budget.get(name)
        if cap is not None and used >= cap:
            raise QuotaExceeded(
                f"{name} 调用达上限 {cap}（额度纪律：超出该模式走估算）"
            )
        self._stats[name] = used + 1
        return self._inner.call(name, **kwargs)

    # ------------------------------------------------------------------
    # 城际查询缓存（P3-D2a：主链 train/flight 真源自动共享）
    # ------------------------------------------------------------------

    def cached_call(self, name: str, **kwargs: Any) -> Any:
        """``(name, o, d, date)`` 键缓存查询：命中不计数，未命中调 ``call`` 后缓存。

        键要素从 kwargs 提取（from_city/from_station → o，to_city/to_station → d，
        date → date）——主链城际真源查询（train_trip/train_ticket/flight_search）
        经此调用：同对同日期只查一次真源（正负都缓存：None 结果同样缓存，BFS
        重复展开同一无班次对不再反复查询；后续不重复消耗预算/额度）。
        键要素不全（如缺 date）或未配置 ``cache`` → 退化为直接 ``call``
        （行为零变化）。kwargs 原样透传给 ``call``，真实工具参数不受影响。
        """
        if self._cache is not None:
            o = kwargs.get("from_city") or kwargs.get("from_station") or ""
            d = kwargs.get("to_city") or kwargs.get("to_station") or ""
            date = kwargs.get("date") or ""
            if o and d and date:
                key = (name, o, d, date)
                if key in self._cache:
                    return self._cache[key]
                result = self.call(name, **kwargs)
                self._cache[key] = result
                return result
        return self.call(name, **kwargs)

    # ------------------------------------------------------------------
    # 穿透通道（候选生成器专用：节律但无预算）
    # ------------------------------------------------------------------

    def call_unbudgeted(self, name: str, **kwargs: Any) -> Any:
        self._pace_unbudgeted()
        return self._inner.call(name, **kwargs)

    def _pace_unbudgeted(self) -> None:
        wait = self._unbudgeted_pace - (time.monotonic() - self._last_unbudgeted_at)
        if wait > 0:
            time.sleep(wait)
        self._last_unbudgeted_at = time.monotonic()

    def unbudgeted(self) -> Any:
        """返回穿透代理：``.call(name, **kwargs)`` → 本管家的 ``call_unbudgeted``。

        用于替代旧 ``intercity_provider._inner`` 裸穿透——候选生成器用它构造
        provider 时，工具调用走穿透通道（节律、无预算、不计数）。
        """
        return _UnbudgetedProxy(self)

    # ------------------------------------------------------------------
    # 只读视图
    # ------------------------------------------------------------------

    @property
    def stats(self) -> Dict[str, int]:
        return self._stats

    @property
    def budget(self) -> Dict[str, int]:
        return dict(self._budget)

    @property
    def cache(self) -> Optional[Dict[Any, Any]]:
        return self._cache

    @property
    def inner(self) -> Any:
        """原始 tool_provider（兼容旧 ``provider._inner`` 访问，P3-D 收尾后移除）。"""
        return self._inner


class _UnbudgetedProxy:
    """把 ``.call`` 转发到 QuotaManager.call_unbudgeted 的薄代理。"""

    __slots__ = ("_quota",)

    def __init__(self, quota: QuotaManager):
        self._quota = quota

    def call(self, name: str, **kwargs: Any) -> Any:
        return self._quota.call_unbudgeted(name, **kwargs)


def make_quota_manager(
    tool_provider: Any,
    mode_budget: Optional[Dict[str, int]] = None,
    stats: Optional[Dict[str, int]] = None,
    unbudgeted_pace: float = 0.35,
    cache: Optional[Dict[Any, Any]] = None,
) -> QuotaManager:
    """工厂：包一层 QuotaManager（budget 缺省 None 表示不限额）。

    ``cache``（P3-D2a）缺省 None 表示不启用城际查询缓存；传入 dict 则
    ``cached_call`` 的 ``(name, o, d, date)`` 缓存生效。
    """
    return QuotaManager(
        tool_provider,
        budget=mode_budget,
        stats=stats,
        unbudgeted_pace=unbudgeted_pace,
        cache=cache,
    )