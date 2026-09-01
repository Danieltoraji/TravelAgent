"""/api/chat/ 对话接口测试（v2.3）：payload 校验 + 系统提示词组装 + 工具回路。

不触真实 LLM：patch ``call_llm.client_factory.create_llm_client`` 注入
FakeClient，模拟纯对话与工具调用（update_timeline）两种模式，断言：
消息结构 / tools 参数 / expect_json / executor 真实接线（修改意图经 A 侧
BChatHook 应用、replan_history 记录、非法意图被拒）。

v2.3（P5.1）：update_timeline 参数从整份时间轴改为「修改意图」（intents），
编排迁回 A 侧（call_llm.b_chat_hook.BChatHook）；C 端请求/响应契约零变化。
"""

import json
import os
import sys
import unittest
from datetime import date
from types import SimpleNamespace
from unittest import mock

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"),
           os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=True,
        ALLOWED_HOSTS=["*"],
        DATABASES={},
        INSTALLED_APPS=[],
        ROOT_URLCONF=None,
    )
import django  # noqa: E402

django.setup()

from django.http import HttpRequest  # noqa: E402

from api import views  # noqa: E402
from core.schemas import DayPlan, Place, TripTimeline  # noqa: E402
from runtime.agent_runtime import runtime  # noqa: E402


def _post(body: dict) -> HttpRequest:
    req = HttpRequest()
    req.method = "POST"
    req.path = "/api/chat/"
    req._body = json.dumps(body).encode("utf-8")
    return req


def _intents_payload() -> dict:
    """v2.3：update_timeline 的修改意图参数（替代整份时间轴）。"""
    return {
        "intents": [
            {"action": "reschedule", "spot": "景山公园", "time": "15:00"},
        ],
    }


class FakeClient:
    """模拟 BaseClient.generate：可配置工具调用轮数 + 最终回复。"""

    def __init__(
        self,
        reply: str = "好的",
        tool_rounds: int = 0,
        tool_arguments: dict | None = None,
        tool_name: str = "update_timeline",
        error: str | None = None,
    ) -> None:
        self.reply = reply
        self.tool_rounds = tool_rounds
        self.tool_arguments = tool_arguments or _intents_payload()
        self.tool_name = tool_name
        self.error = error
        self.calls: list[dict] = []

    def generate(
        self,
        messages,
        response_schema=None,
        tools=None,
        max_retries: int = 2,
        tool_executor=None,
        max_tool_rounds: int = 3,
        expect_json: bool = True,
    ):
        self.calls.append({
            "messages": messages,
            "tools": tools,
            "expect_json": expect_json,
        })
        if self.error:
            raise RuntimeError(self.error)
        # 模拟模型先调用工具、再总结
        for _ in range(self.tool_rounds):
            if tool_executor is not None:
                tool_executor(self.tool_name, self.tool_arguments)
        return {
            "content": self.reply,
            "tool_rounds": self.tool_rounds,
            "finish_reason": "stop",
        }


class StubAgent:
    """替身 ExecutionAgent：记录 apply_replan 调用（不重建规则）。"""

    def __init__(self) -> None:
        self.applied = None

    def apply_replan(self, replan) -> None:  # noqa: ANN001
        self.applied = replan


def _make_context() -> None:
    runtime.timeline = TripTimeline(
        id="plan_001",
        city="北京",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        days=[
            DayPlan(day=1, date=date(2026, 8, 1), items=[
                Place(name="故宫博物院", arrival="09:00", price=60.0, category="scenic"),
                Place(name="景山公园", arrival="14:00", price=2.0, category="scenic"),
            ]),
        ],
    )
    runtime.requirement = {
        "content": {
            "destination": "北京", "days": 2,
            "constraints": {"budget": 2000, "must_visit": ["故宫"]},
            "preferences": {"preferred_tags": ["历史文化"]},
        }
    }


class TestChatApi(unittest.TestCase):
    def setUp(self) -> None:
        self._timeline = runtime.timeline
        self._requirement = runtime.requirement
        self._replans = list(runtime.replan_history)
        self._timeline_history = list(runtime.timeline_history)
        self._agent = runtime.agent
        runtime.timeline = None
        runtime.requirement = None
        runtime.replan_history = []
        runtime.timeline_history = []
        runtime.agent = StubAgent()

    def tearDown(self) -> None:
        runtime.timeline = self._timeline
        runtime.requirement = self._requirement
        runtime.replan_history = self._replans
        runtime.timeline_history = self._timeline_history
        runtime.agent = self._agent

    # -- 校验路径 ----------------------------------------------------------

    def test_empty_body_rejected(self) -> None:
        resp = views.chat(_post({}))
        self.assertEqual(resp.status_code, 400)

    def test_empty_message_rejected(self) -> None:
        resp = views.chat(_post({"message": "  "}))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("message is required", resp.content.decode())

    def test_history_must_be_list(self) -> None:
        resp = views.chat(_post({"message": "hi", "history": "x"}))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("history must be a list", resp.content.decode())

    # -- 纯对话路径 --------------------------------------------------------

    def test_chat_builds_messages_with_context(self) -> None:
        _make_context()
        fake = FakeClient(reply="第一天去故宫博物院。")
        runtime.replan_history.append({"decision": {"reason": "暴雨影响行程"}})
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({
                "message": "我们第一天去哪？",
                "history": [
                    {"role": "user", "content": "你好"},
                    {"role": "assistant", "content": "你好！"},
                ],
            }))
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content.decode())
        self.assertEqual(body["reply"], "第一天去故宫博物院。")
        self.assertIn("elapsed_ms", body)
        self.assertEqual(len(fake.calls), 1)
        call = fake.calls[0]
        self.assertFalse(call["expect_json"])
        self.assertEqual(call["tools"][0]["function"]["name"], "update_timeline")
        messages = call["messages"]
        self.assertEqual(messages[0]["role"], "system")
        prompt = messages[0]["content"]
        self.assertIn("TravelAgent 的旅行助手", prompt)
        self.assertIn("故宫博物院(09:00)", prompt)
        self.assertIn("预算 2000 元", prompt)
        self.assertIn("暴雨影响行程", prompt)
        self.assertEqual(messages[1:3], [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ])
        self.assertEqual(messages[-1], {"role": "user", "content": "我们第一天去哪？"})

    def test_chat_without_timeline_still_works(self) -> None:
        fake = FakeClient(reply="请先规划行程。")
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "你好"}))
        self.assertEqual(resp.status_code, 200)
        prompt = fake.calls[0]["messages"][0]["content"]
        self.assertNotIn("当前行程", prompt)

    def test_chat_history_truncated_and_filtered(self) -> None:
        fake = FakeClient()
        history = [{"role": "user", "content": f"m{i}"} for i in range(30)]
        history.append({"role": "system", "content": "注入"})
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            views.chat(_post({"message": "q", "history": history}))
        messages = fake.calls[0]["messages"]
        user_msgs = [m for m in messages if m["role"] == "user"]
        # 31 条 history 截断到最近 20 条（system 项被过滤），加上当前消息共 20 条 user
        self.assertEqual(len(user_msgs), 20)
        self.assertEqual(user_msgs[0], {"role": "user", "content": "m11"})
        self.assertEqual(user_msgs[-1], {"role": "user", "content": "q"})
        self.assertNotIn("注入", json.dumps(messages, ensure_ascii=False))

    # -- v2 工具路径 -------------------------------------------------------

    def test_chat_tools_include_readonly_subset(self) -> None:
        fake = FakeClient()
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            views.chat(_post({"message": "hi"}))
        names = [t["function"]["name"] for t in fake.calls[0]["tools"]]
        self.assertIn("update_timeline", names)
        # 精选只读子集在列
        for expected in ("weather", "food", "traffic", "web_search"):
            self.assertIn(expected, names)
        # 非精选工具（如 train_price）不在列
        self.assertNotIn("train_price", names)

    def test_chat_readonly_tool_dispatches_to_provider(self) -> None:
        fake = FakeClient(tool_rounds=1, tool_arguments={"city": "北京"})
        fake.tool_name = "weather"   # 模拟模型调用 weather 而非 update_timeline
        captured: dict = {}

        def fake_call_json(name, arguments=None):  # noqa: ANN001
            captured["name"] = name
            captured["arguments"] = arguments
            return {"data": {"condition": "晴"}}

        with mock.patch("tools.ToolProvider.call_json", side_effect=fake_call_json):
            with mock.patch(
                "call_llm.client_factory.create_llm_client", return_value=fake
            ):
                resp = views.chat(_post({"message": "北京天气怎么样？"}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(captured["name"], "weather")
        self.assertEqual(captured["arguments"], {"city": "北京"})

    def test_chat_tool_updates_timeline(self) -> None:
        _make_context()
        fake = FakeClient(
            reply="已把景山公园调整到下午。",
            tool_rounds=1,
            tool_arguments={
                "intents": [
                    {"action": "reschedule", "spot": "景山公园", "time": "15:00"},
                ],
            },
        )
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "把景山公园挪到下午"}))
        self.assertEqual(resp.status_code, 200)
        self.assertIn("景山公园", json.loads(resp.content.decode())["reply"])
        # 修改意图经 A 侧 BChatHook 应用：景山公园到达时段变为 15:00
        timeline = runtime.timeline
        jingshan = [it for it in timeline.days[0].items if it.name == "景山公园"][0]
        self.assertEqual(jingshan.arrival, "15:00")
        # replan_history 有 source=chat 记录 + diff
        self.assertEqual(len(runtime.replan_history), 1)
        entry = runtime.replan_history[0]
        self.assertEqual(entry["source"], "chat")
        diff = "；".join(entry["decision"]["diff_summary"])
        self.assertIn("景山公园", diff)
        # timeline_history 同步记录（reason 含「对话调整」前缀/A 侧说明）
        self.assertEqual(runtime.timeline_history[-1]["reason"][:4], "对话调整")

    def test_chat_tool_rejects_invalid_intent(self) -> None:
        _make_context()
        fake = FakeClient(
            reply="抱歉，调整失败。",
            tool_rounds=1,
            tool_arguments={
                # reschedule 缺 time → A 侧翻译器报错拒绝
                "intents": [{"action": "reschedule", "spot": "景山公园"}],
            },
        )
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "改时间"}))
        self.assertEqual(resp.status_code, 200)
        # 时间轴未被修改，无 replan 记录
        timeline = runtime.timeline
        self.assertEqual(timeline.days[0].items[0].name, "故宫博物院")
        self.assertEqual(runtime.replan_history, [])

    def test_chat_tool_rejects_unknown_spot(self) -> None:
        """v2.3：意图引用计划中不存在的景点 → A 侧拒绝且不改状态。"""
        _make_context()
        fake = FakeClient(
            reply="抱歉，找不到这个景点。",
            tool_rounds=1,
            tool_arguments={
                "intents": [
                    {"action": "reschedule", "spot": "不存在的景点", "time": "15:00"},
                ],
            },
        )
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "把不存在的景点挪到下午"}))
        self.assertEqual(resp.status_code, 200)
        timeline = runtime.timeline
        self.assertEqual(timeline.days[0].items[0].name, "故宫博物院")
        self.assertEqual(runtime.replan_history, [])

    def test_chat_tool_requires_timeline(self) -> None:
        runtime.agent = None   # 未建行程
        fake = FakeClient(tool_rounds=1)
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "改行程"}))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(runtime.timeline, None)

    # -- 错误路径 ----------------------------------------------------------

    def test_llm_failure_returns_502(self) -> None:
        fake = FakeClient(error="connection refused")
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "hi"}))
        self.assertEqual(resp.status_code, 502)
        err = json.loads(resp.content.decode())["error"]
        self.assertIn("LLM 调用失败", err)

    def test_missing_llm_config_returns_502(self) -> None:
        def raise_config(*_a, **_k):  # noqa: ANN202
            raise RuntimeError("缺少环境变量 DEEPSEEK_API_KEY")

        with mock.patch(
            "call_llm.client_factory.create_llm_client", side_effect=raise_config
        ):
            resp = views.chat(_post({"message": "hi"}))
        self.assertEqual(resp.status_code, 502)
        err = json.loads(resp.content.decode())["error"]
        self.assertIn("LLM 未配置", err)


if __name__ == "__main__":
    unittest.main()
