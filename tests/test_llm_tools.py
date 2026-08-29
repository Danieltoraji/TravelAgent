"""P4 function calling 测试：to_openai_tools 转换、generate tool_calls 回路、门控。"""

import json
import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from call_llm.llm_clients.BaseClient import LLMClient  # noqa: E402
from tools.tool_provider import ToolProvider  # noqa: E402


# ---------------------------------------------------------------------------
# Fake client：绕过 __init__，脚本化 _request_completion 响应
# ---------------------------------------------------------------------------


def _fake_client(scripted):
    """真实 DSClient 实例（哑 key）+ 脚本化 _request_completion。

    不做方法重绑——此前对 @staticmethod _append_retry_turn 的"再绑定"曾把
    self 重复注入，属测试装配缺陷。
    """
    from call_llm.llm_clients.DSClient import DSClient

    client = DSClient(model_name="fake-model", api_key="sk-test",
                      base_url="https://fake", timeout=5,
                      ask_user_if_missing=False, max_clarifications=0)
    client._scripted = list(scripted)
    client.calls = []

    def _request_completion(params):
        client.calls.append(params)
        return client._scripted.pop(0)

    client._request_completion = _request_completion
    return client


def _content_response(payload):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=json.dumps(payload), tool_calls=None),
        finish_reason="stop",
    )])


def _tool_call_response(call_id, name, arguments):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(
            content=None,
            tool_calls=[SimpleNamespace(
                id=call_id,
                function=SimpleNamespace(name=name, arguments=json.dumps(arguments)),
            )],
        ),
        finish_reason="tool_calls",
    )])


_SCORE_SCHEMA = {
    "type": "object",
    "properties": {
        "score": {"type": "integer"},
        "reasons": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["score", "reasons"],
}
_SCORE = {"score": 80, "reasons": ["工具佐证"]}


class TestToolCallsLoop(unittest.TestCase):
    def test_tool_round_then_answer(self) -> None:
        client = _fake_client([
            _tool_call_response("c1", "weather", {"city": "北京"}),
            _content_response(_SCORE),
        ])
        executor = MagicMock(return_value={"condition": "晴"})
        result = client.generate(
            messages=[{"role": "user", "content": "判断影响"}],
            response_schema=_SCORE_SCHEMA,
            tools=[{"type": "function", "function": {"name": "weather"}}],
            tool_executor=executor,
        )
        self.assertEqual(result["content"]["score"], 80)
        self.assertEqual(result["tool_rounds"], 1)
        executor.assert_called_once_with("weather", {"city": "北京"})
        # 首次请求尚未发生工具调用
        tool_msgs = [m for m in client.calls[0]["messages"] if m.get("role") == "tool"]
        self.assertEqual(tool_msgs, [])

    def test_tool_result_appended_to_conversation(self) -> None:
        client = _fake_client([
            _tool_call_response("c1", "weather", {"city": "北京"}),
            _content_response(_SCORE),
        ])
        client.generate(
            messages=[{"role": "user", "content": "判断影响"}],
            response_schema=_SCORE_SCHEMA,
            tools=[{"type": "function", "function": {"name": "weather"}}],
            tool_executor=lambda name, args: {"condition": "晴"},
        )
        second = client.calls[1]["messages"]
        tool_msgs = [m for m in second if m.get("role") == "tool"]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["tool_call_id"], "c1")
        self.assertIn("晴", tool_msgs[0]["content"])
        assistant = [m for m in second if m.get("role") == "assistant" and m.get("tool_calls")]
        self.assertEqual(len(assistant), 1)

    def test_round_limit_exceeded(self) -> None:
        def _always_tool():
            return _tool_call_response("cx", "weather", {"city": "北京"})
        client = _fake_client([_always_tool() for _ in range(5)])
        with self.assertRaises(ValueError) as ctx:
            client.generate(
                messages=[{"role": "user", "content": "x"}],
                tools=[{"type": "function", "function": {"name": "weather"}}],
                tool_executor=lambda name, args: {},
                max_tool_rounds=3,
            )
        self.assertIn("轮次超过上限", str(ctx.exception))

    def test_tools_unsupported_degrades_once(self) -> None:
        client = _fake_client([_content_response(_SCORE)])
        calls = client.calls

        def _request(params):
            calls.append({"tools": params.get("tools")})
            if params.get("tools"):
                raise RuntimeError("model does not support tools")
            return client._scripted.pop(0)

        client._request_completion = _request
        result = client.generate(
            messages=[{"role": "user", "content": "x"}],
            response_schema=_SCORE_SCHEMA,
            tools=[{"type": "function", "function": {"name": "weather"}}],
            tool_executor=lambda name, args: {},
        )
        self.assertEqual(result["content"]["score"], 80)
        self.assertTrue(result["tools_degraded"])
        self.assertIsNotNone(calls[0]["tools"])    # 首次携带 tools（被拒）
        self.assertIsNone(calls[1]["tools"])       # 降级后不再携带 tools

    def test_no_executor_keeps_legacy_path(self) -> None:
        # tool_calls 返回但无 executor → 走既有非 JSON 重试路径，重试耗尽报错
        client = _fake_client([
            _tool_call_response("c1", "weather", {}),
            _tool_call_response("c2", "weather", {}),
            _tool_call_response("c3", "weather", {}),
        ])
        with self.assertRaises(ValueError) as ctx:
            client.generate(messages=[{"role": "user", "content": "x"}])
        self.assertIn("did not return a JSON", str(ctx.exception))


class TestToOpenAITools(unittest.TestCase):
    def test_conversion_format(self) -> None:
        from tools import default_registry
        provider = ToolProvider(default_registry)
        tools = provider.to_openai_tools()
        names = {t["function"]["name"] for t in tools}
        self.assertIn("weather", names)
        self.assertIn("train_trip", names)
        self.assertNotIn("booking", names)
        self.assertNotIn("hotel_book", names)
        weather = next(t for t in tools if t["function"]["name"] == "weather")
        self.assertEqual(weather["type"], "function")
        self.assertEqual(weather["function"]["parameters"]["required"], ["city"])


class TestDecisionEngineGate(unittest.TestCase):
    def _requirement(self):
        return {"content": {"destination": "北京"}}

    def _events(self):
        from core.schemas import EventType
        return [{"event_type": EventType.WEATHER.value if hasattr(EventType.WEATHER, "value") else "weather",
                 "place": "北京", "metrics": {"rain_probability": 70}}]

    def test_gate_off_passes_no_tools(self) -> None:
        os.environ.pop("USE_LLM_TOOLS", None)
        captured = {}
        fake_client = MagicMock()
        fake_client.generate.return_value = {"content": {"score": 10, "reasons": []}}
        captured["client"] = fake_client

        import call_llm.decision_engine as de
        with patch.object(de, "create_llm_client", return_value=fake_client):
            de.decide_replan(self._requirement(), self._events(), tool_provider=MagicMock())
        _, kwargs = fake_client.generate.call_args
        self.assertIsNone(kwargs.get("tools"))
        self.assertIsNone(kwargs.get("tool_executor"))

    def test_gate_on_passes_tools_and_executor(self) -> None:
        os.environ["USE_LLM_TOOLS"] = "1"
        try:
            captured = {}
            fake_client = MagicMock()
            fake_client.generate.return_value = {"content": {"score": 10, "reasons": []}}
            captured["client"] = fake_client
            provider = MagicMock()
            provider.to_openai_tools.return_value = [
                {"type": "function", "function": {"name": "weather_brief"}},
            ]

            import call_llm.decision_engine as de
            with patch.object(de, "create_llm_client", return_value=fake_client):
                de.decide_replan(self._requirement(), self._events(), tool_provider=provider)
            _, kwargs = fake_client.generate.call_args
            self.assertEqual(kwargs.get("tools"), provider.to_openai_tools.return_value)
            self.assertEqual(kwargs.get("tool_executor"), provider.call_json)
        finally:
            os.environ.pop("USE_LLM_TOOLS", None)


if __name__ == "__main__":
    unittest.main()
