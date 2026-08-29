"""网页搜索 Tool：关键词 → 搜索结果列表。

Mock 版（WebSearchTool）：返回预设的模拟搜索结果，测试用。
Live 版（WebSearchToolLive）：调用 WebClient 搜索 DuckDuckGo，返回真实结果。

切换方式：build_registry() 按 settings.use_real_web 自动选择。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from tools.base_tool import BaseTool

logger = logging.getLogger("tools.web_search")


class WebSearchTool(BaseTool):
    name = "web_search"
    domain = "web"
    description = "搜索关键词，返回相关网页列表（标题、URL、摘要）。用于查找景点官网、闭馆公告等信息。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "搜索关键词"},
            "max_results": {"type": "integer", "description": "最大返回结果数（默认 5）"},
        },
        "required": ["query"],
    }

    def _run(self, query: str = "", max_results: int = 5) -> Dict[str, Any]:
        """Mock 版：返回预设的模拟搜索结果。"""
        if not query:
            raise ValueError("query is required")

        mock_results = [
            {
                "title": "故宫博物院官方网站",
                "url": "https://www.dpm.org.cn/",
                "snippet": "故宫博物院官方网站，提供开放时间、门票预约、展览信息等服务。",
            },
            {
                "title": "故宫博物院开放时间及门票 - 北京旅游网",
                "url": "https://www.visitbeijing.com.cn/",
                "snippet": "故宫开放时间：8:30-17:00（16:00 停止入场），每周一闭馆。",
            },
            {
                "title": "故宫门票预约指南 2026 - 携程旅行",
                "url": "https://www.ctrip.com/",
                "snippet": "故宫门票需提前网上预约，旺季 60 元，淡季 40 元。",
            },
        ]
        results = mock_results[:max_results]
        return {
            "query": query,
            "results": results,
            "count": len(results),
        }


class WebSearchToolLive(WebSearchTool):
    """真实搜索实现版。

    调用链路：
      1. WebClient.search(query, max_results) → DuckDuckGo HTML 接口
      2. 解析搜索结果 → 标题/URL/摘要

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    source = "live"

    def __init__(self, client: Any) -> None:
        """初始化 Live 版搜索 Tool。

        Args:
            client: WebClient 实例（共享超时配置）
        """
        super().__init__()
        self._client = client

    def _run(self, query: str = "", max_results: int = 5) -> Dict[str, Any]:
        if not query:
            raise ValueError("query is required")

        search_results = self._client.search(query, max_results=max_results)
        results: List[Dict[str, str]] = [
            r.to_dict() for r in search_results
        ]

        logger.info("WebSearch: '%s' → %d results", query, len(results))

        return {
            "query": query,
            "results": results,
            "count": len(results),
        }
