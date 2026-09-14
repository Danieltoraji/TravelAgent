"""满房 confirm 视图层回归（8.27 修复：500 空 body → 400 结构化信息）。

- ``POST /api/booking/{id}/confirm/`` 酒店满房失败：返回 HTTP 400，body 含
  ``error``（错误原因）、``booking.status=="failed"``、对应 Action ``blocked``。

多用户改造（2026-09）：视图从 ``request.runtime`` 取运行时——用例构造独立
``AgentRuntime`` 挂到请求上（干净态：无 timeline / 无 agent）。
"""

import json
import os
import sys
import unittest

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# django 未装入 site-packages（沙箱禁写，进度文档：Django 5.2.9 解压于 _smoke_tmp/site
# 以 PYTHONPATH 方式加载）——本仓库测试进程需显式挂载，否则收集期 ModuleNotFoundError。
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

# Django 最小配置（仅视图函数直接调用所需，不建库不启服务）
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
from runtime.agent_runtime import AgentRuntime  # noqa: E402


def _post_request(rt: AgentRuntime) -> HttpRequest:
    req = HttpRequest()
    req.method = "POST"
    req.path = "/api/booking/x/confirm/"
    req.runtime = rt
    return req   # body 默认空（HttpRequest.body 只读；confirm 视图不读请求体）


class TestBookingConfirmFullRoom(unittest.TestCase):
    def setUp(self) -> None:
        # 干净的独立运行时：无 timeline / 无 agent（失败回调直接返回，不发事件）
        self.rt = AgentRuntime()
        self.rt.timeline = None
        self.rt.agent = None

    def test_full_room_confirm_returns_400_with_details(self) -> None:
        rec = self.rt.booking_manager.prepare(
            place="皇城景观酒店（满房）", target_date="2026-08-04",
            party_size=2, booking_type="hotel",
        )
        resp = views.booking_confirm(_post_request(self.rt), rec.booking_id)
        body = json.loads(resp.content.decode("utf-8"))
        assert resp.status_code == 400, f"满房失败应 400，实际 {resp.status_code}"
        assert "满房" in body["error"]
        assert body["booking"]["status"] == "failed"
        assert any(a["status"] == "blocked" for a in body["actions"])

    def test_unknown_booking_still_404(self) -> None:
        resp = views.booking_confirm(_post_request(self.rt), "NOSUCHID")
        assert resp.status_code == 404


if __name__ == "__main__":
    unittest.main()
