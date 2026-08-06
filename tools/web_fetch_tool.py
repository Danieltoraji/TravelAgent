"""网页抓取 Tool：URL → 正文提取。

Mock 版（WebFetchTool）：返回预设的模拟网页内容，测试用。
Live 版（WebFetchToolLive）：调用 WebClient 抓取真实网页 + BeautifulSoup 提取正文。

切换方式：build_registry() 按 settings.use_real_web 自动选择。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from tools.base_tool import BaseTool

logger = logging.getLogger("tools.web_fetch")

# 正文提取时移除的标签（噪声内容）
_NOISE_TAGS = ["script", "style", "nav", "footer", "aside", "header", "form", "noscript"]


class WebFetchTool(BaseTool):
    name = "web_fetch"
    description = "抓取指定 URL 的网页内容，提取标题、正文和链接。支持 CSS 选择器精确提取。"
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "目标网页 URL"},
            "selector": {"type": "string", "description": "可选 CSS 选择器，只提取匹配区域的文本"},
            "max_length": {"type": "integer", "description": "正文最大字符数（默认 5000）"},
        },
        "required": ["url"],
    }

    def _run(self, url: str = "", selector: str = "",
             max_length: int = 5000) -> Dict[str, Any]:
        """Mock 版：返回预设的模拟网页内容。"""
        if not url:
            raise ValueError("url is required")

        return {
            "url": url,
            "final_url": url,
            "title": "模拟网页标题",
            "text": (
                "这是模拟网页正文内容。\n"
                "故宫博物院当前开放时间为 08:30-17:00（16:00 停止入场）。\n"
                "每周一闭馆（法定节假日除外）。\n"
                "门票价格：旺季 60 元（4-10 月），淡季 40 元（11-3 月）。\n"
                "建议提前在官网预约购票。"
            ),
            "links": [
                {"text": "故宫博物院官网", "url": "https://www.dpm.org.cn/"},
                {"text": "在线购票", "url": "https://www.dpm.org.cn/Home.html"},
            ],
            "fetch_time": 0.0,
        }


class WebFetchToolLive(WebFetchTool):
    """真实网页抓取实现版。

    调用链路：
      1. WebClient.fetch(url) → 获取 HTML
      2. BeautifulSoup 解析 HTML → 移除噪声标签 → 提取标题/正文/链接

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    source = "live"

    def __init__(self, client: Any) -> None:
        """初始化 Live 版网页抓取 Tool。

        Args:
            client: WebClient 实例（共享超时配置）
        """
        super().__init__()
        self._client = client

    def _run(self, url: str = "", selector: str = "",
             max_length: int = 5000) -> Dict[str, Any]:
        if not url:
            raise ValueError("url is required")

        from bs4 import BeautifulSoup

        start = time.perf_counter()
        page = self._client.fetch(url)
        soup = BeautifulSoup(page.html, "html.parser")

        # 提取标题
        title = soup.title.get_text(strip=True) if soup.title else ""

        # 移除噪声标签
        for tag_name in _NOISE_TAGS:
            for tag in soup.find_all(tag_name):
                tag.decompose()

        # 正文提取：CSS 选择器精确提取 或 全文 get_text
        if selector:
            elements = soup.select(selector)
            text = "\n".join(el.get_text(separator=" ", strip=True) for el in elements)
        else:
            # 优先提取 <main> 或 <article>，否则用 <body>
            main = soup.find("main") or soup.find("article") or soup.body or soup
            text = main.get_text(separator="\n", strip=True)

        # 清理空白行
        lines = [line.strip() for line in text.split("\n") if line.strip()]
        text = "\n".join(lines)

        # 截断到 max_length
        if len(text) > max_length:
            text = text[:max_length] + "...[truncated]"

        # 提取页面内链接（最多 20 个）
        links: List[Dict[str, str]] = []
        if soup.body:
            for a in soup.body.find_all("a", href=True):
                href = a["href"]
                # 解析相对链接
                if href.startswith("/") or href.startswith("./"):
                    href = _resolve_url(page.final_url, href)
                if href.startswith("http"):
                    link_text = a.get_text(strip=True)[:100]
                    if link_text:
                        links.append({"text": link_text, "url": href})
                if len(links) >= 20:
                    break

        elapsed = round((time.perf_counter() - start) * 1000, 2)
        logger.info("WebFetch: %s → title=%s, text=%d chars, %d links, %.1fms",
                    page.final_url, title[:50], len(text), len(links), elapsed)

        return {
            "url": url,
            "final_url": page.final_url,
            "title": title,
            "text": text,
            "links": links,
            "fetch_time": elapsed,
        }


def _resolve_url(base: str, relative: str) -> str:
    """将相对 URL 解析为绝对 URL。"""
    from urllib.parse import urljoin
    return urljoin(base, relative)
