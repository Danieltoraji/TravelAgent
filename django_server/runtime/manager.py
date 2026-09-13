"""多用户运行时管理器（2026-09 多用户改造 M1/M2）。

每用户一个 AgentRuntime（内存实例），user_id → runtime 由本表持有：

- **懒创建**：该用户首次请求时新建空运行时；若 Trip 行存在（此前规划过）
  则先按快照重建（timeline/requirement/events/replans/booking）——进程重启
  后用户无感恢复。决策 hook 无需恢复：BDecisionHook 首次 replan 会从
  ``req.current_timeline`` 逆向重建 ``_current_plan``（A 侧既有机制）。
- **TTL/LRU 淘汰**：内存无界防护（默认 2h 不活跃或超 100 个）；
  淘汰只丢内存，真相在 Trip 行，下次请求自动重建。
- **线程模型**：gunicorn 单 worker + gthread 多线程。写操作由每运行时的
  ``runtime.lock``（RLock）串行化（视图层持有）；本类全局锁只护字典本身，
  构建运行时（可能读 DB）在全局锁外做，竞态时后建者丢弃。

持久化收口：视图层不感知 DB——中间件在 POST 响应后调用 ``persist()``，
从 ``AgentRuntime.snapshot()`` 取快照整行覆写 Trip。快照失败只记日志
（内存态始终可用，持久化是尽力而为）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from runtime.agent_runtime import AgentRuntime

logger = logging.getLogger("runtime.manager")

DEFAULT_MAX_RUNTIMES = 100
DEFAULT_TTL_SECONDS = 2 * 60 * 60


class UserRuntimeManager:
    def __init__(
        self,
        max_runtimes: int = DEFAULT_MAX_RUNTIMES,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> None:
        self._entries: "OrderedDict[int, Tuple[AgentRuntime, float]]" = OrderedDict()
        self._lock = threading.Lock()
        self._max = max_runtimes
        self._ttl = ttl_seconds

    # -- 获取 / 构建 --------------------------------------------------------

    def get(self, user_id: int) -> AgentRuntime:
        """取该用户的运行时；无则构建（空或从 Trip 重建），带 LRU 触碰。"""
        now = time.monotonic()
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                self._entries.move_to_end(user_id)
                self._entries[user_id] = (entry[0], now)
                self._evict_locked(now)
                return entry[0]

        runtime = self._build(user_id)   # 全局锁外：可能读 DB / 建注册表
        with self._lock:
            entry = self._entries.get(user_id)
            if entry is not None:
                return entry[0]          # 并发竞态：别人先建好，用他的
            self._entries[user_id] = (runtime, time.monotonic())
            self._evict_locked(time.monotonic())
            return runtime

    def peek(self, user_id: int) -> Optional[AgentRuntime]:
        """仅返回已在内存的运行时（不触发构建；测试/巡检用）。"""
        with self._lock:
            entry = self._entries.get(user_id)
            return entry[0] if entry is not None else None

    def drop(self, user_id: int) -> None:
        """显式逐出（测试用；生产走 TTL/LRU）。"""
        with self._lock:
            self._entries.pop(user_id, None)

    def _build(self, user_id: int) -> AgentRuntime:
        runtime = AgentRuntime()
        trip = None
        try:
            from api.models import Trip

            trip = Trip.objects.filter(user_id=user_id).first()
        except Exception:  # noqa: BLE001  DB 不可用时按空运行时降级
            logger.exception("trip load failed for user %s", user_id)
        if trip is not None and self._restore(runtime, trip):
            logger.info("runtime restored from Trip for user %s", user_id)
        return runtime

    @staticmethod
    def _restore(runtime: AgentRuntime, trip: Any) -> bool:
        """Trip blob → 内存态。任何一段损坏都整体放弃（回退空运行时）。"""
        try:
            if trip.requirement:
                runtime.requirement = trip.requirement
            tl_payload = trip.timeline
            if tl_payload:
                runtime.init_timeline(
                    runtime.parse_timeline_payload(tl_payload),
                    record_history=False,   # "initial" 历史条目以快照为准
                )
            if trip.events:
                runtime.events = list(trip.events)          # 已是 to_dict 形态
            if trip.replans:
                runtime.replan_history = list(trip.replans)
            if trip.timeline_history:
                runtime.timeline_history = list(trip.timeline_history)
            if trip.booking_state:
                runtime.booking_manager.restore_snapshot(trip.booking_state)
            return True
        except Exception:  # noqa: BLE001
            logger.exception(
                "runtime restore failed for user %s（回退空运行时）",
                getattr(trip, "user_id", "?"),
            )
            return False

    # -- 持久化 --------------------------------------------------------------

    def persist(self, user_id: int) -> None:
        """运行时快照 → Trip 行（POST 响应后由中间件调用；尽力而为）。

        撕裂快照防护（多用户 review P2，2026-09）：snapshot 须持该用户的
        runtime.lock——否则同用户另一线程正持锁写入（plan/chat 进行中）时
        可能抓到半更新状态（timeline 已换、events 未接上），若恰是最后一次
        写入，重启恢复即丢一致性。带超时 try-acquire：拿不到（长写进行中）
        跳过本轮（下次 POST 补写），也不阻塞响应。
        """
        with self._lock:
            entry = self._entries.get(user_id)
        if entry is None:
            return
        rt = entry[0]
        if not rt.lock.acquire(timeout=0.2):
            logger.debug("persist skipped: user %s runtime busy", user_id)
            return
        try:
            from api.models import Trip

            snapshot = rt.snapshot()
            Trip.objects.update_or_create(
                user_id=user_id,
                defaults={
                    "requirement": snapshot["requirement"],
                    "timeline": snapshot["timeline"],
                    "events": snapshot["events"],
                    "replans": snapshot["replans"],
                    "timeline_history": snapshot["timeline_history"],
                    "booking_state": snapshot["booking_state"],
                },
            )
        except Exception:  # noqa: BLE001
            logger.exception("trip persist failed for user %s", user_id)
        finally:
            rt.lock.release()

    # -- 淘汰 ----------------------------------------------------------------

    def _evict_locked(self, now: float) -> None:
        """先按 TTL 清过期，再按容量从最旧开始清（调用方须持 _lock）。"""
        evicted: List[int] = []
        while self._entries:
            oldest_ts = next(iter(self._entries.values()))[1]
            if len(self._entries) <= self._max and now - oldest_ts <= self._ttl:
                break
            user_id, _ = self._entries.popitem(last=False)
            evicted.append(user_id)
        if evicted:
            logger.info("evicted %d runtime(s): %s", len(evicted), evicted)


manager = UserRuntimeManager()
