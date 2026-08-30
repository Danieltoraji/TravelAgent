"""餐厅选择器：给一顿饭（锚定在某个景点）挑一家餐厅。

规则：
- 用户有饮食偏好（food_preferences）时，优先匹配菜系/招牌，再按交通时间就近；
- 没有饮食偏好时，直接选距离锚定景点最近的餐厅。
- 跨顿去重（8.31 P0）：同一条时间轴内已排定的餐厅默认排除（``exclude_ids``，
  由时间轴构建方逐顿累积传入）；**之前各天最终选定的餐厅**也排除（``note_planned``
  记录，多日循环每天完成后调用、每个种子开始时重置——规划器会拿同一 resolver
  反复试算候选路线，只有"每天最终结果"能进排除集，试算选的不算，避免污染）。
  排除后池空 → 松一档：先允许跨天重复（同天内仍去重），再不够允许同天重复。
- 附近搜索模式（``nearby_pool`` 注入时）：候选即锚点附近餐厅（真源），通勤矩阵
  缺边时按 haversine 直线距离估算兜底——附近餐厅常不在景点矩阵里，缺边是常态
  而非异常，不能因缺边降级回假源。
- 聚类共享查询（8.30）：相邻锚点（<1km）聚簇，一簇只发一次附近查询（簇质心，
  radius 覆盖簇半径+搜索半径），结果按各锚点实际坐标本地 haversine 重排——
  北京 20 锚点实测聚成 ~8 簇，高德请求数与 QPS 压力砍 60%。
"""

from __future__ import annotations

import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

try:
    from data_transmission.city_graph import DEFAULT_GRAPH_DIR
    from data_transmission.restaurant import Restaurant, load_restaurants
except ModuleNotFoundError:
    from ..data_transmission.city_graph import DEFAULT_GRAPH_DIR
    from ..data_transmission.restaurant import Restaurant, load_restaurants

from .providers import JsonTravelTimeProvider, TravelEdge, TravelTimeProvider

# haversine 兜底：直线 km → 驾车分钟的估算系数（路网绕行 ×1.4、市区均速
# ~25km/h ≈ 2.4 min/km，合并取 3.4）。仅矩阵缺边时用，矩阵命中不参与。
_ESTIMATED_MINUTES_PER_KM = 3.4


class RestaurantResolver:
    """为路线时间轴挑选餐厅，并提供餐厅与景点之间的交通边。"""

    def __init__(
        self,
        city: str,
        food_preferences: Sequence[str] = (),
        travel_time_provider: Optional[TravelTimeProvider] = None,
        data_dir: Path = DEFAULT_GRAPH_DIR,
        restaurant_provider: Optional[Callable[[str], List[Restaurant]]] = None,
        nearby_pool: Optional[Callable[[Tuple[float, float], int], List[Restaurant]]] = None,
    ):
        """餐厅选择器。

        ``travel_time_provider``：缺省 ``JsonTravelTimeProvider``（spots_graph 假边）；
        真源模式下传入 ``LiveTravelTimeProvider``（batch_route 矩阵）→ 餐厅↔景点通勤
        由矩阵提供（8.28：真源餐厅坐标须并入矩阵的 ``name_to_coord``）。
        ``restaurant_provider``：``fn(city) -> List[Restaurant]``，缺省 ``load_restaurants``
        （假池）；真实数据接入时由 ``data_transmission.live_data.make_live_restaurants_provider`` 注入。
        ``nearby_pool``：``fn(anchor_coord, k) -> List[Restaurant]``，锚点附近候选
        （8.31 P0 真源附近搜索）；提供时 select 优先在附近候选里挑，池空/异常时
        降级用全池（restaurant_provider / 假池）。
        """
        self.city = city
        if restaurant_provider is not None:
            self.restaurants = list(restaurant_provider(city))
        else:
            self.restaurants = load_restaurants(city, data_dir)
        self.food_preferences = [
            str(item).strip() for item in food_preferences if str(item).strip()
        ]
        self.provider = travel_time_provider or JsonTravelTimeProvider(city, data_dir)
        self._by_id = {restaurant.id: restaurant for restaurant in self.restaurants}
        self._nearby_pool = nearby_pool
        # 锚点 id → 坐标 (lat, lng)：附近搜索与 haversine 兜底都要用锚点坐标，
        # 由接线方（BPlannerHook）在拿到候选景点后 ``set_anchor_locations`` 注入。
        # 键既可能是景点名（live 模式 name_to_coord 的键），也可能是假池景点 id
        # （BJ_001 等，假池 name==id 语义下天然覆盖）。
        self._anchor_locations: Dict[str, Tuple[float, float]] = {}
        # 附近候选缓存：anchor_id → 候选列表（同一锚点多顿复用，免重复调 food 工具）。
        self._nearby_cache: Dict[str, List[Restaurant]] = {}
        # 锚点 id → 景点名（live 模式 scenic_N → 景点名；假池 name==id 不需要）。
        self._anchor_names: Dict[str, str] = {}
        # 附近候选（含全池之外的新餐厅）的 id → Restaurant 视图（is_restaurant /
        # name_of / travel_edge 要认得出附近餐厅）。
        self._extra_by_id: Dict[str, Restaurant] = {}
        # 之前各天**最终**选定的餐厅 id（跨天去重；多日循环每天完成后 note_planned，
        # 每个多日种子开始时 reset_planned，保证与试算顺序无关）。
        self._prior_planned_ids: List[str] = []

    def is_restaurant(self, node_id: str) -> bool:
        return str(node_id) in self._by_id or str(node_id) in self._extra_by_id

    def name_of(self, node_id: str) -> str:
        restaurant = self._by_id.get(str(node_id)) or self._extra_by_id.get(
            str(node_id)
        )
        if restaurant is not None:
            return restaurant.name
        # live 模式锚点是 scenic id（scenic_N）：本身不是餐厅，回退原名——
        # name_to_coord / anchor_locations 键为景点名，坐标查询走
        # provider 的 id→name 映射（LiveTravelTimeProvider._display_name）。
        return str(node_id)

    def set_anchor_locations(self, locations: Dict[str, Tuple[float, float]]) -> None:
        """注入锚点（景点）坐标映射，供附近搜索与缺边估算使用。

        键既可是景点名（live 模式 name_to_coord 的键）也可是假池景点 id
        （BJ_001 等）；live 模式的 scenic id（scenic_N）需配合 ``set_anchor_names``
        的 id→name 映射才能查到坐标。
        """
        self._anchor_locations = {
            str(node_id): (float(lat), float(lng))
            for node_id, (lat, lng) in (locations or {}).items()
        }
        self._nearby_cache.clear()

    def set_anchor_names(self, names: Dict[str, str]) -> None:
        """注入锚点 id → 景点名映射（live 模式 scenic_N → 景点名）。"""
        self._anchor_names = {str(k): str(v) for k, v in (names or {}).items()}

    def reset_planned(self) -> None:
        """清空「之前各天最终选定」记录（每个多日规划种子开始时调用）。"""
        self._prior_planned_ids = []

    def note_planned(self, restaurant_ids: Sequence[str]) -> None:
        """记录某天**最终**选定的餐厅 id（跨天去重用；试算中的选择不记录）。"""
        for restaurant_id in restaurant_ids:
            restaurant_id = str(restaurant_id)
            if restaurant_id and restaurant_id not in self._prior_planned_ids:
                self._prior_planned_ids.append(restaurant_id)

    def travel_edge(self, origin_id: str, destination_id: str) -> Optional[TravelEdge]:
        origin_id, destination_id = str(origin_id), str(destination_id)
        if origin_id == destination_id:
            return None
        try:
            return self.provider.get_edge(origin_id, destination_id)
        except (ValueError, KeyError):
            return None

    def travel_minutes(self, origin_id: str, destination_id: str) -> int:
        edge = self.travel_edge(origin_id, destination_id)
        if edge is not None:
            return edge.transport_minutes
        return self._estimate_minutes(origin_id, destination_id)

    def select(
        self, anchor_spot_id: str, exclude_ids: Sequence[str] = ()
    ) -> Optional[Restaurant]:
        """为锚定在 ``anchor_spot_id`` 的一顿饭选餐厅。

        ``exclude_ids``：本条时间轴内已排定的餐厅 id（同天跨顿去重，由时间轴
        构建方逐顿累积传入——resolver 会被规划器反复试算，去重状态不能放实例上）。
        排除集 = exclude_ids ∪ 之前各天最终选定；排除后池空按「松一档」降级：
        先放跨天重复（同天仍去重），再放同天重复（附近候选/全池耗尽的兜底）。
        """
        candidates = self._candidates_for(anchor_spot_id)
        if not candidates:
            return None
        anchor = str(anchor_spot_id)
        same_day = {str(item) for item in exclude_ids}
        prior_days = set(self._prior_planned_ids)

        def rank(restaurant: Restaurant) -> Tuple:
            minutes = self.travel_minutes(anchor, restaurant.id)
            if self.food_preferences:
                score = self._food_score(restaurant)
                return (-score, minutes, restaurant.id)
            return (minutes, restaurant.id)

        fresh = [
            r for r in candidates if r.id not in same_day and r.id not in prior_days
        ]
        if fresh:
            return min(fresh, key=rank)
        same_day_fresh = [r for r in candidates if r.id not in same_day]
        if same_day_fresh:
            # 附近候选/全池撑不满「整程不重复」：允许跨天回头（隔天再吃观感可接受）。
            return min(same_day_fresh, key=rank)
        return min(candidates, key=rank)

    # ------------------------------------------------------------------
    # 附近搜索（8.31 P0，真源模式）
    # ------------------------------------------------------------------

    def nearby(self, anchor_id: str, k: int = 10) -> List[Restaurant]:
        """锚点附近候选；锚点坐标缺失 / 工具失败 → 空列表（调用方降级全池）。

        ``anchor_id`` 可为景点名或景点 id（scenic_N / BJ_001），统一经
        ``_anchor_key`` 归一后查坐标；返回的附近餐厅（含全池外新店）注册进
        ``_extra_by_id``，让 is_restaurant / travel_edge 认得出。
        """
        if self._nearby_pool is None:
            return []
        key = self._anchor_key(anchor_id)
        if key in self._nearby_cache:
            return self._nearby_cache[key]
        anchor_coord = self._anchor_locations.get(key)
        restaurants: List[Restaurant] = []
        if anchor_coord is not None:
            try:
                restaurants = list(self._nearby_pool(anchor_coord, k) or [])
            except Exception:  # noqa: BLE001
                restaurants = []
        for restaurant in restaurants:
            self._extra_by_id.setdefault(restaurant.id, restaurant)
        self._nearby_cache[key] = restaurants
        return restaurants

    def _anchor_key(self, anchor_id: str) -> str:
        """锚点 id 归一：scenic id → 景点名（set_anchor_names / name_of 链）。"""
        anchor_id = str(anchor_id)
        if anchor_id in self._anchor_names:
            return self._anchor_names[anchor_id]
        if anchor_id in self._anchor_locations:
            return anchor_id
        resolved = self.name_of(anchor_id)
        return resolved if resolved != anchor_id else anchor_id

    # ------------------------------------------------------------------
    # 聚类共享查询（8.30：锚点聚簇，一簇一次高德请求）
    # ------------------------------------------------------------------

    # 聚簇距离阈值（km）：彼此 < 1km 的锚点共享同一片附近餐厅，一簇一次查询。
    CLUSTER_RADIUS_KM = 1.0
    # 簇间查询间隔（秒）：免费 key QPS≈2-3/s，多簇连发仍可能撞 10021 限流
    # （与 hook 时代的锚点间隔同源，随聚类挪到此处）。
    CLUSTER_QUERY_INTERVAL = 0.4

    def nearby_clustered(
        self, anchor_ids: Sequence[str], k: int = 10
    ) -> Tuple[Dict[str, List[Restaurant]], List[Dict[str, Any]]]:
        """聚类共享的批量附近查询。

        相邻锚点（< ``CLUSTER_RADIUS_KM``）贪心聚簇 → 每簇以**质心**发一次
        ``nearby_pool``（radius 覆盖「簇半径 + 搜索半径」，默认搜索半径按
        nearby_pool 的 2km 口径）→ 簇内每个锚点拿簇结果按**自身坐标**
        haversine 重排，取前 ``k``。

        返回 ``(anchor_results, clusters)``：
        - ``anchor_results``：``{anchor_key: 按距离排好的 top-k 候选}``；
        - ``clusters``：``[{"members": [anchor_key...], "restaurants": [簇查询
          全量候选（25 家，未截断）]}]``——供上层按簇拆矩阵（簇内锚点 ×
          簇归属餐厅的小矩阵，消灭跨簇死对）。

        收益（实测北京 20 锚点 → ~8 簇）：高德请求数砍 60%，QPS 压力与
        节流等待同步下降；簇内锚点本就共享同一片餐厅，结果近乎无损。
        单簇失败不影响其它簇；簇查询失败 → 该簇锚点拿空列表（上层全池兜底）。
        """
        if self._nearby_pool is None:
            return {}, []

        # 锚点 key → 坐标（缺坐标的锚点不参与聚类，单独不查——上层兜底）。
        keyed: Dict[str, Tuple[float, float]] = {}
        for anchor_id in anchor_ids:
            key = self._anchor_key(anchor_id)
            coord = self._anchor_locations.get(key)
            if coord is not None:
                keyed[key] = coord

        # 贪心聚簇：与簇内任一锚点距离 < 阈值即并入（简单/无需预设簇数）。
        cluster_keys: List[List[str]] = []
        for key, coord in keyed.items():
            for cluster in cluster_keys:
                if any(
                    _haversine_km(coord, keyed[member]) < self.CLUSTER_RADIUS_KM
                    for member in cluster
                ):
                    cluster.append(key)
                    break
            else:
                cluster_keys.append([key])

        # 每簇一次查询（质心 + 扩大 radius 覆盖簇内全部锚点的 2km 圈），簇间节流。
        anchor_results: Dict[str, List[Restaurant]] = {}
        clusters: List[Dict[str, Any]] = []
        for i, cluster in enumerate(cluster_keys):
            if i:
                time.sleep(self.CLUSTER_QUERY_INTERVAL)
            members = {key: keyed[key] for key in cluster}
            center = (
                sum(c[0] for c in members.values()) / len(members),
                sum(c[1] for c in members.values()) / len(members),
            )
            max_member_offset = max(
                _haversine_km(center, c) for c in members.values()
            )
            # 簇半径（km→m）+ 2km 基础搜索半径；nearby_pool 的 radius 单位是米。
            radius_m = int((max_member_offset + 2.0) * 1000)
            try:
                # pool 签名 nearby_pool(anchor_coord, count)：count 位置传参
                # （k=25 关键字会 TypeError 被吞成空簇——8.30 排查教训）。
                candidates = list(self._nearby_pool(center, 25) or [])
            except Exception:  # noqa: BLE001
                candidates = []
            for restaurant in candidates:
                self._extra_by_id.setdefault(restaurant.id, restaurant)
            for key, coord in members.items():
                ranked = sorted(
                    candidates, key=lambda r: _haversine_km(coord, r.location)
                )[:k]
                anchor_results[key] = ranked
                self._nearby_cache[key] = ranked
            clusters.append({"members": list(members), "restaurants": candidates})
        return anchor_results, clusters

    def _candidates_for(self, anchor_spot_id: str) -> List[Restaurant]:
        """select 的候选集：附近模式取锚点附近候选，失败/空 → 全池兜底。"""
        if self._nearby_pool is not None:
            nearby = self.nearby(anchor_spot_id)
            if nearby:
                return nearby
        return list(self.restaurants)

    def _estimate_minutes(self, origin_id: str, destination_id: str) -> int:
        """矩阵缺边时的 haversine 兜底（附近餐厅不在景点矩阵里是常态）。"""
        origin = self._location_of(origin_id)
        destination = self._location_of(destination_id)
        if origin is None or destination is None:
            return 0
        return int(round(_haversine_km(origin, destination) * _ESTIMATED_MINUTES_PER_KM))

    def _location_of(self, node_id: str) -> Optional[Tuple[float, float]]:
        restaurant = self._by_id.get(str(node_id)) or self._extra_by_id.get(
            str(node_id)
        )
        if restaurant is not None:
            return restaurant.location
        # 景点节点：id 归一（scenic_N → 名）后查锚点坐标。
        return self._anchor_locations.get(self._anchor_key(node_id))

    def _food_score(self, restaurant: Restaurant) -> int:
        tags = [tag.lower() for tag in (*restaurant.cuisine_tags, *restaurant.signature_tags)]
        preferences = [preference.lower() for preference in self.food_preferences]
        return sum(
            1
            for preference in preferences
            for tag in tags
            if preference in tag or tag in preference
        )


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两坐标 (lat, lng) 的球面直线距离（km）。"""
    lat1, lng1 = a
    lat2, lng2 = b
    radius = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_lat = math.radians(lat2 - lat1)
    d_lng = math.radians(lng2 - lng1)
    h = (
        math.sin(d_lat / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(d_lng / 2) ** 2
    )
    return 2 * radius * math.asin(math.sqrt(h))
