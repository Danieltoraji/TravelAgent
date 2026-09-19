"""/static/ 决赛落地页端到端测试（2026-09-19 传单二维码入口）。

走 django.test.Client 全中间件链：
- 免鉴权：/static/ 落地页与 APK 下载无 Bearer token 可达（PUBLIC_PREFIXES）；
- 白名单：未登记文件名一律 404（防目录穿越/随手挂文件）；
- 门禁回归：业务端点无 token 仍 401。
"""

from __future__ import annotations

import os
import sys
import unittest

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


class StaticSiteTests(unittest.TestCase):
    def setUp(self) -> None:
        from django.test import Client

        self.client = Client()

    # -- 免鉴权可达 ---------------------------------------------------------

    def test_landing_no_auth_200(self):
        resp = self.client.get("/static/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp["Content-Type"])
        body = b"".join(resp.streaming_content).decode("utf-8")
        self.assertIn("TravelAgent", body)
        self.assertIn("自主旅行管家", body)

    def test_index_alias_200(self):
        resp = self.client.get("/static/index.html")
        self.assertEqual(resp.status_code, 200)

    def test_apk_no_auth_200(self):
        resp = self.client.get("/static/travel-app-v15.apk")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp["Content-Type"], "application/vnd.android.package-archive"
        )
        self.assertIn("travel-app-v15.apk", resp["Content-Disposition"])
        body = b"".join(resp.streaming_content)
        self.assertGreater(len(body), 1_000_000)  # 真安装包，非空壳

    def test_append_slash_redirect(self):
        resp = self.client.get("/static")
        self.assertEqual(resp.status_code, 301)
        self.assertTrue(resp["Location"].endswith("/static/"))

    # -- 文件名白名单 -------------------------------------------------------

    def test_unknown_file_404(self):
        resp = self.client.get("/static/not-registered.txt")
        self.assertEqual(resp.status_code, 404)

    def test_traversal_blocked(self):
        resp = self.client.get("/static/..%2Fsettings.py")
        self.assertEqual(resp.status_code, 404)

    # -- 门禁回归 -----------------------------------------------------------

    def test_business_endpoint_still_401(self):
        resp = self.client.get("/api/status/")
        self.assertEqual(resp.status_code, 401)

    def test_health_still_public(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)


if __name__ == "__main__":
    unittest.main()
