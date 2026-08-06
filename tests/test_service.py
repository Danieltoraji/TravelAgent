"""服务层测试：app/service.py 全端点覆盖。

使用 fastapi.testclient.TestClient 测试所有 HTTP 端点。
fastapi 未安装时自动 skip 全部测试。
"""

import unittest
from datetime import date

# 检测 fastapi 是否可用
try:
    from fastapi.testclient import TestClient
    _HAS_FASTAPI = True
except ImportError:
    _HAS_FASTAPI = False
    TestClient = None  # type: ignore[assignment]


# 测试用行程时间轴
_TIMELINE_PAYLOAD = {
    "city": "北京",
    "start_date": "2026-08-01",
    "end_date": "2026-08-02",
    "days": [
        {
            "day": 1,
            "date": "2026-08-01",
            "items": [
                {
                    "name": "故宫",
                    "category": "scenic",
                    "arrival": "09:00",
                    "ticket_required": True,
                    "price": 60.0,
                    "queue_min": 20,
                },
                {
                    "name": "全聚德(前门店)",
                    "category": "food",
                    "arrival": "18:00",
                },
            ],
        },
        {
            "day": 2,
            "date": "2026-08-02",
            "items": [
                {
                    "name": "天坛",
                    "category": "scenic",
                    "arrival": "09:00",
                    "ticket_required": True,
                    "price": 15.0,
                    "queue_min": 15,
                },
            ],
        },
    ],
}


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceBase(unittest.TestCase):
    """基础端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        # 重置 state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)

    def test_health(self) -> None:
        r = self.client.get("/health")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "ok")

    def test_list_tools(self) -> None:
        r = self.client.get("/tools")
        self.assertEqual(r.status_code, 200)
        tools = r.json()["tools"]
        self.assertIn("booking", tools)
        self.assertIn("weather", tools)

    def test_invoke_tool_weather(self) -> None:
        r = self.client.post("/tools/weather/invoke", json={"city": "北京"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertIn("status", data)
        self.assertEqual(data["tool"], "weather")

    def test_invoke_unknown_tool_404(self) -> None:
        r = self.client.post("/tools/nosuchtool/invoke", json={})
        self.assertEqual(r.status_code, 404)


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceTimeline(unittest.TestCase):
    """行程时间轴端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)

    def test_get_timeline_without_set_returns_400(self) -> None:
        r = self.client.get("/timeline")
        self.assertEqual(r.status_code, 400)

    def test_set_and_get_timeline(self) -> None:
        # POST 设置时间轴
        r = self.client.post("/timeline", json=_TIMELINE_PAYLOAD)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["timeline"]["city"], "北京")

        # GET 获取时间轴
        r = self.client.get("/timeline")
        self.assertEqual(r.status_code, 200)
        tl = r.json()
        self.assertEqual(tl["city"], "北京")
        self.assertEqual(len(tl["days"]), 2)
        self.assertEqual(tl["days"][0]["items"][0]["name"], "故宫")

    def test_set_timeline_initializes_execution_agent(self) -> None:
        self.assertIsNotNone(self.state.execution_agent)

    def test_set_invalid_timeline_returns_400(self) -> None:
        r = self.client.post("/timeline", json={"city": "test"})
        self.assertEqual(r.status_code, 400)


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceBooking(unittest.TestCase):
    """预约管理端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        # 先设置时间轴（预约需要 scenic Tool，scenic 在 registry 中已注册）
        cls.client.post("/timeline", json=_TIMELINE_PAYLOAD)

    def setUp(self) -> None:
        """每个测试前准备一个预约，避免依赖测试执行顺序。"""
        r = self.client.post("/booking/prepare", json={
            "place": "故宫",
            "target_date": "2026-08-01",
            "party_size": 2,
            "booking_type": "scenic",
        })
        self.assertEqual(r.status_code, 200)
        self.booking_id = r.json()["booking_id"]

    def test_prepare_booking(self) -> None:
        # setUp 已准备了一个预约，验证其字段
        r = self.client.get(f"/booking/{self.booking_id}")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["place"], "故宫")
        self.assertEqual(data["status"], "pending_confirm")
        self.assertEqual(data["booking_type"], "scenic")
        # scenic 自动填充（live API 可能返回 0.0，只验证字段存在）
        self.assertIn("price", data)
        self.assertIn("ticket_required", data)

    def test_get_booking(self) -> None:
        bid = self.booking_id
        r = self.client.get(f"/booking/{bid}")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["booking_id"], bid)

    def test_get_booking_not_found_404(self) -> None:
        r = self.client.get("/booking/NOSUCHID")
        self.assertEqual(r.status_code, 404)

    def test_list_bookings(self) -> None:
        r = self.client.get("/booking")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreaterEqual(data["count"], 1)

    def test_confirm_booking(self) -> None:
        bid = self.booking_id
        r = self.client.post(f"/booking/{bid}/confirm")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "submitted")
        self.assertNotEqual(data["confirm_code"], "")

    def test_confirm_not_found_404(self) -> None:
        r = self.client.post("/booking/NOSUCHID/confirm")
        self.assertEqual(r.status_code, 404)

    def test_cancel_booking(self) -> None:
        # 先准备一个新的预约用于取消
        r = self.client.post("/booking/prepare", json={
            "place": "天坛",
            "target_date": "2026-08-02",
            "party_size": 1,
        })
        bid = r.json()["booking_id"]
        r = self.client.post(f"/booking/{bid}/cancel")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "cancelled")

    def test_payment_action(self) -> None:
        # 先准备一个新的预约用于付款提醒
        r = self.client.post("/booking/prepare", json={
            "place": "故宫",
            "target_date": "2026-08-01",
            "party_size": 3,
        })
        bid = r.json()["booking_id"]
        r = self.client.post(f"/booking/{bid}/payment")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["permission"], "manual")
        self.assertIn("门票", data["title"])


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceActions(unittest.TestCase):
    """Action Queue 端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.post("/timeline", json=_TIMELINE_PAYLOAD)
        # 准备一个预约产生 ActionItem
        r = cls.client.post("/booking/prepare", json={
            "place": "故宫",
            "target_date": "2026-08-01",
            "party_size": 2,
        })
        cls.action_id = f"act-{r.json()['booking_id']}"

    def test_list_actions(self) -> None:
        r = self.client.get("/actions")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertGreaterEqual(data["count"], 1)
        action_ids = [a["action_id"] for a in data["actions"]]
        self.assertIn(self.action_id, action_ids)

    def test_approve_action(self) -> None:
        r = self.client.post(f"/actions/{self.action_id}/approve")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "approved")

    def test_reject_action(self) -> None:
        # 先准备另一个预约
        r = self.client.post("/booking/prepare", json={
            "place": "天坛",
            "target_date": "2026-08-02",
            "party_size": 1,
        })
        aid = f"act-{r.json()['booking_id']}"
        r = self.client.post(f"/actions/{aid}/reject")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "rejected")

    def test_approve_not_found_404(self) -> None:
        r = self.client.post("/actions/nosuchaction/approve")
        self.assertEqual(r.status_code, 404)


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceEvents(unittest.TestCase):
    """监控事件端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.post("/timeline", json=_TIMELINE_PAYLOAD)

    def test_get_events_initial(self) -> None:
        """setUpClass 后 events 已清空，但 poll 之前可能无事件。"""
        r = self.client.get("/events")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        # setUpClass 清空了 events，此时 total 可能为 0 或有其他测试残留
        # 只验证端点正常返回
        self.assertIn("events", data)
        self.assertIn("total", data)

    def test_poll_produces_events(self) -> None:
        r = self.client.post("/execution/poll")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["count"], 0)

        # 事件应出现在 /events
        r = self.client.get("/events")
        data = r.json()
        self.assertGreater(data["total"], 0)

    def test_events_since_incremental(self) -> None:
        # 先获取当前总数
        r = self.client.get("/events")
        total_before = r.json()["total"]

        # 再 poll 一次
        self.client.post("/execution/poll")

        # 增量查询
        r = self.client.get(f"/events?since={total_before}")
        data = r.json()
        self.assertGreater(data["count"], 0)

    def test_poll_without_timeline_400(self) -> None:
        from app.service import create_app, state
        # 临时清除 timeline
        old_agent = state.execution_agent
        old_timeline = state.timeline
        state.execution_agent = None
        state.timeline = None
        try:
            app = create_app()
            client = TestClient(app)
            r = client.post("/execution/poll")
            self.assertEqual(r.status_code, 400)
        finally:
            state.execution_agent = old_agent
            state.timeline = old_timeline


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceAutoBooking(unittest.TestCase):
    """自动预约集成测试：lookahead 触发后自动产出 ActionItem。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.post("/timeline", json=_TIMELINE_PAYLOAD)

    def test_lookahead_auto_books(self) -> None:
        """POST /execution/lookahead 触发后，GET /actions 有自动预约。"""
        # 故宫 09:00 到达，提前 20min = 08:40；用 08:45 触发
        r = self.client.post("/execution/lookahead", json={"now": "2026-08-01T08:45:00"})
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertGreater(data["count"], 0)

        # 检查 /actions 有自动预约的 ActionItem
        r = self.client.get("/actions")
        self.assertEqual(r.status_code, 200)
        actions = r.json()["actions"]
        self.assertGreater(len(actions), 0)
        # 应包含故宫的预约
        titles = [a["title"] for a in actions]
        self.assertTrue(any("故宫" in t for t in titles))

        # 检查 /booking 有记录
        r = self.client.get("/booking")
        self.assertEqual(r.status_code, 200)
        bookings = r.json()["bookings"]
        places = [b["place"] for b in bookings]
        self.assertIn("故宫", places)


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceExport(unittest.TestCase):
    """导出端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)
        cls.client.post("/timeline", json=_TIMELINE_PAYLOAD)

    def test_export_ics(self) -> None:
        r = self.client.get("/export/ics")
        self.assertEqual(r.status_code, 200)
        content = r.text
        self.assertIn("BEGIN:VCALENDAR", content)
        self.assertIn("故宫", content)
        self.assertIn("END:VCALENDAR", content)

    def test_export_markdown(self) -> None:
        r = self.client.get("/export/markdown")
        self.assertEqual(r.status_code, 200)
        content = r.text
        self.assertIn("# 北京 行程单", content)
        self.assertIn("故宫", content)
        self.assertIn("天坛", content)

    def test_export_without_timeline_400(self) -> None:
        from app.service import create_app, state
        old_timeline = state.timeline
        state.timeline = None
        state.execution_agent = None
        try:
            app = create_app()
            client = TestClient(app)
            r = client.get("/export/ics")
            self.assertEqual(r.status_code, 400)
        finally:
            state.timeline = old_timeline


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestServiceConfigReload(unittest.TestCase):
    """配置热更新端点测试。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        state.booking_manager = type(state.booking_manager)(state.registry)
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)

    def test_config_reload(self):
        """POST /config/reload 应返回当前配置状态。"""
        r = self.client.post("/config/reload")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        self.assertIn("demo_mode", data)
        self.assertIn("use_real_api", data)
        self.assertIn("use_real_map_api", data)


@unittest.skipUnless(_HAS_FASTAPI, "fastapi not installed")
class TestTimelineActivitiesKey(unittest.TestCase):
    """验证 POST /timeline 接受 activities 键作为 items 的替代。"""

    @classmethod
    def setUpClass(cls) -> None:
        from app.service import create_app, state
        state.timeline = None
        state.execution_agent = None
        state.events.clear()
        cls.state = state
        cls.app = create_app()
        cls.client = TestClient(cls.app)

    def test_timeline_accepts_activities_key(self) -> None:
        """用 activities 代替 items，端点应正常解析。"""
        payload = {
            "city": "北京",
            "start_date": "2026-08-01",
            "end_date": "2026-08-01",
            "days": [
                {
                    "day": 1,
                    "date": "2026-08-01",
                    "activities": [
                        {
                            "id": "BJ_001",
                            "name": "故宫",
                            "category": "scenic",
                            "arrival": "09:00",
                            "end_time": "12:00",
                            "ticket_required": True,
                            "price": 60.0,
                        },
                    ],
                },
            ],
        }
        r = self.client.post("/timeline", json=payload)
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["status"], "ok")
        # 验证解析后的 timeline 包含故宫
        tl = data["timeline"]
        self.assertEqual(tl["city"], "北京")
        items = tl["days"][0]["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "故宫")
        self.assertEqual(items[0]["id"], "BJ_001")
        self.assertEqual(items[0]["end_time"], "12:00")


if __name__ == "__main__":
    unittest.main()
