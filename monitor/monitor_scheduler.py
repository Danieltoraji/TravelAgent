"""Monitor Scheduler：定时事件调度器。

设计（对应《任务整理.md》第八节 Monitor）：
  - 不用 while 循环轮询，而是注册带各自频率的监控规则；
  - 基于 asyncio，各事件独立调度；
  - 每次触发产出统一的 MonitorEvent，交给上层（Execution Agent / Decision Engine）。

频率设计（config/settings.py）：
  - 天气 30 分钟、交通 5 分钟轮询；
  - 景点/餐厅支持 lookahead（到达前 N 分钟触发），由 ExecutionAgent 计算 fire_at。
"""

from __future__ import annotations

import asyncio
import itertools
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Awaitable, Callable, List, Optional

from core.schemas import EventType, MonitorEvent

logger = logging.getLogger("monitor")


@dataclass
class MonitorRule:
    """一条监控规则。

    call: 同步取数函数（返回 ToolResult.data 结构），每次触发时被调用。
    fire_at: 可选；lookahead 规则的绝对触发时间（由 ExecutionAgent 计算）。
    fired: 是否已触发过（lookahead 规则一次性触发）。
    """
    name: str
    event_type: EventType
    interval_s: float
    call: Callable[[], Any]
    place: str = ""
    lookahead_min: Optional[int] = None
    fire_at: Optional[datetime] = None
    fired: bool = False
    enabled: bool = True


class MonitorScheduler:
    """异步监控调度器：注册规则 → 按各自频率轮询 → 产出 MonitorEvent。"""

    def __init__(self, loop: Optional[asyncio.AbstractEventLoop] = None,
                 max_ticks: Optional[int] = None) -> None:
        self._rules: List[MonitorRule] = []
        self._tasks: List[asyncio.Task] = []
        self._seq = itertools.count(1)
        self._loop = loop
        self._max_ticks = max_ticks          # 测试用：每个规则最多触发次数

    # -- 注册 / 查询 -------------------------------------------------------
    def register(self, rule: MonitorRule) -> MonitorRule:
        self._rules.append(rule)
        return rule

    def rules(self) -> List[MonitorRule]:
        return list(self._rules)

    # -- 事件生产 ----------------------------------------------------------
    def emit(self, rule: MonitorRule, data: Any) -> MonitorEvent:
        """把一次观测打包为 MonitorEvent（同步路径也用它）。"""
        return MonitorEvent(
            event_id=f"{rule.event_type.value}-{next(self._seq):04d}",
            event_type=rule.event_type,
            place=rule.place,
            observed_at=datetime.now(),
            rule_name=rule.name,
            data=data,
        )

    # -- 异步调度 ----------------------------------------------------------
    async def _tick(self, rule: MonitorRule,
                    on_event: Callable[[MonitorEvent], Optional[Awaitable[None]]]) -> None:
        ticks = 0
        while rule.enabled:
            try:
                data = rule.call()
                result = on_event(self.emit(rule, data))
                if result is not None:
                    await result
            except Exception as exc:  # noqa: BLE001
                logger.exception("monitor rule %s failed", rule.name)
                ev = self.emit(rule, {"error": str(exc)})
                # 防护：错误事件回调可能再次抛异常，二次抛会打断整个 _tick 循环
                try:
                    result = on_event(ev)
                    if result is not None:
                        await result
                except Exception:  # noqa: BLE001
                    logger.exception("on_event(error_event) failed for rule %s", rule.name)
            ticks += 1
            if self._max_ticks is not None and ticks >= self._max_ticks:
                break
            await asyncio.sleep(rule.interval_s)

    def start(self, on_event: Callable[[MonitorEvent], Optional[Awaitable[None]]],
              loop: Optional[asyncio.AbstractEventLoop] = None) -> None:
        """启动所有启用规则的独立任务。

        loop: 可选，显式注入事件循环。未注入时取当前运行中的循环
              （run_forever 协程内调用本方法时一定有运行中循环）。
        """
        if loop is not None:
            self._loop = loop
        elif self._loop is None:
            # 注意：用 get_running_loop 而非已弃用的 get_event_loop，
            # 后者在无运行循环时会创建新循环（3.12+ 会 DeprecationWarning/报错）。
            self._loop = asyncio.get_running_loop()
        for rule in self._rules:
            if rule.enabled:
                task = self._loop.create_task(self._tick(rule, on_event))
                self._tasks.append(task)

    async def stop(self) -> None:
        """取消所有调度任务。"""
        for task in self._tasks:
            task.cancel()
        self._tasks.clear()
