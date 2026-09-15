"""_candidate_spots_provider 测试（P1 第二层修复回归，2026-09-15）。

根因：BDecisionHook 候选池提供器只读本地假池——张掖类无假池城市 spots
加载失败 → 重规划降级「只决策不重规划」（new_timeline=None），满房换宿
从未执行。修复：USE_LIVE_DATA 开且注入工具时走真源池（与规划同源），
失败回落本地路径。
"""

from types import SimpleNamespace

import django
from django.test import SimpleTestCase, override_settings

os_setup = django.setup() if True else None

from runtime.a_interface import _candidate_spots_provider


def _requirement():
    return {"content": {
        "destination": "张掖", "start_date": "2026-09-21", "days": 6,
        "visitor_number": 1,
        "constraints": {"budget": 6000, "daily_travel_time": 480,
                        "must_visit": ["张掖七彩丹霞景区", "平山湖大峡谷"],
                        "required_tags": [], "dismissed_tags": []},
        "preferences": {"preferred_tags": ["自然风光"], "avoid_tags": []},
    }}


def _stub_tool():
    class _Tool:
        def call(self, name, **kwargs):
            if name == "scenic":
                return {"data": [
                    {"id": "Z1", "name": "张掖七彩丹霞景区", "alias": ["七彩丹霞"],
                     "location": {"lat": 38.97, "lng": 100.03}, "duration": 180,
                     "opening_time": "08:00", "closing_time": "18:00",
                     "price": 74, "tags": ["自然风光"]},
                    {"id": "Z2", "name": "平山湖大峡谷",
                     "location": {"lat": 39.05, "lng": 100.60}, "duration": 180,
                     "opening_time": "08:00", "closing_time": "18:00",
                     "price": 100, "tags": ["自然风光"]},
                    {"id": "Z3", "name": "大佛寺",
                     "location": {"lat": 38.94, "lng": 100.46}, "duration": 90,
                     "opening_time": "08:00", "closing_time": "17:00",
                     "price": 40, "tags": ["历史文化"]},
                ]}
            raise RuntimeError(f"no tool {name}")
    return _Tool()


class CandidateSpotsProviderTest(SimpleTestCase):
    @override_settings()
    def test_live_city_uses_live_pool(self):
        """live 城市（无本地假池）→ 真源池路径产出非空候选（P1 第二层修复）。"""
        import os
        os.environ["USE_LIVE_DATA"] = "1"
        try:
            provider = _candidate_spots_provider(_requirement(), _stub_tool())
            spots = provider("张掖")
            assert spots is not None
            names = [s.get("name") for group in spots for s in (group or [])]
            assert "张掖七彩丹霞景区" in names   # 必去经真源强拉入库
            assert "平山湖大峡谷" in names
        finally:
            os.environ.pop("USE_LIVE_DATA", None)

    def test_gate_off_falls_back_to_local(self):
        """门控关 → 本地 select_spots 路径（零回归）：无假池城市抛 ValueError
        （正是原缺陷表现——BDecisionHook 会捕获并降级「只决策」）。"""
        import os
        os.environ.pop("USE_LIVE_DATA", None)
        provider = _candidate_spots_provider(_requirement(), _stub_tool())
        with self.assertRaises(ValueError):
            provider("张掖")

    def test_live_failure_falls_back_to_local(self):
        """真源失败（工具炸）→ 回落本地路径（对无假池城市沿用原抛错行为）。"""
        import os

        class _Broken:
            def call(self, name, **kwargs):
                raise RuntimeError("scenic 故障（测试注入）")

        os.environ["USE_LIVE_DATA"] = "1"
        try:
            provider = _candidate_spots_provider(_requirement(), _Broken())
            with self.assertRaises(ValueError):
                provider("张掖")
        finally:
            os.environ.pop("USE_LIVE_DATA", None)
