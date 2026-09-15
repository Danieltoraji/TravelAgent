"""agent_trace 端点测试（C 端展示 LLM 思考/调 tool 过程，2026-09-15）。

- runtime 捕获：planner_hook._orchestration_result.agent_trace → runtime.agent_trace；
- 端点：GET /api/agent-trace/ → {"enabled", "trace"}（门控关/未编排 → enabled false）；
- status 小旗：agent_trace_enabled。
"""

from types import SimpleNamespace

import django
from django.test import RequestFactory, SimpleTestCase

django.setup()

from api import views
from runtime.agent_runtime import AgentRuntime


def _stub_hook(agent_trace=None, orch_enabled=False):
    class _StubHook:
        last_data_source = "live"
        last_error = None
        _orchestration_result = {
            "tools_enabled": orch_enabled,
            "agent_trace": agent_trace,
        }

        def generate_timeline(self, *args, **kwargs):
            from datetime import date

            from core.schemas import TripTimeline

            return TripTimeline(id="p", city="北京",
                                start_date=date(2026, 9, 21),
                                end_date=date(2026, 9, 22), days=[])
    return _StubHook()


def _make_runtime(monkeypatch, *, agent_trace=None, orch_enabled=False):
    rt = AgentRuntime()

    def _factory(requirement, tool_provider=None):
        return _stub_hook(agent_trace=agent_trace, orch_enabled=orch_enabled)

    monkeypatch.setattr("runtime.agent_runtime.build_planner_hook", _factory)
    return rt


_TRACE = {
    "enabled": True,
    "steps": [
        {"seq": 1, "phase": "查真源", "tool": "train_trip",
         "args_digest": {"from_city": "锦州"}, "ms": 1500},
        {"seq": 2, "phase": "落锤", "tool": "schedule_plan", "ms": 9000},
        {"seq": 3, "phase": "收尾", "accepted": True, "summary": "达标"},
    ],
    "accepted": True,
}


class AgentTraceRuntimeTest(SimpleTestCase):
    def test_runtime_captures_trace_when_enabled(self, ):
        import os
        os.environ["USE_LLM_ORCHESTRATOR"] = "1"
        try:
            from unittest.mock import patch
            rt = AgentRuntime()
            with patch("runtime.agent_runtime.build_planner_hook",
                       return_value=_stub_hook(agent_trace=_TRACE, orch_enabled=True)):
                rt.init_from_requirement({"content": {"destination": "北京", "days": 1}})
            assert rt.agent_trace is not None
            assert rt.agent_trace["steps"][2]["phase"] == "收尾"
            assert rt.status()["agent_trace_enabled"] is True
        finally:
            os.environ.pop("USE_LLM_ORCHESTRATOR", None)

    def test_runtime_trace_none_when_gate_off(self):
        import os
        from unittest.mock import patch
        os.environ.pop("USE_LLM_ORCHESTRATOR", None)
        rt = AgentRuntime()
        with patch("runtime.agent_runtime.build_planner_hook",
                   return_value=_stub_hook(agent_trace=None, orch_enabled=False)):
            rt.init_from_requirement({"content": {"destination": "北京", "days": 1}})
        assert rt.agent_trace is None
        assert rt.status()["agent_trace_enabled"] is False


class AgentTraceEndpointTest(SimpleTestCase):
    def _request(self, rt):
        request = RequestFactory().get("/api/agent-trace/")
        request.runtime = rt
        request.auth_user = SimpleNamespace(username="tester")
        return request

    def test_endpoint_returns_trace(self):
        rt = SimpleNamespace(agent_trace=_TRACE)
        resp = views.agent_trace(self._request(rt))
        import json
        data = json.loads(resp.content)
        assert data["enabled"] is True
        assert data["trace"]["steps"][0]["tool"] == "train_trip"

    def test_endpoint_disabled_when_no_trace(self):
        rt = SimpleNamespace(agent_trace=None)
        resp = views.agent_trace(self._request(rt))
        import json
        data = json.loads(resp.content)
        assert data == {"enabled": False, "trace": None}
