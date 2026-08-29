"""ToolProvider 与 A 侧 LLM 工具调用接口测试。"""

import unittest
from datetime import date

from core.schemas import DayPlan, Place, ToolSpec, TripTimeline
from tools.base_tool import ToolRegistry
from tools.booking_tool import BookingTool
from tools.tool_provider import ToolProvider
from tools.weather_tool import WeatherTool


def make_registry() -> ToolRegistry:
    reg = ToolRegistry()
    reg.register(WeatherTool())
    reg.register(BookingTool())
    return reg


class TestToolProvider(unittest.TestCase):
    def test_default_allowlist_excludes_side_effect_tools(self) -> None:
        provider = ToolProvider(make_registry())
        names = [s.name for s in provider.list_tools()]
        self.assertIn("weather", names)
        self.assertNotIn("booking", names)

    def test_list_tools_returns_specs(self) -> None:
        provider = ToolProvider(make_registry())
        specs = provider.list_tools()
        self.assertTrue(specs)
        for spec in specs:
            self.assertIsInstance(spec, ToolSpec)
            self.assertTrue(spec.name)
            self.assertTrue(spec.description)
            self.assertIsInstance(spec.input_schema, dict)

    def test_custom_allowlist(self) -> None:
        provider = ToolProvider(make_registry(), allowlist={"booking"})
        names = [s.name for s in provider.list_tools()]
        self.assertEqual(names, ["booking"])
        with self.assertRaises(KeyError):
            provider.call("weather")

    def test_call_returns_tool_result(self) -> None:
        provider = ToolProvider(make_registry())
        result = provider.call("weather", city="北京")
        self.assertEqual(result.tool, "weather")
        self.assertEqual(result.status.value, "ok")
        self.assertIn("condition", result.data)

    def test_call_json_returns_serializable_dict(self) -> None:
        provider = ToolProvider(make_registry())
        payload = provider.call_json("weather", {"city": "北京"})
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["tool"], "weather")
        self.assertIn("data", payload)

    def test_call_not_allowed_raises(self) -> None:
        provider = ToolProvider(make_registry())
        with self.assertRaises(KeyError):
            provider.call("booking")

    def test_get_tool_spec(self) -> None:
        provider = ToolProvider(make_registry())
        spec = provider.get_tool("weather")
        self.assertEqual(spec.name, "weather")
        with self.assertRaises(KeyError):
            provider.get_tool("booking")


class TestExecutionAgentToolContext(unittest.TestCase):
    def test_decision_request_contains_tool_specs(self) -> None:
        from execution.execution_agent import ExecutionAgent

        timeline = TripTimeline(
            city="北京",
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 2),
            days=[
                DayPlan(day=1, date=date(2026, 8, 1), items=[
                    Place(name="故宫", category="scenic", arrival="09:00"),
                ]),
            ],
        )
        seen = {}

        def hook(req):
            seen["req"] = req
            return None

        agent = ExecutionAgent(
            timeline=timeline,
            registry=make_registry(),
            decision_hook=hook,
        )
        self.assertIsNotNone(agent.tool_provider)
        tool_names = [s["name"] for s in agent.tool_provider.list_tools_json()]
        self.assertIn("weather", tool_names)

        # A1（2026-08-28）：context 不再携带 tool_specs（A 侧从未消费）；
        # 这里验证 provider 白名单本身 + context 仅含 threshold
        from core.schemas import DecisionRequest, MonitorEvent, EventType
        from datetime import datetime
        event = MonitorEvent(
            event_id="e1",
            event_type=EventType.WEATHER,
            place="北京",
            observed_at=datetime.now(),
            rule_name="weather-poll",
            data={"rain_probability": 85},
        )
        # handle_event 是 async，这里用 asyncio 跑一次
        import asyncio
        asyncio.run(agent.handle_event(event))
        req = seen["req"]
        self.assertIsInstance(req, DecisionRequest)
        self.assertNotIn("tool_specs", req.context)
        self.assertEqual(req.context["impact_threshold"], agent.impact_threshold)


if __name__ == "__main__":
    unittest.main()
