"""网页工具测试：WebClient、WebFetchTool、WebSearchTool。"""

import unittest
from unittest.mock import MagicMock, patch

from tools.base_tool import ToolRegistry
from tools.web_client import SearchResult, WebClient, WebPage
from tools.web_fetch_tool import WebFetchTool, WebFetchToolLive
from tools.web_search_tool import WebSearchTool, WebSearchToolLive


# ---------------------------------------------------------------------------
# WebClient
# ---------------------------------------------------------------------------

class TestWebClientCharset(unittest.TestCase):
    def test_detect_charset_from_content_type(self) -> None:
        charset = WebClient._detect_charset(b"", "text/html; charset=utf-8")
        self.assertEqual(charset, "utf-8")

    def test_detect_charset_gb2312(self) -> None:
        charset = WebClient._detect_charset(b"", "text/html; charset=gb2312")
        self.assertEqual(charset, "gb2312")

    def test_detect_charset_default_utf8(self) -> None:
        charset = WebClient._detect_charset(b"", "text/html")
        self.assertEqual(charset, "utf-8")

    def test_detect_charset_from_meta_tag(self) -> None:
        raw = b'<html><head><meta charset="gbk"></head></html>'
        charset = WebClient._detect_charset(raw, "text/html")
        self.assertEqual(charset, "gbk")


class TestWebClientFetch(unittest.TestCase):
    @patch("tools.web_client.urlopen")
    def test_fetch_returns_webpage(self, mock_urlopen: MagicMock) -> None:
        """fetch() 返回 WebPage，正确解码 HTML。"""
        mock_resp = MagicMock()
        mock_resp.read.return_value = b"<html><body>Hello</body></html>"
        mock_resp.headers = {"Content-Type": "text/html; charset=utf-8"}
        mock_resp.status = 200
        mock_resp.url = "https://example.com"
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        client = WebClient(timeout=5.0)
        page = client.fetch("https://example.com")

        self.assertIsInstance(page, WebPage)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Hello", page.html)
        self.assertEqual(page.final_url, "https://example.com")

    @patch("tools.web_client.urlopen")
    def test_fetch_handles_gzip(self, mock_urlopen: MagicMock) -> None:
        """fetch() 自动解压 gzip 响应。"""
        import gzip
        html = "<html><body>Compressed content</body></html>"
        compressed = gzip.compress(html.encode("utf-8"))

        mock_resp = MagicMock()
        mock_resp.read.return_value = compressed
        mock_resp.headers = {"Content-Type": "text/html", "Content-Encoding": "gzip"}
        mock_resp.status = 200
        mock_resp.url = "https://example.com"
        mock_urlopen.return_value.__enter__ = MagicMock(return_value=mock_resp)
        mock_urlopen.return_value.__exit__ = MagicMock(return_value=False)

        client = WebClient(timeout=5.0)
        page = client.fetch("https://example.com")

        self.assertIn("Compressed content", page.html)

    @patch("tools.web_client.urlopen")
    def test_fetch_raises_connection_error_on_urlerror(self, mock_urlopen: MagicMock) -> None:
        """fetch() 在 URLError 时抛出 ConnectionError。"""
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("timeout")

        client = WebClient(timeout=5.0)
        with self.assertRaises(ConnectionError):
            client.fetch("https://example.com")


class TestWebClientSearch(unittest.TestCase):
    @patch.object(WebClient, "fetch")
    def test_search_parses_bing_html(self, mock_fetch: MagicMock) -> None:
        """search() 正确解析 Bing HTML 搜索结果。"""
        bing_html = """
        <html><body>
        <li class="b_algo">
            <h2><a href="https://www.dpm.org.cn/">故宫博物院</a></h2>
            <div class="b_caption"><p>故宫博物院官方网站</p></div>
        </li>
        <li class="b_algo">
            <h2><a href="https://www.visitbeijing.com.cn/">北京旅游网</a></h2>
            <div class="b_caption"><p>故宫开放时间及门票信息</p></div>
        </li>
        </body></html>
        """
        mock_fetch.return_value = WebPage(
            url="https://www.bing.com/search?q=test",
            status_code=200, headers={}, html=bing_html,
            final_url="https://www.bing.com/search?q=test",
        )

        client = WebClient(timeout=5.0)
        results = client.search("故宫", max_results=5)

        self.assertEqual(len(results), 2)
        self.assertIsInstance(results[0], SearchResult)
        self.assertEqual(results[0].title, "故宫博物院")
        self.assertEqual(results[0].url, "https://www.dpm.org.cn/")
        self.assertEqual(results[0].snippet, "故宫博物院官方网站")
        self.assertEqual(results[1].title, "北京旅游网")

    @patch.object(WebClient, "fetch")
    def test_search_respects_max_results(self, mock_fetch: MagicMock) -> None:
        """search() 遵守 max_results 限制。"""
        bing_html = """
        <html><body>
        <li class="b_algo"><h2><a href="https://a.com">A</a></h2><div class="b_caption"><p>s1</p></div></li>
        <li class="b_algo"><h2><a href="https://b.com">B</a></h2><div class="b_caption"><p>s2</p></div></li>
        <li class="b_algo"><h2><a href="https://c.com">C</a></h2><div class="b_caption"><p>s3</p></div></li>
        </body></html>
        """
        mock_fetch.return_value = WebPage(
            url="https://www.bing.com/search?q=test",
            status_code=200, headers={}, html=bing_html,
            final_url="https://www.bing.com/search?q=test",
        )

        client = WebClient(timeout=5.0)
        results = client.search("test", max_results=2)

        self.assertEqual(len(results), 2)


# ---------------------------------------------------------------------------
# WebFetchTool (Mock)
# ---------------------------------------------------------------------------

class TestWebFetchMock(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = WebFetchTool()

    def test_returns_correct_structure(self) -> None:
        result = self.tool.execute(url="https://example.com")
        self.assertEqual(result.status.value, "ok")
        data = result.data
        self.assertIn("url", data)
        self.assertIn("title", data)
        self.assertIn("text", data)
        self.assertIn("links", data)
        self.assertIn("fetch_time", data)

    def test_mock_returns_preset_content(self) -> None:
        result = self.tool.execute(url="https://example.com")
        data = result.data
        self.assertIn("故宫", data["text"])
        self.assertGreater(len(data["links"]), 0)

    def test_missing_url_raises(self) -> None:
        result = self.tool.execute()
        self.assertEqual(result.status.value, "error")

    def test_source_is_mock(self) -> None:
        self.assertEqual(self.tool.source, "mock")


# ---------------------------------------------------------------------------
# WebFetchTool (Live) — 用 patch 测试解析逻辑
# ---------------------------------------------------------------------------

class TestWebFetchLive(unittest.TestCase):
    def _make_live_tool(self, html: str) -> WebFetchToolLive:
        """构造一个 Live 版工具，fetch() 返回指定 HTML。"""
        client = MagicMock(spec=WebClient)
        client.fetch.return_value = WebPage(
            url="https://example.com",
            status_code=200, headers={},
            html=html,
            final_url="https://example.com",
        )
        return WebFetchToolLive(client)

    def test_extracts_title_and_text(self) -> None:
        html = """
        <html><head><title>故宫博物院</title></head>
        <body>
            <script>var x = 1;</script>
            <style>body { color: red; }</style>
            <nav>导航栏</nav>
            <footer>页脚</footer>
            <main>
                <h1>欢迎来到故宫</h1>
                <p>故宫开放时间为 08:30-17:00。</p>
            </main>
        </body></html>
        """
        tool = self._make_live_tool(html)
        result = tool.execute(url="https://example.com")

        self.assertEqual(result.status.value, "ok")
        data = result.data
        self.assertEqual(data["title"], "故宫博物院")
        self.assertIn("故宫开放时间", data["text"])
        # script/style/nav/footer 内容应被移除
        self.assertNotIn("var x = 1", data["text"])
        self.assertNotIn("color: red", data["text"])
        self.assertNotIn("导航栏", data["text"])
        self.assertNotIn("页脚", data["text"])

    def test_css_selector_extraction(self) -> None:
        html = """
        <html><body>
            <div class="content">正文内容</div>
            <div class="sidebar">侧边栏</div>
        </body></html>
        """
        tool = self._make_live_tool(html)
        result = tool.execute(url="https://example.com", selector=".content")

        data = result.data
        self.assertIn("正文内容", data["text"])
        self.assertNotIn("侧边栏", data["text"])

    def test_max_length_truncation(self) -> None:
        long_text = "A" * 10000
        html = f"<html><body><main>{long_text}</main></body></html>"
        tool = self._make_live_tool(html)
        result = tool.execute(url="https://example.com", max_length=100)

        data = result.data
        self.assertLessEqual(len(data["text"]), 120)  # 100 + truncation marker
        self.assertIn("[truncated]", data["text"])

    def test_extracts_links(self) -> None:
        html = """
        <html><body>
            <a href="https://example.com/page1">页面1</a>
            <a href="/page2">页面2</a>
            <a href="javascript:void(0)">JS链接</a>
        </body></html>
        """
        tool = self._make_live_tool(html)
        result = tool.execute(url="https://example.com")

        data = result.data
        urls = [link["url"] for link in data["links"]]
        self.assertIn("https://example.com/page1", urls)
        # 相对链接应被解析为绝对链接
        self.assertIn("https://example.com/page2", urls)
        # javascript: 链接应被过滤
        self.assertFalse(any("javascript" in u for u in urls))

    def test_source_is_live(self) -> None:
        tool = self._make_live_tool("<html></html>")
        self.assertEqual(tool.source, "live")


# ---------------------------------------------------------------------------
# WebSearchTool (Mock)
# ---------------------------------------------------------------------------

class TestWebSearchMock(unittest.TestCase):
    def setUp(self) -> None:
        self.tool = WebSearchTool()

    def test_returns_correct_structure(self) -> None:
        result = self.tool.execute(query="故宫")
        self.assertEqual(result.status.value, "ok")
        data = result.data
        self.assertIn("query", data)
        self.assertIn("results", data)
        self.assertIn("count", data)

    def test_mock_returns_preset_results(self) -> None:
        result = self.tool.execute(query="故宫")
        data = result.data
        self.assertGreater(data["count"], 0)
        first = data["results"][0]
        self.assertIn("title", first)
        self.assertIn("url", first)
        self.assertIn("snippet", first)

    def test_max_results_limit(self) -> None:
        result = self.tool.execute(query="故宫", max_results=1)
        data = result.data
        self.assertEqual(data["count"], 1)

    def test_missing_query_raises(self) -> None:
        result = self.tool.execute()
        self.assertEqual(result.status.value, "error")

    def test_source_is_mock(self) -> None:
        self.assertEqual(self.tool.source, "mock")


# ---------------------------------------------------------------------------
# WebSearchTool (Live) — 用 patch 测试解析逻辑
# ---------------------------------------------------------------------------

class TestWebSearchLive(unittest.TestCase):
    def test_returns_search_results(self) -> None:
        client = MagicMock(spec=WebClient)
        client.search.return_value = [
            SearchResult("故宫博物院", "https://www.dpm.org.cn/", "官方网站"),
            SearchResult("故宫门票", "https://www.ctrip.com/", "门票预约"),
        ]
        tool = WebSearchToolLive(client)
        result = tool.execute(query="故宫", max_results=5)

        self.assertEqual(result.status.value, "ok")
        data = result.data
        self.assertEqual(data["query"], "故宫")
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["results"][0]["title"], "故宫博物院")
        self.assertEqual(data["results"][0]["url"], "https://www.dpm.org.cn/")

    def test_source_is_live(self) -> None:
        client = MagicMock(spec=WebClient)
        tool = WebSearchToolLive(client)
        self.assertEqual(tool.source, "live")


# ---------------------------------------------------------------------------
# Registry 集成
# ---------------------------------------------------------------------------

class TestWebToolRegistry(unittest.TestCase):
    def test_registry_includes_web_tools(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        from config.settings import settings
        old_demo = settings.demo_mode
        settings.demo_mode = True  # 强制 Mock 模式
        try:
            registry = build_registry(MockWorld())
            names = registry.names()
            self.assertIn("web_fetch", names)
            self.assertIn("web_search", names)
        finally:
            settings.demo_mode = old_demo

    def test_registry_call_web_fetch_mock(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        from config.settings import settings
        old_demo = settings.demo_mode
        settings.demo_mode = True  # 强制 Mock 模式
        try:
            registry = build_registry(MockWorld())
            result = registry.call("web_fetch", url="https://example.com")
            self.assertEqual(result.status.value, "ok")
            self.assertIn("text", result.data)
        finally:
            settings.demo_mode = old_demo

    def test_registry_call_web_search_mock(self) -> None:
        from tools import build_registry
        from tools.mock_data import MockWorld
        from config.settings import settings
        old_demo = settings.demo_mode
        settings.demo_mode = True  # 强制 Mock 模式，避免真实网络请求
        try:
            registry = build_registry(MockWorld())
            result = registry.call("web_search", query="故宫")
            self.assertEqual(result.status.value, "ok")
            self.assertGreater(result.data["count"], 0)
        finally:
            settings.demo_mode = old_demo


if __name__ == "__main__":
    unittest.main()
