"""P4 拆分：住宿附着器（HotelAttacher）。

自 ``b_planner_hook.BPlannerHook`` 拆出（BPlannerHook 拆分后只剩编排）：
- 酒店选择写入计划（原 ``_attach_hotels``）
- 真源酒店池（原 ``_live_hotel_pool`` / ``_load_live_hotels_with_fallback`` /
  ``_live_hotel_provider_or_none``，B4 HotelTool / RollingGo MCP，失败回退假池）

**类身份约定**：``HotelAttacher`` 是 mixin，方法签名与原 BPlannerHook 私有
方法完全一致，经继承保留在 ``BPlannerHook`` 实例上（测试零改动）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import logging

logger = logging.getLogger("call_llm.planner_parts.hotels")


def _multi_city_enabled() -> bool:
    """D9 门控（规范源 trip_segments.multi_city_enabled，防循环导入经本包转发）。"""
    from call_llm.planner_parts.trip_segments import multi_city_enabled as _fn

    return _fn()


class HotelAttacher:
    """住宿附着（mixin）。

    依赖宿主实例属性：``requirement`` / ``_tool_provider`` / ``_use_live`` /
    ``city`` / ``_travel_time_provider``。
    """

    def _attach_hotels(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """把住宿安排写入计划（``plan["accommodation"]``，与 main.py 口径一致）。

        酒店初始规划接入（8.27，服务器先前不选酒店）：``select_hotels_for_plan``
        按晚数选常驻酒店并做预算 / 通勤校验，产出每晚 bookings + hotel_cost；
        ``plan_to_trip_timeline`` 消费它生成 hotel 段。无目的地 / 无景点 / 无酒店
        数据时返回 None，计划保持无住宿段（不阻断规划）。

        8.30 酒店真源（B4 HotelTool）：真源模式下注入 ``hotel_provider``（RollingGo
        MCP → 真源酒店候选）与 ``travel_time_provider``（矩阵真源分钟）；真源选店
        失败由 ``HotelSelector`` 内部回退假池，不阻断规划。
        """
        # D5（方案 c 阶段 4，2026-09-18）：多城时走跨城住宿（每城常驻一家，
        # 换城夜换宿），不再按首城常驻选型。
        if getattr(self, "city_plan", None) and _multi_city_enabled():
            self._attach_multi_city_hotels(plan)
            return plan
        try:
            from transport.hotels import select_hotels_for_plan

            # 8.29：真源矩阵模式注入 travel_time_provider → 酒店↔景点通勤走矩阵真源分钟
            # 8.30：hotel_provider = RollingGo 真源酒店（失败回退假池，见 HotelSelector）
            acc = select_hotels_for_plan(
                self.requirement,
                plan,
                hotel_provider=self._live_hotel_provider_or_none(),
                travel_time_provider=self._travel_time_provider,
            )
        except Exception as exc:  # noqa: BLE001  选酒店失败不阻断规划本身
            logger.warning("select_hotels_for_plan failed: %s", exc)
            acc = None
        if acc:
            plan["accommodation"] = acc
        return plan

    def _live_hotel_pool(self) -> List[Any]:
        """真源酒店候选（B4 HotelTool / RollingGo MCP），失败回退假池。

        8.30 酒店真源：``_use_live`` + 注入 ``tool_provider`` 时优先走
        ``make_live_hotel_provider``（真源酒店，含真实坐标/价格）；空池或
        工具异常 → 回退 ``load_hotels`` 假池（不阻断规划）。候选坐标随后
        并入阶段 1 矩阵 → 酒店↔景点通勤真源分钟。
        """
        if getattr(self, "_live_hotel_pool_cache", None) is None:
            self._live_hotel_pool_cache = self._load_live_hotels_with_fallback()
        return self._live_hotel_pool_cache

    def _load_live_hotels_with_fallback(self) -> List[Any]:
        """真源优先取酒店池；空 / 异常 / 未启用 → 假池。"""
        if self._use_live and self._tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                hotels = list(make_live_hotel_provider(self._tool_provider)(self.city))
                if hotels:
                    return hotels
                logger.warning("hotel 工具返回空池（city=%s），回退假池", self.city)
                # 降级告知（真源查不到 → 告知用户）
                self._add_notice("酒店真源查询返回空池，已用内置候选酒店替代")
            except Exception as exc:  # noqa: BLE001
                logger.warning("hotel 真源失败，回退假池：%s", exc)
                self._add_notice(f"酒店真源查询失败，已用内置候选酒店替代（{exc}）")
        try:
            from data_transmission.hotel import load_hotels

            return list(load_hotels(self.city))
        except Exception:  # noqa: BLE001
            return []

    # 州/区名 → 首府别名（2026-09-18 实测）：RollingGo hotel_search 对州名
    # （伊犁）不限定城市 → 返回默认城市（乌鲁木齐）数据；用首府名再拉即正确。
    _HOTEL_CITY_ALIAS: Dict[str, str] = {"伊犁": "伊宁"}

    def _live_hotel_pool_for(self, city: str) -> List[Any]:
        """按城拉酒店候选池（方案 c 阶段 4，D5 跨城住宿）。

        真源优先（hotel 工具按 city 参数化，天然支持多城）/ 假池兜底，
        per-city 缓存（`self._mc_hotel_pools`，hook 每次规划新建）。
        州名回退：city 命中 `_HOTEL_CITY_ALIAS` 且真源池为空时用首府名重拉
        （线上实测：city=伊犁 返回乌鲁木齐酒店，city=伊宁 返回正确伊宁池）。
        """
        cache = getattr(self, "_mc_hotel_pools", None)
        if cache is None:
            cache = {}
            self._mc_hotel_pools = cache
        if city in cache:
            return cache[city]
        # 别名命中 → 直接用别名查询（R3 实锤：RollingGo hotel_search 对州名
        # 不限定城市，「伊犁」返回乌鲁木齐非空池——空池判断接不住，须重定向）
        query_city = self._HOTEL_CITY_ALIAS.get(city, city)
        if query_city != city:
            logger.info("hotel 池按别名查询：%s → %s", city, query_city)
        pool: List[Any] = []
        if self._use_live and self._tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                pool = list(make_live_hotel_provider(self._tool_provider)(query_city))
                if not pool:
                    logger.warning("hotel 工具返回空池（city=%s），回退假池", city)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hotel 真源失败（city=%s）：%s", city, exc)
                pool = []
        if not pool:
            try:
                from data_transmission.hotel import load_hotels

                pool = list(load_hotels(city))
            except Exception:  # noqa: BLE001
                pool = []
        cache[city] = pool
        return pool

    def _attach_multi_city_hotels(self, plan: Dict[str, Any]) -> None:
        """D5 跨城住宿（方案 c 阶段 4）：每城常驻一家，night i 归属 day i 所属城。

        - 候选池：每城 `_live_hotel_pool_for(city)`（真源优先/假池兜底）；
        - 选店评分（口径对齐 HotelSelector.cost，通勤 v1 用直线距离）：
          `dist_km + 0.1×price + 60×(5−rating)`——锚点 = 该城首日首个 scenic；
        - bookings：night i（day i 之后那晚）归属 day i 所属城；换城夜
          `change=True` + reason="换城换宿"；`constant_hotel` = 首城店
          （消费方形状兼容，仅显示/单城语义）；`switched_days` 为解释结构；
        - `city_hotels`：每城一家（R1 治本，2026-09-18，由 picks 落盘）——
          多城消费方必须按城取 `city_hotels`，不得拿 `constant_hotel`
          （首城店）消费其他城市（返程头腿跨城驾车缺陷教训）；
        - `hotel_cost` = Σ price×晚数（_plan_cost_summary 单一来源零改动）；
        - 残差：跨城通勤评分未走矩阵（per-city 子矩阵留二期）。
        """
        import math

        city_plan = getattr(self, "city_plan", None) or []
        days = plan.get("days") or []
        if not city_plan or not days:
            return

        def city_of_day(n: int) -> Optional[str]:
            for cp in city_plan:
                if cp["day_from"] <= n <= cp["day_to"]:
                    return str(cp["city"])
            return None

        def _hf(h: Any, key: str, default: Any = None) -> Any:
            if isinstance(h, dict):
                return h.get(key, default)
            return getattr(h, key, default)

        def _hotel_latlng(h: Any):
            # location 三态：dict{lat,lng} / (lat,lng) 元组 / 顶层 lat+lng
            loc = _hf(h, "location")
            if isinstance(loc, dict):
                return loc.get("lat"), loc.get("lng")
            if isinstance(loc, (tuple, list)) and len(loc) >= 2:
                return loc[0], loc[1]
            return _hf(h, "lat"), _hf(h, "lng")

        # 每城评分锚点 = 该城首日首个 scenic 坐标
        anchor: Dict[str, Any] = {}
        for day in days:
            n = int(day.get("day") or 0)
            city = city_of_day(n)
            if city and city not in anchor:
                for it in day.get("items") or []:
                    if it.get("category") == "scenic":
                        anchor[city] = (it.get("lat"), it.get("lng"))
                        break

        def _dist_km(coord: Any, h: Any) -> float:
            hlat, hlng = _hotel_latlng(h)
            if not hlat or not hlng or not coord:
                return 0.0
            try:
                lat1, lng1 = float(hlat), float(hlng)
                lat2, lng2 = float(coord[0]), float(coord[1])
            except (TypeError, ValueError):
                return 0.0
            dlat = math.radians(lat2 - lat1)
            dlng = math.radians(lng2 - lng1)
            x = (math.sin(dlat / 2) ** 2
                 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2))
                 * math.sin(dlng / 2) ** 2)
            return 2 * 6371 * math.asin(math.sqrt(x))

        picks: Dict[str, Dict[str, Any]] = {}
        warnings: List[str] = []
        for cp in city_plan:
            city = str(cp["city"])
            pool = self._live_hotel_pool_for(city)
            if not pool:
                warnings.append(f"{city} 无酒店候选，该城夜晚未安排住宿")
                continue
            a = anchor.get(city)

            def _score(h: Any) -> float:
                rating = float(_hf(h, "rating", 0) or 0)
                price = float(_hf(h, "price_per_night", 0) or 0)
                return _dist_km(a, h) + 0.1 * price + 60 * (5 - rating)

            best = min(pool, key=_score)
            picks[city] = best
        if not picks:
            return

        def _entry(n: int, city: str, change: bool) -> Optional[Dict[str, Any]]:
            h = picks.get(city)
            if h is None:
                return None
            hlat, hlng = _hotel_latlng(h)
            return {
                "night": n,
                "hotel_id": str(_hf(h, "id", "")),
                "hotel_name": str(_hf(h, "name", "") or city),
                "lat": hlat,
                "lng": hlng,
                "price": float(_hf(h, "price_per_night", 0) or 0),
                "change": change,
                "reason": "换城换宿" if change else "",
                "commute_minutes": None,
            }

        bookings: List[Dict[str, Any]] = []
        switched: List[Dict[str, Any]] = []
        prev_city: Optional[str] = None
        hotel_cost = 0.0
        for day in days:
            n = int(day.get("day") or 0)
            city = city_of_day(n) or prev_city
            if not city:
                continue
            entry = _entry(n, city, prev_city is not None and city != prev_city)
            if entry is None:
                continue
            if entry["change"]:
                switched.append({
                    "day": n, "hotel_id": entry["hotel_id"],
                    "hotel_name": entry["hotel_name"],
                    "lat": entry["lat"], "lng": entry["lng"],
                    "commute_minutes": None, "reason": "换城换宿",
                })
            bookings.append(entry)
            hotel_cost += entry["price"]
            prev_city = city
        first_city = str(city_plan[0]["city"])
        first_entry = _entry(1, first_city, False)
        # R1 治本（2026-09-18）：每城一家落盘（picks 是唯一事实源），供
        # _refine_intercity_stations 等多城消费方按城取店；契约只增不改。
        city_hotels: Dict[str, Dict[str, Any]] = {}
        for _c, _h in picks.items():
            _lat, _lng = _hotel_latlng(_h)
            city_hotels[_c] = {
                "hotel_id": str(_hf(_h, "id", "")),
                "hotel_name": str(_hf(_h, "name", "") or _c),
                "lat": _lat,
                "lng": _lng,
                "price": float(_hf(_h, "price_per_night", 0) or 0),
            }
        plan["accommodation"] = {
            "constant_hotel": {
                "hotel_id": first_entry["hotel_id"] if first_entry else "",
                "hotel_name": first_entry["hotel_name"] if first_entry else "",
                "lat": first_entry["lat"] if first_entry else None,
                "lng": first_entry["lng"] if first_entry else None,
                "price": first_entry["price"] if first_entry else 0.0,
            },
            "city_hotels": city_hotels,
            "bookings": bookings,
            "switched_days": switched,
            "hotel_cost": hotel_cost,
            "nights": len(bookings),
            "warnings": warnings,
        }
        logger.info("多城住宿挂载：%s（Σ %.0f 元 / %d 晚）",
                    " → ".join(picks), hotel_cost, len(bookings))

    def _live_hotel_provider_or_none(self) -> Optional[Any]:
        """真源酒店 provider（供 ``select_hotels_for_plan`` 注入）；未启用 → None。

        仅注入函数本身（不在此调用）；执行失败由 ``HotelSelector`` 内部回退假池。
        """
        if self._use_live and self._tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                return make_live_hotel_provider(self._tool_provider)
            except Exception:  # noqa: BLE001
                return None
        return None