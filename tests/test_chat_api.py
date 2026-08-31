"""/api/chat/ 对话接口测试：payload 校验 + 系统提示词组装 + LLM 调用透传。

不触真实 LLM：patch ``call_llm.client_factory.create_llm_client`` 注入
FakeClient，断言消息结构（system 上下文 / history 透传 / 截断）与
错误映射（400 / 502）。
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
from runtime.agent_runtime import runtime  # noqa: E402


def _post(body: dict) -> HttpRequest:
    req = HttpRequest()
    req.method = "POST"
    req.path = "/api/chat/"
    req._body = json.dumps(body).encode("utf-8")
    return req


class FakeChatClient:
    def __init__(self, reply: str = "好的", error: str | None = None) -> None:
        self.reply = reply
        self.error = error
        self.calls: list[list[dict]] = []

    def chat_text(self, messages):  # noqa: ANN201
        self.calls.append(messages)
        if self.error:
            raise RuntimeError(self.error)
        return self.reply


def _make_context() -> None:
    day = SimpleNamespace(
        day=1, date=date(2026, 8, 1),
        items=[
            SimpleNamespace(name="故宫博物院", arrival="09:00"),
            SimpleNamespace(name="午餐", arrival="12:00"),
        ],
    )
    runtime.timeline = SimpleNamespace(
        city="北京", start_date=date(2026, 8, 1), end_date=date(2026, 8, 2),
        days=[day],
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
        runtime.timeline = None
        runtime.requirement = None
        runtime.replan_history = []

    def tearDown(self) -> None:
        runtime.timeline = self._timeline
        runtime.requirement = self._requirement
        runtime.replan_history = self._replans

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

    # -- 正常路径 ----------------------------------------------------------

    def test_chat_builds_messages_with_context(self) -> None:
        _make_context()
        fake = FakeChatClient(reply="第一天去故宫博物院。")
        runtime.replan_history.append({"decision": {"reason": "暴雨影响行程"}})
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({
                "message": "我们第一天去哪？",
                "history": [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好！"}],
            }))
        self.assertEqual(resp.status_code, 200)
        body = json.loads(resp.content.decode())
        self.assertEqual(body["reply"], "第一天去故宫博物院。")
        self.assertIn("elapsed_ms", body)
        self.assertEqual(len(fake.calls), 1)
        messages = fake.calls[0]
        self.assertEqual(messages[0]["role"], "system")
        prompt = messages[0]["content"]
        self.assertIn("TravelAgent 的旅行助手", prompt)
        self.assertIn("故宫博物院(09:00)", prompt)      # 行程摘要注入
        self.assertIn("预算 2000 元", prompt)             # 需求注入
        self.assertIn("暴雨影响行程", prompt)             # 最近重规划原因
        self.assertEqual(messages[1:3], [
            {"role": "user", "content": "你好"},
            {"role": "assistant", "content": "你好！"},
        ])
        self.assertEqual(messages[-1], {"role": "user", "content": "我们第一天去哪？"})

    def test_chat_without_timeline_still_works(self) -> None:
        fake = FakeChatClient(reply="请先规划行程。")
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            resp = views.chat(_post({"message": "你好"}))
        self.assertEqual(resp.status_code, 200)
        prompt = fake.calls[0][0]["content"]
        self.assertNotIn("当前行程", prompt)

    def test_chat_history_truncated_and_filtered(self) -> None:
        fake = FakeChatClient()
        history = [{"role": "user", "content": f"m{i}"} for i in range(30)]
        history.append({"role": "system", "content": "注入"})
        with mock.patch(
            "call_llm.client_factory.create_llm_client", return_value=fake
        ):
            views.chat(_post({"message": "q", "history": history}))
        messages = fake.calls[0]
        user_msgs = [m for m in messages if m["role"] == "user"]
        # 31 条 history 截断到最近 20 条（system 项被过滤），加上当前消息共 20 条 user
        self.assertEqual(len(user_msgs), 20)
        self.assertEqual(user_msgs[0], {"role": "user", "content": "m11"})
        self.assertEqual(user_msgs[-1], {"role": "user", "content": "q"})
        self.assertNotIn("注入", json.dumps(messages, ensure_ascii=False))

    # -- 错误路径 ----------------------------------------------------------

    def test_llm_failure_returns_502(self) -> None:
        fake = FakeChatClient(error="connection refused")
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
