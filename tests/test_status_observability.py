"""status 观测字段测试（2026-09-14 复验补回）。

多用户改造后 /api/status/ 丢了 ``last_data_source``/``last_error``（真源判定
金标准信号），编排阶段 b 又需要外部可验证 ``USE_LLM_ORCHESTRATOR`` 门控是否
生效——``AgentRuntime.status()`` 增三个只增字段：last_data_source / last_error /
orchestration（编排现场摘要，门控关恒为 None）。

不联网：planner hook 桩（monkeypatch ``runtime.agent_runtime.build_planner_hook``）。
"""

import pytest

from runtime.agent_runtime import AgentRuntime


class _StubHook:
    """planner hook 桩：可编程 last_data_source / _orchestration_result。"""

    def __init__(self, requirement, tool_provider=None,
                 data_source="fake", orch=None, notices=None):
        self.last_data_source = data_source
        self.last_error = None
        self._orchestration_result = orch
        self.fallback_notices = list(notices or [])

    def generate_timeline(self, *args, **kwargs):
        from core.schemas import TripTimeline
        from datetime import date

        return TripTimeline(
            id="plan_t", city="北京", start_date=date(2026, 8, 4),
            end_date=date(2026, 8, 5),
            days=[],
        )


def _make_runtime(monkeypatch, *, data_source, orch, notices=None):
    rt = AgentRuntime()

    def _factory(requirement, tool_provider=None):
        return _StubHook(requirement, tool_provider,
                         data_source=data_source, orch=orch,
                         notices=notices)

    monkeypatch.setattr(
        "runtime.agent_runtime.build_planner_hook", _factory
    )
    return rt


def test_status_reports_data_source_and_error(monkeypatch):
    """固定管线（门控关）：status 带 last_data_source，orchestration=None。"""
    rt = _make_runtime(monkeypatch, data_source="live", orch=None)
    rt.init_from_requirement({"content": {"destination": "北京", "days": 1}})
    status = rt.status()
    assert status["last_data_source"] == "live"
    assert status["last_error"] is None
    assert status["orchestration"] is None   # 门控关 → 编排现场恒 None


def test_status_reports_orchestration_when_enabled(monkeypatch):
    """门控开：编排现场摘要透出（accepted/轮数/回落原因）。"""
    rt = _make_runtime(monkeypatch, data_source="live", orch={
        "tools_enabled": True, "accepted": True, "schedule_calls": 2,
        "tool_rounds": 4, "fallback_reason": None,
    })
    rt.init_from_requirement({"content": {"destination": "北京", "days": 1}})
    status = rt.status()
    assert status["orchestration"]["accepted"] is True
    assert status["orchestration"]["schedule_calls"] == 2
    assert status["orchestration"]["fallback_reason"] is None


def test_status_orchestration_none_when_gate_off_hook(monkeypatch):
    """门控关的 hook（_orchestration_result tools_enabled=False）→ 不误报。"""
    rt = _make_runtime(monkeypatch, data_source="live", orch={
        "tools_enabled": False, "accepted": False, "fallback_reason": "gate off",
    })
    rt.init_from_requirement({"content": {"destination": "北京", "days": 1}})
    assert rt.status()["orchestration"] is None


def test_status_reports_fallback_notices(monkeypatch):
    """降级告知（真源查不到 → 告知用户）：notices 透出到 status（只增字段）。"""
    rt = _make_runtime(
        monkeypatch, data_source="live", orch=None,
        notices=["『北京大兴国际机场→张掖甘州机场』航班段未查到当日真源班次"
                 "（可能超出 12306 预售期或当日无航班），已按估算衔接"],
    )
    rt.init_from_requirement({"content": {"destination": "张掖", "days": 1}})
    status = rt.status()
    assert status["notices"] and "估算衔接" in status["notices"][0]


def test_status_notices_empty_by_default(monkeypatch):
    rt = _make_runtime(monkeypatch, data_source="live", orch=None)
    rt.init_from_requirement({"content": {"destination": "北京", "days": 1}})
    assert rt.status()["notices"] == []
