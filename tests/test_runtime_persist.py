"""Trip 持久化与重启恢复测试（多用户改造 2026-09 M2）。

- ``AgentRuntime.snapshot()``：JSON 安全（date/datetime/Enum 归一）、字段完整；
- ``BookingManager.snapshot()/restore_snapshot()``：往返一致（纯 B 侧，不依赖 Django）；
- ``UserRuntimeManager.persist/_restore``：Trip 行整行覆写 + 懒重建
  （timeline/requirement/events/replans/booking_state 全量恢复）——
  即"进程重启后用户首次请求恢复会话"的核心路径。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import uuid
from datetime import datetime

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from core.schemas import EventType, MonitorEvent  # noqa: E402
from runtime.agent_runtime import AgentRuntime  # noqa: E402

_TIMELINE_PAYLOAD = {
    "city": "北京", "start_date": "2026-10-01", "end_date": "2026-10-02",
    "days": [
        {"day": 1, "date": "2026-10-01",
         "items": [{"name": "故宫", "arrival": "09:00"}]},
        {"day": 2, "date": "2026-10-02",
         "items": [{"name": "颐和园", "arrival": "10:00"}]},
    ],
}


def _fresh_runtime() -> AgentRuntime:
    return AgentRuntime()


class TestSnapshotJsonSafe(unittest.TestCase):
    def test_timeline_snapshot_roundtrip_json(self) -> None:
        rt = _fresh_runtime()
        rt.set_timeline_from_payload(dict(_TIMELINE_PAYLOAD))
        snap = rt.snapshot()
        # date 对象已归一（JSONField 用标准 json，不归一会在落库时炸）
        json.dumps(snap, ensure_ascii=False)
        self.assertEqual(snap["timeline"]["city"], "北京")
        self.assertEqual(snap["timeline"]["days"][0]["date"], "2026-10-01")
        # 满房循环终结（2026-09-16）只增键 failed_hotels（空会话恒空列表）
        self.assertEqual(
            snap["booking_state"], {"records": [], "actions": [], "failed_hotels": []}
        )

    def test_event_snapshot_json_safe(self) -> None:
        rt = _fresh_runtime()
        rt.events.append(MonitorEvent(
            event_id="evt-1",
            event_type=EventType.WEATHER,
            place="北京",
            observed_at=datetime(2026, 10, 1, 9, 0, 0),
            rule_name="t",
            spot_id="",
            data={"rain_probability": 85},
        ))
        snap = rt.snapshot()
        json.dumps(snap, ensure_ascii=False)   # Enum/datetime 归一后可序列化
        self.assertEqual(snap["events"][0]["event_type"], "weather")
        self.assertEqual(snap["events"][0]["observed_at"], "2026-10-01T09:00:00")


class TestBookingSnapshot(unittest.TestCase):
    def test_booking_snapshot_roundtrip(self) -> None:
        from booking.booking_manager import BookingManager

        src = BookingManager()   # 无 persist_path：纯内存
        rec = src.prepare(place="故宫", target_date="2026-10-01",
                          party_size=2, booking_type="scenic")
        snap = src.snapshot()
        json.dumps(snap)         # 形态即文件后端格式，JSON 安全

        dst = BookingManager()
        dst.restore_snapshot(snap)
        self.assertEqual(len(dst.records()), 1)
        restored = dst.get(rec.booking_id)
        self.assertEqual(restored.place, "故宫")
        self.assertEqual(restored.status.value, rec.status.value)


class TestTripPersistRestore(unittest.TestCase):
    """manager.persist → Trip 行 → _restore 全量恢复（重启恢复核心路径）。"""

    def setUp(self) -> None:
        from django.contrib.auth.models import User

        self.user = User.objects.create_user(
            username=f"persist_{uuid.uuid4().hex[:8]}", password="x")
        from runtime.manager import UserRuntimeManager

        self.manager = UserRuntimeManager()

    def tearDown(self) -> None:
        self.user.delete()

    def test_persist_creates_row_and_restore_rebuilds(self) -> None:
        from api.models import Trip

        rt1 = _fresh_runtime()
        rt1.set_timeline_from_payload(dict(_TIMELINE_PAYLOAD))
        rt1.requirement = {"content": {"destination": "北京", "days": 2}}
        rt1.booking_manager.prepare(place="故宫", target_date="2026-10-01",
                                    party_size=2, booking_type="scenic")
        rt1.events.append(MonitorEvent(
            event_id="evt-1", event_type=EventType.WEATHER, place="北京",
            observed_at=datetime(2026, 10, 1, 9, 0, 0),
            rule_name="t", spot_id="", data={},
        ))
        rt1.replan_history.append({"id": "replan-1", "decision": {"reason": "r"}})

        uid = self.user.id
        self.manager._entries[uid] = (rt1, time.monotonic())
        self.manager.persist(uid)

        # Trip 行落库且 JSON 完整
        trip = Trip.objects.get(user_id=uid)
        self.assertEqual(trip.timeline["city"], "北京")
        self.assertEqual(trip.requirement["content"]["destination"], "北京")
        self.assertEqual(trip.events[0]["event_type"], "weather")
        self.assertEqual(len(trip.booking_state["records"]), 1)

        # 模拟进程重启：全新运行时 + _restore
        rt2 = _fresh_runtime()
        self.assertTrue(self.manager._restore(rt2, trip))
        self.assertEqual(rt2.timeline.city, "北京")
        self.assertEqual(len(rt2.timeline.days), 2)
        self.assertEqual(rt2.requirement["content"]["destination"], "北京")
        self.assertEqual(len(rt2.events), 1)
        self.assertEqual(rt2.replan_history[0]["id"], "replan-1")
        self.assertEqual(len(rt2.booking_manager.records()), 1)
        # agent 已重建（监控规则可继续 poll）；决策 hook 同步重建——重启安全性
        # 由 A 侧 BDecisionHook 的 _current_plan 逆向重建保证（首次 replan 从
        # req.current_timeline 重建），不依赖持久化
        self.assertIsNotNone(rt2.agent)
        self.assertIsNotNone(rt2._decision_hook)

    def test_restore_broken_trip_falls_back_to_fresh(self) -> None:
        from api.models import Trip

        Trip.objects.create(user=self.user, timeline={"days": "not-a-list"})
        trip = Trip.objects.get(user_id=self.user.id)
        rt = _fresh_runtime()
        # timeline 形状损坏 → _restore 整体放弃（False），运行时保持可用空态
        self.assertFalse(self.manager._restore(rt, trip))
        self.assertIsNone(rt.timeline)


if __name__ == "__main__":
    unittest.main()
