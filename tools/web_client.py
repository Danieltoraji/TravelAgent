"""网页客户端：统一 HTTP 请求封装 + 搜索引擎接口。

所有 Live 版网页相关 Tool 共用同一个 WebClient 实例。
认证方式：无（直接请求公开网页）。

遵循 AmapClient 的 urllib 模式：gzip 解压 + URLError → ConnectionError + 超时控制。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import quote_plus, urljoin
from urllib.request import Request, urlopen

logger = logging.getLogger("tools.web")

# 真实浏览器 User-Agent（避免被部分网站拒绝）
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

_BING_URL = "https://www.bing.com/search"


class WebPage:
    """一次网页抓取的结果。"""

    def __init__(self, url: str, status_code: int, headers: Dict[str, str],
                 html: str, final_url: str) -> None:
        self.url = url               # 原始请求 URL
        self.status_code = status_code
        self.headers = headers
        self.html = html             # 解码后的 HTML 文本
        self.final_url = final_url   # 重定向后的最终 URL


class SearchResult:
    """一条搜索结果。"""

    def __init__(self, title: str, url: str, snippet: str) -> None:
        self.title = title
        self.url = url
        self.snippet = snippet

    def to_dict(self) -> Dict[str, str]:
        return {"title": self.title, "url": self.url, "snippet": self.snippet}


class WebClient:
    """网页抓取 + 搜索客户端（无 API Key 需求）。

    用法::

        client = WebClient()
        page = client.fetch("https://example.com")   # 抓取网页
        results = client.search("故宫 闭馆公告")       # 搜索
    """

    def __init__(self, timeout: float | None = None) -> None:
        from config.settings import settings
        self._timeout = timeout if timeout is not None else settings.api_timeout

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def fetch(self, url: str) -> WebPage:
        """抓取指定 URL，返回 WebPage（含 HTML 文本）。

        自动处理：gzip 解压、charset 检测、HTTP 重定向。
        """
        req = Request(url)
        req.add_header("User-Agent", _USER_AGENT)
        req.add_header("Accept-Encoding", "gzip")
        logger.debug("FETCH %s", url)

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                # charset 检测：HTTP header > meta 标签 > 默认 utf-8
                charset = self._detect_charset(raw, resp.headers.get("Content-Type", ""))
                html = raw.decode(charset, errors="replace")
                final_url = resp.url or url
                status_code = resp.status
                headers = dict(resp.headers)
        except URLError as exc:
            raise ConnectionError(f"网页请求失败 [{url}]: {exc}") from exc

        logger.info("Fetched %s → %d chars, status=%d", final_url, len(html), status_code)
        return WebPage(
            url=url, status_code=status_code, headers=headers,
            html=html, final_url=final_url,
        )

    def search(self, query: str, max_results: int = 5) -> List[SearchResult]:
        """用 Bing 搜索，返回结果列表。

        Bing 搜索结果页的 HTML 结构：
        - 每条结果在 ``<li class="b_algo">`` 容器中
        - 标题链接为 ``h2 > a``，href 即真实目标 URL（无重定向）
        - 摘要在 ``.b_caption p`` 或结果内的 ``<p>`` 标签中
        """
        from bs4 import BeautifulSoup

        url = f"{_BING_URL}?q={quote_plus(query)}&setlang=zh-CN"
        page = self.fetch(url)
        soup = BeautifulSoup(page.html, "html.parser")

        results: List[SearchResult] = []
        for item in soup.select("li.b_algo"):
            link = item.select_one("h2 a")
            if link is None:
                continue
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if href.startswith("//"):
                href = "https:" + href
            elif href.startswith("/"):
                href = urljoin(_BING_URL, href)

            # 摘要：优先 .b_caption p，否则取结果内第一个 <p>
            snippet_el = item.select_one(".b_caption p") or item.select_one("p")
            snippet = snippet_el.get_text(strip=True) if snippet_el else ""

            results.append(SearchResult(title=title, url=href, snippet=snippet))
            if len(results) >= max_results:
                break

        logger.info("Search '%s' → %d results", query, len(results))
        return results

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_charset(raw: bytes, content_type: str) -> str:
        """从 HTTP Content-Type header 或 HTML meta 标签检测编码。"""
        # 1. HTTP Content-Type 中的 charset
        for part in content_type.split(";"):
            part = part.strip().lower()
            if part.startswith("charset="):
                return part.split("=", 1)[1].strip().strip('"')
        # 2. HTML meta 标签中的 charset
        head = raw[:2048].decode("ascii", errors="ignore")
        for pattern in ('charset="', "charset='", "charset="):
            idx = head.lower().find(pattern)
            if idx != -1:
                start = idx + len(pattern)
                end = head.find('"', start) if pattern.endswith('"') else None
                if end is None:
                    end = start + 20
                candidate = head[start:end].strip().rstrip('"').rstrip("'")
                if candidate and candidate.isascii():
                    return candidate
        # 3. 默认 utf-8
        return "utf-8"