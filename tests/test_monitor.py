"""Monitor Scheduler 测试：事件产出、异步调度。"""

import asyncio
import unittest

from core.schemas import EventType, MonitorEvent
from monitor.monitor_scheduler import MonitorRule, MonitorScheduler


class TestMonitorEmit(unittest.TestCase):
    def test_emit_event_fields(self) -> None:
        sched = MonitorScheduler()
        rule = MonitorRule(name="w", event_type=EventType.WEATHER,
                           interval_s=1, call=lambda: {"a": 1})
        ev = sched.emit(rule, {"a": 1})
        self.assertIsInstance(ev, MonitorEvent)
        self.assertEqual(ev.event_type, EventType.WEATHER)
        self.assertEqual(ev.rule_name, "w")
        self.assertTrue(ev.event_id.startswith("weather-"))

    def test_event_ids_are_unique(self) -> None:
        sched = MonitorScheduler()
        rule = MonitorRule(name="w", event_type=EventType.WEATHER,
                           interval_s=1, call=lambda: {"a": 1})
        ids = {sched.emit(rule, {}).event_id for _ in range(5)}
        self.assertEqual(len(ids), 5)


class TestMonitorAsync(unittest.TestCase):
    def test_scheduler_runs_rules(self) -> None:
        seen: list = []
        sched = MonitorScheduler(max_ticks=1)  # 每个规则只触发 1 次，测试快速结束
        sched.register(MonitorRule(name="t", event_type=EventType.TRAFFIC,
                                   interval_s=0, call=lambda: {"delay_min": 0}))

        async def main() -> None:
            sched.start(lambda ev: seen.append(ev))
            await asyncio.sleep(0.1)
            await sched.stop()

        asyncio.run(main())
        self.assertGreaterEqual(len(seen), 1)
        self.assertEqual(seen[0].event_type, EventType.TRAFFIC)


if __name__ == "__main__":
    unittest.main()
