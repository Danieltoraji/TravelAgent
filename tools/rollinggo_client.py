"""RollingGo 酒店 MCP 客户端封装。

- 通过 MCP Streamable HTTP 连接 https://mcp.rollinggo.cn/mcp
- 使用 Bearer Token 认证
- 提供 list_tools / call_tool，并把 MCP 异步调用桥接为同步调用
  （在独立线程里跑 asyncio.run，避免与 Django/ExecutionAgent 的事件循环冲突）
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, List, Optional

logger = logging.getLogger("tools.rollinggo")


class RollingGoClient:
    """RollingGo MCP 客户端（同步门面，内部异步执行）。"""

    def __init__(
        self,
        url: str,
        api_key: str,
        timeout: float = 30.0,
    ) -> None:
        self.url = url
        self.api_key = api_key
        self.timeout = timeout

    # -- 同步入口 ----------------------------------------------------------

    def list_tools(self) -> List[Dict[str, Any]]:
        """发现 MCP 工具列表。"""
        return self._run(self._list_tools_async())

    def call_tool(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """调用 MCP 工具并返回解析后的 JSON 结果。"""
        return self._run(self._call_tool_async(name, arguments or {}))

    # -- 异步桥接 ----------------------------------------------------------

    def _run(self, coro: Any) -> Any:
        """在独立线程中运行异步协程，避免当前线程已有事件循环时报错。"""
        with ThreadPoolExecutor(max_workers=1) as pool:
            return pool.submit(lambda: self._asyncio_run(coro)).result()

    @staticmethod
    def _asyncio_run(coro: Any) -> Any:
        import asyncio

        return asyncio.run(coro)

    # -- MCP 异步实现 ------------------------------------------------------

    async def _list_tools_async(self) -> List[Dict[str, Any]]:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = self._headers()
        async with httpx2.AsyncClient(headers=headers, timeout=self.timeout) as http_client:
            async with streamable_http_client(self.url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    return [
                        {
                            "name": tool.name,
                            "description": tool.description,
                            "input_schema": tool.input_schema,
                        }
                        for tool in tools.tools
                    ]

    async def _call_tool_async(
        self,
        name: str,
        arguments: Dict[str, Any],
    ) -> Dict[str, Any]:
        import httpx2
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client

        headers = self._headers()
        async with httpx2.AsyncClient(headers=headers, timeout=self.timeout) as http_client:
            async with streamable_http_client(self.url, http_client=http_client) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    result = await session.call_tool(name, arguments=arguments)
                    return self._parse_result(result)

    # -- 内部 --------------------------------------------------------------

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

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
                    return json.loads(text)
                except (TypeError, ValueError):
                    return {"text": text}
        return {"raw": str(result)}
