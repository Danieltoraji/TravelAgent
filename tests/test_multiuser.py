"""多用户隔离测试（2026-09 多用户改造）。

- 端到端（django.test.Client 全中间件链）：两个注册用户各自 plan/timeline/
  booking 完全隔离——A 写入后 B 视角为空，B 写入不覆盖 A；
- 运行时管理器单元测试：同用户同实例、TTL/LRU 淘汰（stub 注入，不碰 DB）。
"""

from __future__ import annotations

import json
import os
import sys
import time
import unittest
import uuid

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _register_user(client, prefix: str) -> tuple[int, str]:
    """注册一个独立用户，返回 (user_id, token)。"""
    from django.contrib.auth.models import User

    username = f"{prefix}_{uuid.uuid4().hex[:8]}"
    resp = client.post(
        "/api/auth/register/",
        data=json.dumps({"username": username, "password": "secret123"}),
        content_type="application/json",
    )
    assert resp.status_code == 200, resp.content
    token = json.loads(resp.content)["token"]
    return User.objects.get(username=username).id, token


_TIMELINE_A = {
    "city": "北京", "start_date": "2026-10-01", "end_date": "2026-10-01",
    "days": [{"day": 1, "date": "2026-10-01",
              "items": [{"name": "故宫", "arrival": "09:00"}]}],
}
_TIMELINE_B = {
    "city": "上海", "start_date": "2026-10-02", "end_date": "2026-10-02",
    "days": [{"day": 1, "date": "2026-10-02",
              "items": [{"name": "外滩", "arrival": "10:00"}]}],
}


class TestTwoUserIsolation(unittest.TestCase):
    def setUp(self) -> None:
        from django.test import Client

        self.a_client = Client()
        self.b_client = Client()

    def tearDown(self) -> None:
        from django.contrib.auth.models import User

        from runtime.manager import manager

        User.objects.filter(username__startswith="iso_a_").delete()
        User.objects.filter(username__startswith="iso_b_").delete()
        # 用户删除后运行时表里的残留一并清掉。
        # 注意：manager._lock 是非重入 Lock——先在锁内收集、锁外再 drop，
        # 同线程嵌套加锁会死锁（2026-09-12 首跑实测挂死）。
        with manager._lock:
            stale = [uid for uid in manager._entries
                     if not User.objects.filter(id=uid).exists()]
        for uid in stale:
            manager.drop(uid)

    def test_timeline_and_bookings_isolated(self) -> None:
        uid_a, token_a = _register_user(self.a_client, "iso_a")
        uid_b, token_b = _register_user(self.b_client, "iso_b")

        # A：写 timeline + 预约
        resp = self.a_client.post(
            "/api/timeline/", data=json.dumps(dict(_TIMELINE_A)),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token_a}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self.a_client.post(
            "/api/booking/prepare/",
            data=json.dumps({"place": "故宫", "target_date": "2026-10-01",
                             "party_size": 2}),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token_a}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        # B：A 的写入不可见
        self.assertEqual(
            self.b_client.get("/api/timeline/", HTTP_AUTHORIZATION=f"Bearer {token_b}").status_code,
            400,
        )
        body = json.loads(
            self.b_client.get("/api/booking/", HTTP_AUTHORIZATION=f"Bearer {token_b}").content
        )
        self.assertEqual(body["count"], 0)

        # B：写自己的 timeline，不覆盖 A
        resp = self.b_client.post(
            "/api/timeline/", data=json.dumps(dict(_TIMELINE_B)),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token_b}",
        )
        self.assertEqual(resp.status_code, 200, resp.content)

        city_a = json.loads(
            self.a_client.get("/api/timeline/", HTTP_AUTHORIZATION=f"Bearer {token_a}").content
        )["city"]
        city_b = json.loads(
            self.b_client.get("/api/timeline/", HTTP_AUTHORIZATION=f"Bearer {token_b}").content
        )["city"]
        self.assertEqual((city_a, city_b), ("北京", "上海"))

        # 预约也互不可见
        n_a = json.loads(
            self.a_client.get("/api/booking/", HTTP_AUTHORIZATION=f"Bearer {token_a}").content
        )["count"]
        n_b = json.loads(
            self.b_client.get("/api/booking/", HTTP_AUTHORIZATION=f"Bearer {token_b}").content
        )["count"]
        self.assertEqual((n_a, n_b), (1, 0))

        # 运行时确实是两个实例
        from runtime.manager import manager

        self.assertIsNot(manager.peek(uid_a), manager.peek(uid_b))


class TestUserRuntimeManager(unittest.TestCase):
    """manager 纯逻辑：stub 掉 _build（不碰 ORM/注册表）。"""

    def _make_manager(self, max_runtimes: int = 10, ttl_seconds: int = 3600):
        from runtime.manager import UserRuntimeManager

        m = UserRuntimeManager(max_runtimes=max_runtimes, ttl_seconds=ttl_seconds)
        made: list = []
        m._build = lambda user_id: made.append(user_id) or object()  # type: ignore[method-assign]
        return m, made

    def test_same_user_same_instance(self) -> None:
        m, _ = self._make_manager()
        rt1 = m.get(1)
        rt2 = m.get(1)
        self.assertIs(rt1, rt2)

    def test_capacity_eviction_lru(self) -> None:
        m, _ = self._make_manager(max_runtimes=2)
        m.get(1)
        m.get(2)
        m.get(1)                    # 触碰 1 → 2 成为最旧
        m.get(3)                    # 容量满 → 逐出 2
        self.assertIsNone(m.peek(2))
        self.assertIsNotNone(m.peek(1))
        self.assertIsNotNone(m.peek(3))

    def test_ttl_eviction(self) -> None:
        m, _ = self._make_manager(ttl_seconds=60)
        m.get(1)
        m.get(2)
        # 手动把 1 的活跃时间拨回 TTL 之外（不依赖 sleep/时钟精度）
        with m._lock:
            rt1, _ = m._entries[1]
            m._entries[1] = (rt1, time.monotonic() - 3600)
        m.get(3)                    # 1 已过期 → 逐出；容量内保留 2、3
        self.assertIsNone(m.peek(1))
        self.assertIsNotNone(m.peek(2))
        self.assertIsNotNone(m.peek(3))

    def test_persist_without_entry_is_noop(self) -> None:
        m, _ = self._make_manager()
        m.persist(999)              # 不在内存 → 不查库不报错


class TestPollBusy(unittest.TestCase):
    """review 修复（2026-09-13）：poll/lookahead 非阻塞持锁——同用户锁被
    其他线程持有时立即返回 busy，不再占线程干等。

    注意 RLock 同线程可重入：Django test Client 在测试线程内同步执行请求，
    必须用辅助线程持锁才能模拟「另一线程正在 plan」的竞争。
    """

    def test_poll_returns_busy_when_lock_held_elsewhere(self) -> None:
        import threading

        from django.contrib.auth.models import User
        from django.test import Client

        from runtime.manager import manager

        client = Client()
        username = f"busy_{uuid.uuid4().hex[:8]}"
        resp = client.post(
            "/api/auth/register/",
            data=json.dumps({"username": username, "password": "secret123"}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        token = json.loads(resp.content)["token"]
        uid = User.objects.get(username=username).id
        rt = manager.get(uid)

        entered = threading.Event()
        release = threading.Event()

        def holder() -> None:
            with rt.lock:
                entered.set()
                release.wait(5)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        try:
            assert entered.wait(5)
            resp = client.post("/api/execution/poll/",
                               HTTP_AUTHORIZATION=f"Bearer {token}")
            self.assertEqual(resp.status_code, 200)
            body = json.loads(resp.content)
            self.assertEqual(body["status"], "busy")
            self.assertEqual(body["events"], [])
            self.assertEqual(body["count"], 0)
        finally:
            release.set()
            t.join(5)
            manager.drop(uid)
            User.objects.filter(username=username).delete()


if __name__ == "__main__":
    unittest.main()
