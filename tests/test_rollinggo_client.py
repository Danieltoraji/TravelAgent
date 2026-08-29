"""RollingGoClient 健壮性测试：结果解析、错误分类、重试。"""

import unittest
import asyncio
from types import SimpleNamespace

from tools.rollinggo_client import (
    RollingGoAuthError,
    RollingGoClient,
    RollingGoConnectionError,
    RollingGoError,
    RollingGoProtocolError,
    RollingGoTimeoutError,
)


class _TextContent:
    type = "text"

    def __init__(self, text):
        self.text = text


class TestParseResult(unittest.TestCase):
    def test_structured_content(self) -> None:
        result = SimpleNamespace(
            structured_content={"hotels": []},
            content=[],
            is_error=False,
        )
        self.assertEqual(RollingGoClient._parse_result(result), {"hotels": []})

    def test_text_json(self) -> None:
        result = SimpleNamespace(
            structured_content=None,
            content=[_TextContent('{"hotels": [1]}')],
            is_error=False,
        )
        self.assertEqual(RollingGoClient._parse_result(result), {"hotels": [1]})

    def test_text_non_json(self) -> None:
        result = SimpleNamespace(
            structured_content=None,
            content=[_TextContent("hello")],
            is_error=False,
        )
        self.assertEqual(RollingGoClient._parse_result(result), {"text": "hello"})

    def test_empty_falls_back_to_raw(self) -> None:
        result = SimpleNamespace(
            structured_content=None,
            content=[],
            is_error=False,
        )
        parsed = RollingGoClient._parse_result(result)
        self.assertIn("raw", parsed)


class TestClassifyException(unittest.TestCase):
    """_classify_exception 是纯函数（只读异常对象）——用 __new__ 裸实例，
    不触发 RollingGoClient.__init__（会启动后台 worker 并真连 url，
    违反「单测禁联网」且在无服务地址下时序 flaky）。"""

    def setUp(self) -> None:
        self.client = RollingGoClient.__new__(RollingGoClient)

    def tearDown(self) -> None:
        self.client = None

    def test_auth_status(self) -> None:
        response = SimpleNamespace(status_code=401)
        exc = SimpleNamespace(response=response)
        self.assertIsInstance(self.client._classify_exception(exc), RollingGoAuthError)

    def test_auth_text(self) -> None:
        exc = RuntimeError("403 Forbidden")
        self.assertIsInstance(self.client._classify_exception(exc), RollingGoAuthError)

    def test_timeout(self) -> None:
        self.assertIsInstance(
            self.client._classify_exception(TimeoutError("timeout")),
            RollingGoTimeoutError,
        )

    def test_protocol_validation(self) -> None:
        exc = ValueError("validation error")
        self.assertIsInstance(
            self.client._classify_exception(exc),
            RollingGoProtocolError,
        )

    def test_connection_fallback(self) -> None:
        exc = ConnectionError("broken pipe")
        self.assertIsInstance(
            self.client._classify_exception(exc),
            RollingGoConnectionError,
        )


class TestRetry(unittest.TestCase):
    def test_retry_then_success(self) -> None:
        client = RollingGoClient(
            url="http://localhost", api_key="test",
            timeout=1, max_retries=2, retry_backoff_base=0,
        )
        calls = {"n": 0}

        class _Future:
            def result(self, timeout=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RollingGoConnectionError("boom")
                return {"ok": True}

        client._submit = lambda coro: _Future()
        client._restart_worker = lambda: None
        try:
            result = client._run_with_retry(lambda: None)
            self.assertEqual(result, {"ok": True})
            self.assertEqual(calls["n"], 2)
        finally:
            client.close()

    def test_auth_error_not_retried(self) -> None:
        client = RollingGoClient(
            url="http://localhost", api_key="test",
            timeout=1, max_retries=2, retry_backoff_base=0,
        )
        calls = {"n": 0}

        class _Future:
            def result(self, timeout=None):
                calls["n"] += 1
                raise RollingGoAuthError("401")

        client._submit = lambda coro: _Future()
        client._restart_worker = lambda: None
        try:
            with self.assertRaises(RollingGoAuthError):
                client._run_with_retry(lambda: None)
            self.assertEqual(calls["n"], 1)
        finally:
            client.close()


class TestRequestAsyncGuards(unittest.IsolatedAsyncioTestCase):
    async def test_close_request_allowed_when_marked_closed(self) -> None:
        client = RollingGoClient.__new__(RollingGoClient)
        client._closed = True
        client._queue = asyncio.Queue()
        done_task = asyncio.get_running_loop().create_future()
        done_task.set_result(None)
        client._worker_task = done_task

        with self.assertRaises(RollingGoConnectionError) as ctx:
            await client._request_async("close")
        self.assertIn("dead", str(ctx.exception).lower())

if __name__ == "__main__":
    unittest.main()
