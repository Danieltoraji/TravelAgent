"""规划轨迹（plan trace v1，2026-09-16）测试。

单元（TestPlanTraceRecorder / TestTraceDigest）：
- Recorder 步骤序号/时间戳/200 封顶/多线程并发安全/坏输入不抛；
- 敏感参数脱敏（tel → ***）、按工具分派的 result_digest 与通用兜底；
- LLM 片段（A 透传 last_llm_trace / B 自留 parse·chat）→ 步骤展开。

集成（TestPlanTraceAPI，plan 全链路用桩，同 test_plan_archive.py）：
- POST /api/plan/ 响应含有序 trace（parse/plan/enrich 里程碑 + 落锤统计），
  trace.request_id 与 X-Request-ID 一致；
- 失败响应（500）也带部分轨迹；
- GET /api/plan-trace/：idle → running（规划中轮询，无锁）→ done；
- 工具异常调用双留痕（tool_call_log + trace 补盲区）；
- last_llm_trace 缺失时优雅降级（轨迹完整，仅缺候选池 LLM 细节）；
- POST /api/chat/ 响应含 trace（LLM 步 + 工具步）。
"""

from __future__ import annotations

import json
import os
import sys
import threading
import unittest
import uuid
from datetime import date
from unittest import mock

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


# ── 单元：Recorder ──────────────────────────────────────────────────────


class TestPlanTraceRecorder(unittest.TestCase):
    def _rec(self):
        from runtime.plan_trace import PlanTraceRecorder

        return PlanTraceRecorder(request_id="ab12cd34")

    def test_seq_ordered_and_capped(self) -> None:
        from runtime.plan_trace import MAX_STEPS

        rec = self._rec()
        for i in range(MAX_STEPS + 10):
            rec.add_milestone(f"m{i}")
        body = rec.assemble()
        self.assertEqual(body["request_id"], "ab12cd34")
        self.assertEqual(len(body["steps"]), MAX_STEPS)
        self.assertEqual(
            [s["seq"] for s in body["steps"]], list(range(1, MAX_STEPS + 1))
        )

    def test_sensitive_args_masked(self) -> None:
        rec = self._rec()
        rec.add_tool(
            "booking", {"action": "prepare", "tel": "13800138000", "place": "故宫"},
            "ok",
        )
        step = rec.assemble()["steps"][0]
        self.assertNotIn("13800138000", step["args"])
        self.assertIn("***", step["args"])
        self.assertIn("故宫", step["args"])   # 非敏感键值保留（排障关键）

    def test_thread_safety(self) -> None:
        rec = self._rec()

        def worker() -> None:
            for _ in range(20):   # 8×20=160 < MAX_STEPS，避开封顶干扰
                rec.add_milestone("x")

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        steps = rec.assemble()["steps"]
        self.assertEqual(len(steps), 160)
        self.assertEqual(sorted(s["seq"] for s in steps), list(range(1, 161)))

    def test_extend_from_llm_meta(self) -> None:
        rec = self._rec()
        rec.extend_from_llm_meta({
            "model": "deepseek-chat",
            "elapsed_ms": 1200,
            "content_summary": "定制 3 个候选桶",
            "tool_trace": [{"round": 1, "calls": [
                {"name": "train_trip",
                 "arguments": {"from_city": "北京", "tel": "13800138000"},
                 "result": {"status": "ok", "trips": [{"price": 99}]}},
            ]}],
            "reviews": [{"round": 1, "tools": ["train_trip"],
                         "review": "suspicious", "reason": "价格可疑"}],
        }, phase="llm", title="候选池定制（LLM）")
        steps = rec.assemble()["steps"]
        self.assertEqual([s["phase"] for s in steps], ["llm", "data", "review"])
        self.assertEqual(steps[0]["model"], "deepseek-chat")
        # LLM 片段里的工具参数同样脱敏
        self.assertNotIn("13800138000", steps[1]["args"])
        self.assertEqual(steps[1]["tool"], "train_trip")
        self.assertIn("1 个班次", steps[1]["result_digest"])
        self.assertIn("suspicious", steps[2]["title"])

    def test_bad_input_never_raises(self) -> None:
        rec = self._rec()
        rec.add_tool(None, None, "weird-status")
        rec.extend_from_llm_meta("not-a-dict")
        rec.extend_from_llm_meta({"tool_trace": "bad", "reviews": [1, 2]})
        rec.add_milestone("")
        self.assertTrue(rec.assemble()["steps"])

    def test_assemble_json_safe(self) -> None:
        rec = self._rec()
        rec.add_milestone("x", detail={"date": date(2026, 10, 1)})
        body = rec.assemble(plan_meta={"timeline": {"total_cost": 1.5}})
        json.dumps(body, ensure_ascii=False)   # 不抛即通过


class TestTraceDigest(unittest.TestCase):
    def test_dispatch_and_fallback(self) -> None:
        from runtime.plan_trace import result_digest_for

        d = result_digest_for(
            "train_trip", {"trips": [{"price": 73.5}, {"price": 100}]}
        )
        self.assertIn("2 个班次", d)
        self.assertIn("73.5", d)
        d = result_digest_for(
            "weather", {"weather": "晴", "temp_min": 12, "temp_max": 23}
        )
        self.assertIn("晴", d)
        self.assertIn("12", d)
        # 未知工具 / 空列表 → 通用兜底，非空
        self.assertTrue(result_digest_for("unknown_tool", {"foo": 1}))
        self.assertIn("0 条记录", result_digest_for("train_trip", {"trips": []}))


# ── 集成：plan / plan-trace / chat ──────────────────────────────────────


def _plan_body(destination: str = "北京") -> dict:
    return {"content": {"destination": destination, "days": 1,
                        "constraints": {"budget": 3000}}}


def _tiny_timeline():
    from core.schemas import DayPlan, Place, TripTimeline

    return TripTimeline(
        id="tl-trace", city="北京",
        start_date=date(2026, 10, 1), end_date=date(2026, 10, 1),
        days=[DayPlan(day=1, date=date(2026, 10, 1),
                      items=[Place(id="BJ_001", name="故宫", arrival="09:00")])],
    )


def _fake_init_from_requirement(self, payload):
    """plan 桩：记 requirement + 真实小时间轴，跳过真实规划器。

    有意不设置 last_llm_trace/last_data_source → 同时覆盖「A 侧透传缺席时
    优雅降级」路径（getattr 容忍缺失）。
    """
    self.requirement = payload
    self._last_planner_error = None
    timeline = _tiny_timeline()
    self.init_timeline(timeline)
    return timeline


class TestPlanTraceAPI(unittest.TestCase):
    def setUp(self) -> None:
        from django.test import Client

        self.client = Client()
        self._created: list[str] = []
        self._patchers = [
            mock.patch(
                "runtime.agent_runtime.AgentRuntime.init_from_requirement",
                autospec=True,
                side_effect=_fake_init_from_requirement,
            ),
            mock.patch(
                "runtime.agent_runtime.AgentRuntime.enrich_transport_details",
                autospec=True,
                side_effect=lambda self, timeline, **kw: None,
            ),
        ]
        self._init_mock = self._patchers[0].start()
        self._patchers[1].start()
        self.addCleanup(lambda: [p.stop() for p in self._patchers])

    def tearDown(self) -> None:
        from django.contrib.auth.models import User

        from runtime.manager import manager

        for username in self._created:
            try:
                user = User.objects.filter(username=username).first()
                if user is not None:
                    manager.drop(user.id)
                    user.delete()
            except Exception:  # noqa: BLE001  清理失败不影响其他用例
                pass

    def _register(self, username: str | None = None, password: str = "secret123"):
        username = username or f"u_{uuid.uuid4().hex[:8]}"
        self._created.append(username)
        resp = self.client.post(
            "/api/auth/register/",
            data=json.dumps({"username": username, "password": password}),
            content_type="application/json",
        )
        assert resp.status_code == 200, resp.content
        return username, json.loads(resp.content)["token"]

    def _post(self, path: str, body: dict, token: str):
        return self.client.post(
            path, data=json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {token}",
        )

    def _get(self, path: str, token: str, client=None):
        return (client or self.client).get(
            path, HTTP_AUTHORIZATION=f"Bearer {token}"
        )

    def test_plan_response_contains_ordered_trace(self) -> None:
        _, token = self._register()
        resp = self._post("/api/plan/", _plan_body(), token)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = json.loads(resp.content)
        trace = body["trace"]
        self.assertEqual(trace["version"], 1)
        # request_id 与 X-Request-ID 一致（可与 server.log 对账）
        self.assertEqual(trace["request_id"], resp["X-Request-ID"])
        steps = trace["steps"]
        self.assertTrue(steps)
        self.assertEqual(
            [s["seq"] for s in steps], list(range(1, len(steps) + 1))
        )
        phases = {s["phase"] for s in steps}
        self.assertTrue({"parse", "plan", "enrich"}.issubset(phases))
        # 落锤统计（A 透传缺席时 plan_meta 仍完整，仅 data_source 为 None）
        self.assertEqual(trace["plan_meta"]["timeline"]["days"], 1)
        self.assertEqual(trace["plan_meta"]["timeline"]["spots"], 1)

    def test_plan_500_contains_partial_trace(self) -> None:
        _, token = self._register()
        self._init_mock.side_effect = RuntimeError("planner exploded")
        try:
            resp = self._post("/api/plan/", _plan_body(), token)
        finally:
            self._init_mock.side_effect = _fake_init_from_requirement
        self.assertEqual(resp.status_code, 500)
        body = json.loads(resp.content)
        self.assertIn("planner exploded", body["error"])
        self.assertTrue(body["trace"]["steps"])

    def test_plan_trace_endpoint_idle_done(self) -> None:
        _, token = self._register()
        self.assertEqual(
            json.loads(self._get("/api/plan-trace/", token).content)["status"],
            "idle",
        )
        self._post("/api/plan/", _plan_body(), token)
        body = json.loads(self._get("/api/plan-trace/", token).content)
        self.assertEqual(body["status"], "done")
        self.assertEqual(body["trace"]["plan_meta"]["timeline"]["city"], "北京")

    def test_plan_trace_running_while_planning(self) -> None:
        """规划进行中（持锁长写）plan-trace 端点仍可轮询（无锁设计）。"""
        from django.test import Client

        _, token = self._register()
        started_evt = threading.Event()
        release_evt = threading.Event()

        def slow_init(self, payload):
            started_evt.set()
            release_evt.wait(timeout=10)
            return _fake_init_from_requirement(self, payload)

        self._init_mock.side_effect = slow_init
        result: dict = {}

        def run_plan():
            result["resp"] = self._post("/api/plan/", _plan_body(), token)

        worker = threading.Thread(target=run_plan)
        worker.start()
        try:
            self.assertTrue(started_evt.wait(timeout=5), "规划线程未启动")
            # 另起 Client 轮询（规划线程持锁期间，端点必须无锁即时返回）
            body = json.loads(
                self._get("/api/plan-trace/", token, client=Client()).content
            )
            self.assertEqual(body["status"], "running")
            self.assertIn("parse", [s["phase"] for s in body["steps"]])
        finally:
            release_evt.set()
            worker.join(timeout=10)
        self.assertEqual(result["resp"].status_code, 200, result["resp"].content)
        # 规划结束后转 done
        body = json.loads(self._get("/api/plan-trace/", token).content)
        self.assertEqual(body["status"], "done")

    def test_tool_exception_leaves_trace_and_tool_log(self) -> None:
        """工具异常双留痕：tool_call_log 补失败记录 + trace error 步（补盲区）。"""
        from tools import ToolRegistry

        with mock.patch.object(
            ToolRegistry, "call", side_effect=RuntimeError("boom")
        ):
            from runtime.agent_runtime import AgentRuntime
            from runtime.plan_trace import PlanTraceRecorder

            rt = AgentRuntime()
            rec = PlanTraceRecorder(request_id="test0001")
            rt.current_trace_recorder = rec
            with self.assertLogs("runtime.agent", level="ERROR"):
                with self.assertRaises(RuntimeError):
                    rt.registry.call("weather", city="北京")
        step = rec.assemble()["steps"][0]
        self.assertEqual(step["status"], "error")
        self.assertIn("boom", step["error"])
        self.assertTrue(rt.tool_call_log)
        self.assertEqual(rt.tool_call_log[0]["status"], "error")

    def test_chat_response_contains_trace(self) -> None:
        _, token = self._register()
        import call_llm.client_factory as factory

        class _FakeClient:
            model_name = "deepseek-chat"

            def generate(self, messages, **kwargs):
                executor = kwargs.get("tool_executor")
                if executor is not None:
                    executor("weather", {"city": "北京"})
                return {
                    "content": "北京明天晴。",
                    "tool_trace": [{"round": 1, "calls": [
                        {"name": "weather", "arguments": {"city": "北京"},
                         "result": {"status": "ok", "weather": "晴",
                                    "temp_min": 12, "temp_max": 23}},
                    ]}],
                    "reviews": [],
                }

        with mock.patch.object(
            factory, "create_llm_client", return_value=_FakeClient()
        ):
            resp = self._post(
                "/api/chat/", {"message": "北京天气怎么样？"}, token
            )
        self.assertEqual(resp.status_code, 200, resp.content)
        body = json.loads(resp.content)
        self.assertTrue(body["reply"])
        kinds = [s["kind"] for s in body["trace"]["steps"]]
        self.assertIn("tool", kinds)
        self.assertIn("llm", kinds)
        llm_step = next(s for s in body["trace"]["steps"] if s["kind"] == "llm")
        self.assertEqual(llm_step["model"], "deepseek-chat")


if __name__ == "__main__":
    unittest.main()
