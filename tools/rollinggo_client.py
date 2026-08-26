"""RollingGo 酒店 MCP 客户端封装（健壮版）。

- 通过 MCP Streamable HTTP 连接 https://mcp.rollinggo.cn/mcp
- 使用 Bearer Token 认证
- 后台事件循环 + 单 worker 常驻，复用 MCP ClientSession（避免每次新建连接）
- 支持超时、失败重试、错误分类、结果解析容错
- 对外仍是同步接口（list_tools / call_tool / close）
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("tools.rollinggo")


class RollingGoError(Exception):
    """RollingGo MCP 调用基类错误。"""


class RollingGoAuthError(RollingGoError):
    """认证失败（401/403），不可重试。"""


class RollingGoTimeoutError(RollingGoError):
    """请求超时。"""


class RollingGoConnectionError(RollingGoError):
    """网络/连接类错误，可重试。"""


class RollingGoProtocolError(RollingGoError):
    """协议/结果解析错误，不可重试。"""


class RollingGoClient:
    """RollingGo MCP 客户端（同步门面，后台 worker + 会话复用）。"""

    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: float = 30.0,
        max_retries: int = 2,
        retry_backoff_base: float = 1.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout
        self.max_retries = max_retries
        self.retry_backoff_base = retry_backoff_base

        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._queue: Optional[asyncio.Queue] = None
        self._worker_task: Optional[asyncio.Task] = None
        self._closed = False
        self._lock = threading.Lock()
        self._start_loop()

    # -- 生命周期 ----------------------------------------------------------

    def _start_loop(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever,
            name="rollinggo-mcp-loop",
            daemon=True,
        )
        self._thread.start()
        # 等待 worker 初始化完成
        future = asyncio.run_coroutine_threadsafe(
            self._start_worker_async(), self._loop
        )
        future.result(timeout=min(self.timeout, 10))

    def close(self) -> None:
        """关闭后台事件循环与 MCP 会话。"""
        if self._closed:
            return
        self._closed = True
        if (
            self._loop is not None
            and self._queue is not None
            and self._worker_task is not None
            and not self._worker_task.done()
        ):
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._request_async("close"), self._loop
                )
                future.result(timeout=min(self.timeout, 10))
            except Exception:  # noqa: BLE001
                logger.debug("RollingGo close skipped/failed: %s", exc_info=True)
        if self._loop is not None:
            try:
                self._loop.call_soon_threadsafe(self._loop.stop)
            except Exception:  # noqa: BLE001
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    # -- 同步入口 ----------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        """发现 MCP 工具列表。"""
        return self._run_with_retry(lambda: self._request_async("list_tools"))

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用 MCP 工具并返回解析后的 JSON 结果。"""
        return self._run_with_retry(
            lambda: self._request_async("call_tool", name, arguments or {})
        )

    # -- 后台 worker -------------------------------------------------------

    async def _start_worker_async(self) -> None:
        self._queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._worker_async())

    async def _worker_async(self) -> None:
        """常驻 worker：在同一 task 内持有 MCP 会话，处理请求队列。"""
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        http_client = httpx2.AsyncClient(headers=self._headers(), timeout=self.timeout)
        stream_cm = streamable_http_client(self.url, http_client=http_client)
        read, write = await stream_cm.__aenter__()
        session = ClientSession(read, write)
        await session.__aenter__()
        await session.initialize()

        try:
            while True:
                request = await self._queue.get()
                kind = request[0]
                if kind == "close":
                    break
                fut = request[-1]
                try:
                    if kind == "list_tools":
                        tools = await session.list_tools()
                        fut.set_result([
                            {
                                "name": tool.name,
                                "description": tool.description,
                                "input_schema": tool.input_schema,
                            }
                            for tool in tools.tools
                        ])
                    else:
                        name = request[1]
                        arguments = request[2]
                        result = await session.call_tool(name, arguments=arguments)
                        if getattr(result, "is_error", False):
                            raise RollingGoProtocolError(
                                self._extract_text(result) or f"MCP tool error: {name}"
                            )
                        fut.set_result(self._parse_result(result))
                except Exception as exc:  # noqa: BLE001
                    fut.set_exception(self._classify_exception(exc))
        finally:
            # 在同一个 task 内退出 context manager，避免 AnyIO cancel scope 跨 task 问题
            try:
                await session.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("close MCP session failed")
            try:
                await stream_cm.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("close stream failed")
            try:
                await http_client.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001
                logger.exception("close http client failed")

    async def _request_async(self, kind: str, *args: Any) -> Any:
        """提交一个请求到 worker 队列，并等待结果。"""
        if self._closed or self._queue is None or self._worker_task is None:
            raise RollingGoConnectionError("RollingGo worker is not available")
        if self._worker_task.done():
            # worker 已退出：需要重建
            raise RollingGoConnectionError("RollingGo worker is dead")
        fut = asyncio.get_running_loop().create_future()
        await self._queue.put((kind, *args, fut))
        return await fut

    # -- 重试与错误分类 ----------------------------------------------------

    def _run_with_retry(self, coro_factory: Callable[[], Any]) -> Any:
        last_exc: Optional[BaseException] = None
        for attempt in range(self.max_retries + 1):
            try:
                future = self._submit(coro_factory())
                return future.result(timeout=self.timeout + 10)
            except Exception as exc:  # noqa: BLE001
                classified = self._classify_exception(exc)
                if isinstance(classified, (RollingGoAuthError, RollingGoProtocolError)):
                    raise classified from exc
                last_exc = classified
                logger.warning(
                    "RollingGo call failed (attempt %d/%d): %s",
                    attempt + 1,
                    self.max_retries + 1,
                    classified,
                )
                if attempt < self.max_retries:
                    self._restart_worker()
                    time.sleep(self.retry_backoff_base * (2 ** attempt))
                    continue
                raise classified from exc
        raise last_exc  # pragma: no cover

    def _submit(self, coro: Any) -> Any:
        if self._closed or self._loop is None:
            raise RollingGoError("RollingGoClient is closed")
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def _restart_worker(self) -> None:
        """重建 worker（连接断开/worker 死亡时使用）。"""
        if self._loop is None:
            return
        try:
            future = asyncio.run_coroutine_threadsafe(
                self._restart_worker_async(), self._loop
            )
            future.result(timeout=min(self.timeout, 10))
        except Exception:  # noqa: BLE001
            logger.exception("restart RollingGo worker failed")

    async def _restart_worker_async(self) -> None:
        if self._worker_task is not None and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except Exception:  # noqa: BLE001
                pass
        self._queue = asyncio.Queue()
        self._worker_task = asyncio.create_task(self._worker_async())

    # -- 内部 --------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _classify_exception(self, exc: BaseException) -> RollingGoError:
        """把底层异常归类为可重试/不可重试错误。"""
        if isinstance(exc, RollingGoError):
            return exc
        response = getattr(exc, "response", None)
        status = getattr(response, "status_code", None)
        if status in (401, 403):
            return RollingGoAuthError(str(exc))
        text = str(exc).lower()
        if "401" in text or "403" in text or "unauthorized" in text or "forbidden" in text:
            return RollingGoAuthError(str(exc))
        if "timed out" in text or "timeout" in text or isinstance(exc, TimeoutError):
            return RollingGoTimeoutError(str(exc))
        if "parse" in text or "schema" in text or "validation" in text:
            return RollingGoProtocolError(str(exc))
        return RollingGoConnectionError(str(exc))

    @staticmethod
    def _extract_text(result: Any) -> str:
        content = getattr(result, "content", None) or []
        for item in content:
            if getattr(item, "type", None) == "text":
                return getattr(item, "text", "") or ""
        return ""

    @staticmethod
    def _parse_result(result: Any) -> Dict[str, Any]:
        """把 MCP CallToolResult 转成 JSON dict。"""
        structured = getattr(result, "structured_content", None)
        if structured:
            return structured

        content = getattr(result, "content", None) or []
        for item in content:
            if getattr(item, "type", None) == "text":
                text = getattr(item, "text", "") or ""
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        return parsed
                    return {"data": parsed}
                except (TypeError, ValueError):
                    return {"text": text}
        return {"raw": str(result)}
