"""酒店选择器：默认常驻一家，通勤超阈值才换宿。

规则（8.20 确认，替换「每晚自由换宿」）：
- **默认常驻**：全行程住一家酒店 H*，按「总通勤 + 价格 + 评分」的加权代价最小选出。
- **阈值换宿**：某天「常驻酒店 → 当日第一个景点」通勤 ≥ ``MAX_SINGLE_HOTEL_COMMUTE``
  （默认 120 分钟）且存在一家候选酒店到该景点通勤 ≤ ``SWITCH_TARGET_MAX``
  （半阈值 60 分钟）时，该晚换成最接近的那家——通勤收益足够大才值得付换宿代价。
- **换宿代价由阈值本身压制**：换宿不产生额外交通段（退房/行李为现实动作，不计入游玩时长）。
- **预算内才换**：换到更贵的酒店导致总预算超时放弃换宿并提示。

通勤时间（8.29 起）双轨：
- **真源模式**（注入 ``travel_time_provider``，即开启真源矩阵时）：酒店↔景点分钟
  直接取矩阵真源值（酒店候选坐标已并入 batch_route 矩阵）——与景点间/景点↔餐厅
  一致，不再用直线距离折算；
- **假数据模式**（无 provider）：按酒店与景点的经纬度直线距离折算
  （城市均速 28 km/h，最短 15 分钟），与 `fake_spots/{city}/spots_graph.json`
  的餐厅↔景点边一样属于模拟数据；酒店节点不进 spots_graph
  （选择器自算自洽，展示时同样用本模块口径）。
"""

from __future__ import annotations

import json
import math
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    from data_transmission.city_graph import DEFAULT_GRAPH_DIR, match_city_spots
    from data_transmission.hotel import Hotel, load_hotels
except ModuleNotFoundError:
    from ..data_transmission.city_graph import DEFAULT_GRAPH_DIR, match_city_spots
    from ..data_transmission.hotel import Hotel, load_hotels

# 换宿阈值（分钟）：常驻酒店 → 当日首景点超此值才允许换宿。
# 8.30 调整 120→60：地级市远郊场景（demo1 张掖）市区→七彩丹霞 ~50min、
# →山丹马场 ~75min，在旧 120 阈值下永不触发换宿；60 让「丹霞日住临泽、
# 马场日住山丹」可行（用户拍板：两小时对地级市内通勤太长）。
MAX_SINGLE_HOTEL_COMMUTE = 60
# 换宿目标阈值（分钟）：换宿酒店的当日通勤必须 ≤ 此值（半阈值）——
# 若换宿后通勤仍很长（如所有酒店离该景点都 200+ 分钟），换宿收益不足以
# 抵消换宿代价，维持常驻。
SWITCH_TARGET_MAX = MAX_SINGLE_HOTEL_COMMUTE // 2
# 缺边/缺数据时的惩罚通勤值
LARGE_COMMUTE = 999
# 常驻评分权重：cost = TRAVEL_WEIGHT·Σ通勤 + PRICE_WEIGHT·价格 + RATING_WEIGHT·(5−评分)
TRAVEL_WEIGHT = 1.0
PRICE_WEIGHT = 0.1
RATING_WEIGHT = 60.0
# 城市均速（km/h）与最短通勤（分钟）
CITY_SPEED_KMH = 28.0
MIN_COMMUTE_MINUTES = 15

# 价位段：经济 ≤ ECON_MAX，豪华 > LUX_MIN，中间为舒适
ECONOMIC_MAX_PRICE = 500
LUXURY_MIN_PRICE = 800

_PRICE_LEVEL_ALIASES = {
    "经济": "economic",
    "经济型": "economic",
    "economy": "economic",
    "舒适": "comfort",
    "舒适型": "comfort",
    "comfort": "comfort",
    "豪华": "luxury",
    "豪华型": "luxury",
    "luxury": "luxury",
}


def _normalize_price_level(value: Optional[str]) -> Optional[str]:
    if not value:
        return None
    return _PRICE_LEVEL_ALIASES.get(str(value).strip().lower())


def _haversine_km(
    lat1: float, lng1: float, lat2: float, lng2: float
) -> float:
    radius = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = (
        math.sin(dphi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(a))


def _load_spot_locations(city: str, data_dir: Path) -> Dict[str, Tuple[float, float]]:
    """加载 {spot_id: (lat, lng)}；spots.json 带 BOM，用 utf-8-sig 读。"""
    locations: Dict[str, Tuple[float, float]] = {}
    try:
        path = match_city_spots(city, data_dir)
        raw = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError, json.JSONDecodeError):
        return locations
    spots = raw.get("spots", raw) if isinstance(raw, dict) else raw
    for spot in spots:
        location = spot.get("location") or {}
        try:
            locations[str(spot["id"])] = (
                float(location.get("lat", 0.0)),
                float(location.get("lng", 0.0)),
            )
        except (KeyError, TypeError, ValueError):
            continue
    return locations


class HotelSelector:
    """按「默认常驻 + 阈值换宿」规则为多日行程选酒店。

    用法：:
        selector = HotelSelector(requirement["content"]["destination"])
        result = selector.select(
            daily_first_last=[("BJ_001", "BJ_004"), ("BJ_002", "BJ_006")],
            nights=2,
            budget_remaining=1200,   # 可空：总预算扣除门票后的住宿可用额
        )
    """

    def __init__(
        self,
        city: str,
        hotel_preferences: Optional[Dict[str, Any]] = None,
        data_dir: Path = DEFAULT_GRAPH_DIR,
        hotel_provider: Optional[Callable[[str], List[Hotel]]] = None,
        spot_locations_provider: Optional[Callable[[str], Dict[str, Tuple[float, float]]]] = None,
        travel_time_provider: Optional["TravelTimeProvider"] = None,
    ):
        """酒店选择器。

        ``hotel_provider``：``fn(city) -> List[Hotel]``，缺省 ``load_hotels``
        （假数据）；真实数据接入时由 ``data_transmission.live_data.make_live_hotel_provider`` 注入。
        ``spot_locations_provider``：``fn(city) -> {spot_id: (lat, lng)}``，缺省读
        本地 spots.json；真源候选池时由调用方传入（保持通勤口径一致）。
        ``travel_time_provider``（8.29 酒店通勤真源化）：真源矩阵模式注入
        ``LiveTravelTimeProvider``（酒店↔景点分钟取矩阵真源值）；缺省 None →
        按直线距离折算（假数据口径）。酒店候选坐标须由调用方并入矩阵的
        ``name_to_coord`` 并补 ``set_name_map({hotel_id: hotel_name})``。
        """
        self.city = city
        if hotel_provider is not None:
            # 8.30 酒店真源：真源 provider 抛异常 / 返回空 → 回退假池（不阻断选店）
            try:
                self.hotels = list(hotel_provider(city))
            except Exception:  # noqa: BLE001
                self.hotels = []
            if not self.hotels:
                self.hotels = load_hotels(city, data_dir)
        else:
            self.hotels = load_hotels(city, data_dir)
        self.preferences = dict(hotel_preferences or {})
        if spot_locations_provider is not None:
            self._spot_locations = dict(spot_locations_provider(city))
        else:
            self._spot_locations = _load_spot_locations(city, data_dir)
        self._by_id = {hotel.id: hotel for hotel in self.hotels}
        self._travel_provider = travel_time_provider

    # ------------------------------------------------------------------
    # 通勤
    # ------------------------------------------------------------------

    def travel_minutes(self, hotel_id: str, spot_id: str) -> int:
        hotel = self._by_id.get(str(hotel_id))
        if hotel is None:
            return LARGE_COMMUTE
        # 8.29：真源矩阵模式优先（酒店↔景点通勤真源化）——矩阵真实行驶分钟
        if self._travel_provider is not None:
            try:
                edge = self._travel_provider.get_edge(str(hotel_id), str(spot_id))
            except (ValueError, KeyError, RuntimeError):
                # 未映射节点 / 矩阵缺行超过降级上限：按大惩罚处理（不误导换宿判定）
                return LARGE_COMMUTE
            return int(edge.transport_minutes)
        spot_location = self._spot_locations.get(str(spot_id))
        if spot_location is None:
            return LARGE_COMMUTE
        km = _haversine_km(
            hotel.location[0],
            hotel.location[1],
            spot_location[0],
            spot_location[1],
        )
        return max(MIN_COMMUTE_MINUTES, int(math.ceil(km / CITY_SPEED_KMH * 60)))

    # ------------------------------------------------------------------
    # 硬过滤
    # ------------------------------------------------------------------

    def eligible(
        self,
        budget_per_night: Optional[float] = None,
        exclude_ids: Sequence[str] = (),
    ) -> List[Hotel]:
        """候选酒店：价位段 / 位置偏好 / 最低星级 / 每晚预算上限 / 排除集 过滤。"""
        excluded = {str(hotel_id) for hotel_id in exclude_ids}
        price_level = _normalize_price_level(
            self.preferences.get("price_level")
        )
        location_preferences = [
            str(item).strip()
            for item in (self.preferences.get("location_preferences") or [])
            if str(item).strip()
        ]
        min_star = self.preferences.get("min_star")

        def _price_passes(price: float) -> bool:
            if price_level == "economic":
                return price <= ECONOMIC_MAX_PRICE
            if price_level == "luxury":
                return price >= LUXURY_MIN_PRICE
            if price_level == "comfort":
                return ECONOMIC_MAX_PRICE < price < LUXURY_MIN_PRICE
            return True

        def _location_passes(hotel: Hotel) -> bool:
            if not location_preferences:
                return True
            haystack = " ".join(
                [hotel.name, *hotel.tags]
            ).lower()
            return any(
                preference.lower() in haystack for preference in location_preferences
            )

        def _star_passes(hotel: Hotel) -> bool:
            if min_star is None:
                return True
            try:
                return hotel.star >= int(min_star)
            except (TypeError, ValueError):
                return True

        def _budget_passes(hotel: Hotel) -> bool:
            if budget_per_night is None:
                return True
            return hotel.price_per_night <= budget_per_night

        return [
            hotel
            for hotel in self.hotels
            if hotel.id not in excluded
            and _price_passes(hotel.price_per_night)
            and _location_passes(hotel)
            and _star_passes(hotel)
            and _budget_passes(hotel)
        ]

    # ------------------------------------------------------------------
    # 价格增量（hotel.price_change 事件翻译用）
    # ------------------------------------------------------------------

    def effective_price(
        self, hotel: Hotel, price_deltas: Optional[Dict[str, float]] = None
    ) -> float:
        """每晚价格；price_deltas 提供该酒店的价格增量（元）。"""
        price = hotel.price_per_night
        if price_deltas and hotel.id in price_deltas:
            price = price + float(price_deltas[hotel.id])
        return max(price, 0.0)

    # ------------------------------------------------------------------
    # 常驻酒店评分
    # ------------------------------------------------------------------

    def _commute_sum(self, hotel: Hotel, first_last: Sequence[Tuple[str, str]]) -> int:
        """Σ(酒店→当日首景点 + 当日末景点→酒店)。"""
        return sum(
            self.travel_minutes(hotel.id, first)
            + self.travel_minutes(hotel.id, last)
            for first, last in first_last
        )

    def _cost(self, hotel: Hotel, first_last: Sequence[Tuple[str, str]]) -> float:
        return (
            TRAVEL_WEIGHT * self._commute_sum(hotel, first_last)
            + PRICE_WEIGHT * hotel.price_per_night
            + RATING_WEIGHT * (5.0 - hotel.rating)
        )

    def pick_constant(
        self, first_last: Sequence[Tuple[str, str]]
    ) -> Optional[Hotel]:
        """选常驻酒店 H*：加权代价最小（通勤优先、便宜、高分）。"""
        candidates = self.eligible()
        if not candidates or not first_last:
            return None
        return min(candidates, key=lambda hotel: self._cost(hotel, first_last))

    # ------------------------------------------------------------------
    # 主入口：阈值约束式换宿
    # ------------------------------------------------------------------

    def select(
        self,
        daily_first_last: Sequence[Tuple[str, str]],
        nights: int,
        budget_remaining: Optional[float] = None,
        exclude_ids: Sequence[str] = (),
        price_deltas: Optional[Dict[str, float]] = None,
    ) -> Dict[str, Any]:
        """生成每晚酒店预订（默认常驻，通勤超阈值才换宿）。

        ``daily_first_last``：每天 (首景点, 末景点) 序列，长度 = 游玩天数。
        ``nights``：住宿晚数（= return_date − departure_date；无日期时退化）。
        ``budget_remaining``：总预算扣除门票后的住宿可用额（每人/每趟）；None 不限。
        ``exclude_ids``：排除的酒店 id（如满房），replan 用。
        ``price_deltas``：{hotel_id: 每晚价格增量}（hotel.price_change 事件翻译用）。

        返回 dict：
            constant_hotel      常驻酒店信息或 None
            bookings            每晚一条 {night, hotel_id, hotel_name, price,
                                        change, reason, commute_minutes}
            switched_days       [{day, hotel_id, hotel_name, reason}]
            hotel_cost          Σ每晚价格
            nights              晚数
            warnings            [str]
        """
        warnings: List[str] = []
        first_last = list(daily_first_last)
        if nights <= 0 or not first_last:
            return {
                "constant_hotel": None,
                "bookings": [],
                "switched_days": [],
                "hotel_cost": 0.0,
                "nights": nights,
                "warnings": warnings,
            }

        budget_per_night = None
        if budget_remaining is not None and budget_remaining >= 0:
            budget_per_night = budget_remaining / nights

        candidates = self.eligible(
            budget_per_night=budget_per_night, exclude_ids=exclude_ids
        )
        if not candidates and budget_per_night is not None:
            warnings.append(
                f"每晚预算上限 {budget_per_night:.0f} 元内无酒店，放宽预算后重选"
            )
            candidates = self.eligible(
                budget_per_night=None, exclude_ids=exclude_ids
            )
        if not candidates:
            warnings.append("无符合条件的酒店（数据缺失或过滤过严）")
            return {
                "constant_hotel": None,
                "bookings": [],
                "switched_days": [],
                "hotel_cost": 0.0,
                "nights": nights,
                "warnings": warnings,
            }

        def cost(hotel: Hotel) -> float:
            return (
                TRAVEL_WEIGHT * self._commute_sum(hotel, first_last)
                + PRICE_WEIGHT * self.effective_price(hotel, price_deltas)
                + RATING_WEIGHT * (5.0 - hotel.rating)
            )

        constant = min(candidates, key=cost)

        # 逐日换宿判定：常驻酒店 → 当日首景点 通勤 ≥ 阈值才换
        switched_days: List[Dict[str, Any]] = []
        for day_index, (first_spot, _) in enumerate(first_last, start=1):
            if day_index > nights:
                break  # 没有对应的夜晚，换宿无承载
            commute = self.travel_minutes(constant.id, first_spot)
            if commute < MAX_SINGLE_HOTEL_COMMUTE:
                continue
            # 换宿目标：通勤显著缩短（≤ 半阈值）的酒店，选最近的一家
            switch_candidates = [
                hotel
                for hotel in candidates
                if self.travel_minutes(hotel.id, first_spot)
                <= SWITCH_TARGET_MAX
            ]
            if not switch_candidates:
                warnings.append(
                    f"第{day_index}天常驻酒店到首景点需 {commute} 分钟 ≥ "
                    f"{MAX_SINGLE_HOTEL_COMMUTE}，但没有足够近的酒店"
                    f"（≤ {SWITCH_TARGET_MAX} 分钟），维持常驻"
                )
                continue
            switch_hotel = min(
                switch_candidates,
                key=lambda hotel: self.travel_minutes(hotel.id, first_spot),
            )
            switched_days.append(
                {
                    "day": day_index,
                    "hotel_id": switch_hotel.id,
                    "hotel_name": switch_hotel.name,
                    "lat": float(switch_hotel.location[0]),
                    "lng": float(switch_hotel.location[1]),
                    "commute_minutes": self.travel_minutes(
                        switch_hotel.id, first_spot
                    ),
                    "reason": (
                        f"第{day_index}天常驻酒店到首景点需 {commute} 分钟，"
                        f"≥ {MAX_SINGLE_HOTEL_COMMUTE} 分钟，换至更近的"
                        f"{switch_hotel.name}"
                    ),
                }
            )

        switch_by_day = {item["day"]: item for item in switched_days}

        # 每晚酒店：当晚对应天换宿 → 换宿酒店；否则常驻
        bookings: List[Dict[str, Any]] = []
        previous_hotel_id: str = constant.id
        for night in range(1, nights + 1):
            if night <= len(first_last) and night in switch_by_day:
                hotel = self._by_id[switch_by_day[night]["hotel_id"]]
                reason = switch_by_day[night]["reason"]
            else:
                hotel = constant
                reason = None
            change = hotel.id != previous_hotel_id
            bookings.append(
                {
                    "night": night,
                    "hotel_id": hotel.id,
                    "hotel_name": hotel.name,
                    # A4 修复（8.30）：酒店真实坐标透传（Hotel.location 为
                    # (lat, lng) tuple），C 端地图可标注酒店（此前 hotel 段坐标恒 0）。
                    "lat": float(hotel.location[0]),
                    "lng": float(hotel.location[1]),
                    "price": self.effective_price(hotel, price_deltas),
                    "change": change,
                    "reason": reason,
                    "commute_minutes": self.travel_minutes(
                        hotel.id, first_last[min(night, len(first_last)) - 1][0]
                    ),
                }
            )
            previous_hotel_id = hotel.id

        hotel_cost = sum(booking["price"] for booking in bookings)
        return {
            "constant_hotel": {
                "hotel_id": constant.id,
                "hotel_name": constant.name,
                "lat": float(constant.location[0]),
                "lng": float(constant.location[1]),
                "price": self.effective_price(constant, price_deltas),
            },
            "bookings": bookings,
            "switched_days": switched_days,
            "hotel_cost": hotel_cost,
            "nights": nights,
            "warnings": warnings,
        }


# ---------------------------------------------------------------------------
# 计划级入口（main.py / demo 用）
# ---------------------------------------------------------------------------


def compute_nights(requirement: Dict[str, Any]) -> int:
    """住宿晚数：有出行时段按 (return_date − departure_date) 的整天数；
    无日期时退化为 max(days − 1, 0)。"""
    content = requirement.get("content", {})
    schedule = content.get("travel_schedule") or {}

    def _parse_iso(text: object) -> Optional[date]:
        try:
            return date.fromisoformat(str(text))
        except (TypeError, ValueError):
            return None

    departure = _parse_iso(schedule.get("departure_date"))
    return_date = _parse_iso(schedule.get("return_date"))
    if departure is not None and return_date is not None:
        return max((return_date - departure).days, 0)
    days = content.get("days")
    try:
        return max(int(days) - 1, 0)
    except (TypeError, ValueError):
        return 0


def _day_first_last_spots(plan: Dict[str, Any]) -> List[Tuple[str, str]]:
    """从执行计划提取每天（首景点, 末景点）的 spot_id；无景点天跳过。"""
    days = plan.get("days")
    if not days:
        days = [{"route_details": plan.get("route_details", [])}]
    first_last: List[Tuple[str, str]] = []
    for day in days:
        spots = [
            node.get("details", {}).get("spot_id")
            for node in day.get("route_details", [])
            if node.get("type") == "spot"
            and node.get("details", {}).get("spot_id")
        ]
        if len(spots) >= 1:
            first_last.append((spots[0], spots[-1]))
    return first_last


def select_hotels_for_plan(
    requirement: Dict[str, Any],
    plan: Dict[str, Any],
    graph_dir: Path = DEFAULT_GRAPH_DIR,
    exclude_ids: Sequence[str] = (),
    price_deltas: Optional[Dict[str, float]] = None,
    hotel_provider: Optional[Callable[[str], List[Hotel]]] = None,
    spot_locations_provider: Optional[Callable[[str], Dict[str, Tuple[float, float]]]] = None,
    travel_time_provider: Optional["TravelTimeProvider"] = None,
) -> Optional[Dict[str, Any]]:
    """从执行计划生成住宿安排（衔接 main.py 的编排入口）。

    - 晚数：``compute_nights``（日期差，退化 days−1）
    - 预算贯通：住宿可用额 = 总预算 − 门票费用（每人）；不足时酒店层放宽并提示
    - 通勤校验：每天往返酒店的新增通勤，超当日额度时记入 warnings（不重排，
      联合优化的「带酒店通勤重校验」以提示形式落地）
    - ``exclude_ids`` / ``price_deltas``：重规划时酒店事件（满房 / 价格变化）的翻译

    返回 HotelSelector.select 的结果并附 plan 元信息；无目的地/无景点/无酒店数据
    返回 None。
    """
    content = requirement.get("content", {})
    destination = content.get("destination")
    if not destination:
        return None
    first_last = _day_first_last_spots(plan)
    if not first_last:
        return None

    nights = compute_nights(requirement)
    try:
        budget_limit = content["constraints"]["budget"]
        if isinstance(budget_limit, bool) or budget_limit is None:
            budget_limit = None
        else:
            budget_limit = float(budget_limit)
    except (KeyError, TypeError, ValueError):
        budget_limit = None
    # 8.30 预算口径：住宿可用额 = 总预算 −（门票 + 讲解）。讲解费随排程输出
    # （plan.estimated_guide_cost，spot detail guide_price 汇总 × 人数；
    # 骨架计划缺省按 0——无景点则住宿无从谈起，此处仅作口径统一）。
    ticket_cost = float(plan.get("estimated_ticket_cost") or 0.0)
    guide_cost = float(plan.get("estimated_guide_cost") or 0.0)
    budget_remaining = (
        budget_limit - (ticket_cost + guide_cost)
        if budget_limit is not None
        else None
    )

    selector = HotelSelector(
        destination,
        hotel_preferences=content.get("hotel_preferences"),
        data_dir=graph_dir,
        hotel_provider=hotel_provider,
        spot_locations_provider=spot_locations_provider,
        travel_time_provider=travel_time_provider,   # 8.29：真源矩阵 → 酒店通勤真源化
    )
    result = selector.select(
        first_last,
        nights,
        budget_remaining=budget_remaining,
        exclude_ids=exclude_ids,
        price_deltas=price_deltas,
    )
    if not result["bookings"]:
        return None

    daily_limit = int(content.get("constraints", {}).get("daily_travel_time") or 0)
    # 当日酒店通勤按「当晚酒店」计入：第 d 天用第 d 晚的酒店（超出晚数的天沿用最后一晚）
    added_commutes: List[int] = []
    for offset, (first, last) in enumerate(first_last):
        night_index = (
            min(offset, len(result["bookings"]) - 1) if result["bookings"] else offset
        )
        hotel_id = result["bookings"][night_index]["hotel_id"]
        commute = (
            selector.travel_minutes(hotel_id, first)
            + selector.travel_minutes(hotel_id, last)
        )
        added_commutes.append(commute)
        if daily_limit > 0:
            day_plan = (plan.get("days") or [{}])[offset]
            busy = float(day_plan.get("total_counted_minutes") or 0.0)
            if busy + commute > daily_limit:
                result["warnings"].append(
                    f"第{offset + 1}天计入酒店往返通勤 {commute} 分钟后超当日额度"
                    f"（超 {int(busy + commute - daily_limit)} 分钟）——"
                    "酒店选择以通勤为软偏好，如需严格纳入排程属后续联合优化"
                )

    result["daily_first_last"] = first_last
    result["daily_hotel_commutes"] = added_commutes
    return result