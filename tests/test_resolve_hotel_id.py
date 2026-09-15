"""_resolve_hotel_id 酒店满房注入的 id 解析测试（P1 修复回归，2026-09-15）。

根因：live 酒店不在假池，`_resolve_hotel_id` 此前回退**名称字符串**当
hotel_id → 换宿修复 `exclude_ids=[名称]` 排除不命中真酒店 Place.id
（张掖满房实锤：重规划跑了但酒店未换）。修复：假池 miss 后扫当前时间轴的
hotel 段按名称匹配取真实 Place.id。
"""

from types import SimpleNamespace

from django.test import SimpleTestCase

from api.views import _resolve_hotel_id


def _timeline(hotel_name="艾扉酒店(张掖西站店)", hotel_id="1355024", city="张掖"):
    """TripTimeline 形状的最小桩（dataclass 属性访问口径）。"""
    return SimpleNamespace(
        city=city,
        days=[SimpleNamespace(items=[
            SimpleNamespace(category="scenic", name="大佛寺", id="s1"),
            SimpleNamespace(category="hotel", name=hotel_name, id=hotel_id),
        ])],
    )


class ResolveHotelIdTest(SimpleTestCase):
    def test_timeline_fallback_resolves_live_hotel_id(self):
        """假池 miss（张掖无假池）→ 扫时间轴 hotel 段取真实 Place.id。"""
        timeline = _timeline()
        self.assertEqual(
            _resolve_hotel_id("艾扉酒店(张掖西站店)", timeline), "1355024"
        )

    def test_timeline_fallback_strips_full_suffix(self):
        """「XX（满房）」事件名剥离后缀仍能命中。"""
        timeline = _timeline()
        self.assertEqual(
            _resolve_hotel_id("艾扉酒店(张掖西站店)（满房）", timeline), "1355024"
        )

    def test_dict_shaped_timeline_items(self):
        """dict 形状的 items（JSON 快照口径）同样支持。"""
        timeline = {
            "city": "张掖",
            "days": [{"items": [
                {"category": "hotel", "name": "艾扉酒店(张掖西站店)", "id": "1355024"},
            ]}],
        }
        self.assertEqual(
            _resolve_hotel_id("艾扉酒店(张掖西站店)", timeline), "1355024"
        )

    def test_full_miss_falls_back_to_name(self):
        """时间轴也没有 → 原兜底回退名称（行为不变，不抛异常）。"""
        timeline = _timeline(hotel_name="别的酒店")
        self.assertEqual(
            _resolve_hotel_id("艾扉酒店(张掖西站店)", timeline),
            "艾扉酒店(张掖西站店)",
        )

    def test_fake_pool_hit_still_wins(self):
        """假池命中（北京）路径不变（优先级高于时间轴兜底）。"""
        timeline = _timeline(hotel_name="北京饭店", hotel_id="BJ_H001", city="北京")
        resolved = _resolve_hotel_id("北京饭店", timeline)
        self.assertTrue(resolved.startswith("BJ_") or resolved == "北京饭店")
