"""timeline_validator 深度可行性校验测试（chat v2.2）。

覆盖：闭馆（早于开门/超闭馆）、每日时长（超 daily_travel_time）、
预算（超 budget）、通过路径、候选池 duration 匹配。
"""

import os
import sys
import unittest
from datetime import date

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"),
           os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)
_site = os.path.join(_B_ROOT, "..", "_smoke_tmp", "site")
if os.path.isdir(_site) and _site not in sys.path:
    sys.path.insert(0, _site)

from core.schemas import DayPlan, Place, TripTimeline  # noqa: E402
from api.timeline_validator import (  # noqa: E402
    DEFAULT_MEAL_MIN,
    DEFAULT_SPOT_MIN,
    DEFAULT_TRANSPORT_MIN,
    validate_timeline,
)

REQ = {
    "content": {
        "destination": "北京", "days": 2,
        "constraints": {"budget": 2000, "daily_travel_time": 480},
        "preferences": {"preferred_tags": ["历史文化"]},
    }
}

# 假候选池：duration 供校验器匹配
FAKE_POOL = [
    [
        {"id": "BJ_001", "name": "故宫博物院", "alias": ["故宫"], "duration": 360},
        {"id": "BJ_006", "name": "景山公园", "alias": ["景山"], "duration": 60},
    ],
    [],
    [
        {"id": "BJ_004", "name": "天坛公园", "alias": ["天坛"], "duration": 120},
    ],
]


def _tl(days_items):
    """构造时间轴：days_items = [[(name, category, arrival, end, open, price), ...], ...]"""
    days = []
    for i, items in enumerate(days_items, start=1):
        place_items = []
        for (name, cat, arrival, end, open_time, price) in items:
            place_items.append(Place(
                name=name, category=cat, arrival=arrival,
                end_time=end, open_time=open_time, price=price,
            ))
        days.append(DayPlan(
            day=i, date=date(2026, 8, i),
            items=place_items,
        ))
    return TripTimeline(
        city="北京", start_date=date(2026, 8, 1),
        end_date=date(2026, 8, len(days_items)), days=days,
    )


class TestTimelineValidator(unittest.TestCase):
    def test_valid_timeline_passes(self) -> None:
        tl = _tl([[
            ("故宫博物院", "scenic", "09:00", "15:00", "09:00-17:00", 60),
            ("景山公园", "scenic", "15:18", "16:18", "06:30-21:00", 2),
            ("晚餐", "food", "17:30", "18:30", "", 100),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=FAKE_POOL)
        self.assertEqual(errors, [])

    def test_closing_before_open(self) -> None:
        tl = _tl([[
            ("故宫博物院", "scenic", "08:00", "14:00", "09:00-17:00", 60),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=FAKE_POOL)
        self.assertEqual(len(errors), 1)
        self.assertIn("早于开门时间", errors[0])

    def test_closing_after_close(self) -> None:
        # end_time 缺失 → 用候选池 duration（故宫 360min）估算 → 超闭馆
        tl = _tl([[
            ("故宫博物院", "scenic", "15:00", "", "09:00-17:00", 60),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=FAKE_POOL)
        self.assertEqual(len(errors), 1)
        self.assertIn("超过闭馆时间", errors[0])

    def test_closing_ok_when_duration_fits(self) -> None:
        # 景山公园 duration 60min，15:18 到达 → 16:18 结束，闭馆 21:00 前
        tl = _tl([[
            ("景山公园", "scenic", "15:18", "", "06:30-21:00", 2),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=FAKE_POOL)
        self.assertEqual(errors, [])

    def test_daily_limit_exceeded(self) -> None:
        # 故宫360 + 景山60 + 天坛120 + 交通30 + 餐饮60×2 = 690 > 480
        tl = _tl([[
            ("故宫博物院", "scenic", "09:00", "15:00", "09:00-17:00", 60),
            ("景山公园", "scenic", "15:18", "16:18", "06:30-21:00", 2),
            ("天坛公园", "scenic", "16:30", "18:30", "06:00-22:00", 15),
            ("午餐", "food", "12:00", "13:00", "", 50),
            ("晚餐", "food", "19:00", "20:00", "", 50),
            ("交通", "transport", "08:30", "09:00", "", 0),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=FAKE_POOL)
        self.assertEqual(len(errors), 1)
        self.assertIn("第1天超时", errors[0])
        self.assertIn("690", errors[0])

    def test_budget_exceeded(self) -> None:
        tl = _tl([[
            ("故宫博物院", "scenic", "09:00", "15:00", "09:00-17:00", 3000),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=FAKE_POOL)
        self.assertEqual(len(errors), 1)
        self.assertIn("预算超支", errors[0])

    def test_missing_candidate_pool_uses_defaults(self) -> None:
        # 无候选池：未知景点用默认 90min；闭馆按默认估算
        tl = _tl([[
            ("神秘景点", "scenic", "16:00", "", "09:00-17:00", 0),
        ]])
        errors = validate_timeline(tl, REQ, candidate_pool=[])
        self.assertEqual(len(errors), 1)
        self.assertIn("超过闭馆时间", errors[0])  # 16:00 + 90 > 17:00

    def test_no_requirement_defaults_skip_limits(self) -> None:
        # 无 requirement（无预算/无每日上限）→ 只做闭馆校验
        tl = _tl([[
            ("故宫博物院", "scenic", "09:00", "15:00", "09:00-17:00", 99999),
        ]])
        errors = validate_timeline(tl, None, candidate_pool=FAKE_POOL)
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
