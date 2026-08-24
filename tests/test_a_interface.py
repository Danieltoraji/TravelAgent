"""回归测试：runtime.a_interface.build_planner_hook 必须把 tool_provider 透传给 BPlannerHook。

背景（0825 修复）：build_planner_hook 此前签名收参但未透传 tool_provider，
导致服务器上 USE_LIVE_DATA=1 也永远 last_data_source="fake"（C 端只见假数据）。

本测试走与服务器相同的构造路径（a_interface → BPlannerHook），
用假真源 provider 断言 live 启用 / 关闭两态。
"""

import os
import sys
import unittest

# 与服务器运行上下文对齐：django_server（runtime 包）+ a_side（A 侧代码）
_B_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from runtime.a_interface import build_planner_hook  # noqa: E402


_LIVE_SPOTS = [
    {"id": "L1", "name": "故宫博物院", "alias": ["故宫"], "location": {"lat": 39.916, "lng": 116.397}, "duration": 180, "opening_time": "08:30", "closing_time": "17:00", "price": 60, "tags": ["历史文化"]},
    {"id": "L2", "name": "天坛公园", "location": {"lat": 39.882, "lng": 116.406}, "duration": 120, "opening_time": "08:00", "closing_time": "22:00", "price": 35, "tags": ["古建筑"]},
    {"id": "L3", "name": "香山公园", "location": {"lat": 39.99, "lng": 116.19}, "duration": 150, "opening_time": "06:00", "closing_time": "18:00", "price": 10, "tags": ["自然"]},
    {"id": "L4", "name": "颐和园", "location": {"lat": 39.999, "lng": 116.275}, "duration": 240, "opening_time": "06:30", "closing_time": "18:00", "price": 30, "tags": ["历史文化", "自然"]},
    {"id": "L5", "name": "景山公园", "location": {"lat": 39.923, "lng": 116.397}, "duration": 90, "opening_time": "06:30", "closing_time": "21:00", "price": 2, "tags": ["自然"]},
    {"id": "L6", "name": "什刹海", "location": {"lat": 39.94, "lng": 116.383}, "duration": 240, "opening_time": "08:00", "closing_time": "20:00", "price": 40, "tags": ["文化", "自然"]},
]


class _FakeTool:
    def __init__(self, fail_scenic=False, fail_map=False):
        self.fail_scenic = fail_scenic
        self.fail_map = fail_map
        self.calls = []

    def call(self, name, **kwargs):
        self.calls.append((name, kwargs))
        if name == "scenic":
            if self.fail_scenic:
                raise RuntimeError("scenic 故障（测试注入）")
            return {"data": [dict(s) for s in _LIVE_SPOTS]}
        if name == "map":
            if self.fail_map:
                raise RuntimeError("map 故障（测试注入）")
            if kwargs.get("action") == "batch_route":
                rows = []
                for o in kwargs["origins"]:
                    for d in kwargs["destinations"]:
                        rows.append(
                            {"origin": o, "destination": d, "distance_km": 3.0,
                             "transport_minutes": 25}
                        )
                return {"rows": rows}
            return {"transport_minutes": 30, "distance_km": 3.0}
        raise RuntimeError(f"no tool {name}")


def _requirement():
    return {
        "content": {
            "destination": "北京",
            "start_date": "2026-08-04",
            "days": 2,
            "visitor_number": 1,
            "constraints": {
                "budget": 2000,
                "must_visit": ["故宫"],
                "required_tags": [],
                "dismissed_tags": [],
                "daily_travel_time": 480,
                "include_meal_time_in_daily_limit": False,
            },
            "preferences": {"preferred_tags": ["历史文化"], "avoid_tags": []},
        }
    }


class TestPlannerHookThreadsToolProvider(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("USE_LIVE_DATA", None)

    def test_live_engaged_when_provider_passed_and_switch_on(self) -> None:
        os.environ["USE_LIVE_DATA"] = "1"
        tool = _FakeTool()
        hook = build_planner_hook(_requirement(), tool_provider=tool)
        timeline = hook.generate_timeline()
        self.assertEqual(hook.last_data_source, "live")   # 关键：透传生效
        self.assertIsNone(hook.last_error)
        map_calls = [c for c in tool.calls if c[0] == "map"]
        self.assertTrue(map_calls, "live 模式应调用 map.batch_route 构建交通矩阵")
        self.assertTrue(all(kw["action"] == "batch_route" for _, kw in map_calls))
        self.assertTrue(timeline.days)

    def test_live_falls_back_on_scenic_failure(self) -> None:
        os.environ["USE_LIVE_DATA"] = "1"
        tool = _FakeTool(fail_scenic=True)
        hook = build_planner_hook(_requirement(), tool_provider=tool)
        timeline = hook.generate_timeline()
        self.assertEqual(hook.last_data_source, "live_fallback")
        self.assertIn("真实数据接入失败", hook.last_error or "")
        self.assertTrue(timeline.days)

    def test_switch_off_keeps_fake_and_never_calls_tools(self) -> None:
        os.environ.pop("USE_LIVE_DATA", None)   # 开关关闭
        tool = _FakeTool()
        hook = build_planner_hook(_requirement(), tool_provider=tool)
        timeline = hook.generate_timeline()
        self.assertEqual(hook.last_data_source, "fake")
        self.assertEqual([c[0] for c in tool.calls], [])
        self.assertTrue(timeline.days)


if __name__ == "__main__":
    unittest.main()
