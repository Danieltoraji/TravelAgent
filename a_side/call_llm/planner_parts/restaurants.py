"""P4 拆分：真源餐厅编排器（RestaurantOrchestrator）。

自 ``b_planner_hook.BPlannerHook`` 拆出（BPlannerHook 拆分后只剩编排）：
- 阶段 2 带餐厅重排（原 ``_live_plan_with_restaurants``，8.30 两阶段矩阵 +
  归属制连边 + 按簇拆矩阵）
- 真源餐厅解析器构建（原 ``_build_live_restaurants``）
- 餐厅/锚点相关纯函数（原模块级 ``_collect_plan_spot_names`` /
  ``_collect_meal_anchors`` / ``_haversine_km`` / ``_coord_tuple``，测试直接
  import，经 ``b_planner_hook`` re-export 保持命名空间兼容）

**类身份约定**：``RestaurantOrchestrator`` 是 mixin，方法签名与原 BPlannerHook
私有方法完全一致，经继承保留在 ``BPlannerHook`` 实例上（测试零改动）。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import logging

logger = logging.getLogger("call_llm.planner_parts.restaurants")


# 每个用餐锚点进真源矩阵的附近餐厅数（8.30：3→5，top1 进时间轴 + 去重换选
# 余量 + 为 §五 TOP10 推荐预留）。
_NEARBY_MATRIX_K = 5
# 按簇拆矩阵的簇间间隔（秒）：免费 key QPS≈2-3/s（与聚类查询节流同节奏）。
_CLUSTER_MATRIX_INTERVAL = 0.4
# 簇间查询间隔已随聚类挪到 RestaurantResolver.CLUSTER_QUERY_INTERVAL。
# haversine 估算系数（直线 km → 驾车分钟；与 transport/restaurants.py 同口径，
# 圈层外餐厅边/矩阵失败兜底用——真源边只给归属圈层）。
_ESTIMATED_MINUTES_PER_KM = 3.4


def _collect_plan_spot_names(plan: Dict[str, Any]) -> List[str]:
    """计划内实际排入的景点名（``route_details`` 里 ``type=="spot"`` 节点，去重保序）。

    8.30 矩阵瘦身：阶段 2 增量矩阵的起点只用计划内景点（含用餐锚点），
    不再把全部候选景点 × 餐厅都算一遍。
    """
    names: List[str] = []
    seen = set()
    for day in plan.get("days", []):
        for node in day.get("route_details", []) or []:
            if node.get("type") != "spot":
                continue
            name = node.get("name")
            if name and name not in seen:
                seen.add(name)
                names.append(name)
    return names


def _collect_meal_anchors(plan: Dict[str, Any]) -> List[str]:
    """用餐锚点：每顿已安排 meal 前最近一个景点的名称（去重保序）。

    阶段 1 计划（无餐厅）的 meal 段是抽象的；锚点即「用餐窗口临近的景点」，
    阶段 2 的餐厅矩阵只与这些景点相关。
    """
    anchors: List[str] = []
    seen = set()
    for day in plan.get("days", []):
        last_spot: Optional[str] = None
        for node in day.get("route_details", []) or []:
            if node.get("type") == "spot":
                last_spot = node.get("name")
            elif node.get("type") == "meal" and last_spot and last_spot not in seen:
                seen.add(last_spot)
                anchors.append(last_spot)
    return anchors


def _haversine_km(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """两坐标 (lat, lng) 的球面直线距离（km）。"""
    import math

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


def _coord_tuple(coord: Optional[str]) -> Optional[Tuple[float, float]]:
    """坐标串 ``"lng,lat"`` → ``(lat, lng)``；非法 → None。"""
    if not coord:
        return None
    parts = str(coord).split(",")
    if len(parts) != 2:
        return None
    try:
        lng, lat = float(parts[0]), float(parts[1])
    except ValueError:
        return None
    return (lat, lng)


class RestaurantOrchestrator:
    """真源餐厅编排（mixin）。

    依赖宿主实例属性：``_tool_provider`` / ``_travel_time_provider`` / ``city`` /
    ``requirement`` / ``_live_spots_source`` / ``_planner``（宿主自身）。
    """

    def _live_plan_with_restaurants(
        self,
        plan1: Dict[str, Any],
        spots: Any,
        name_to_coord: Dict[str, str],
        base_matrix: Dict[Tuple[str, str], Tuple[float, int]],
        live_hotels: Sequence[Any],
        first_day_start_time: Optional[str] = None,
        last_day_end_minutes: Optional[int] = None,
    ) -> Dict[str, Any]:
        """8.30 阶段 2：对「候选景点 × 真源餐厅」补增量矩阵后带餐厅重排。

        - 锚点（用餐窗口临近的景点）来自阶段 1 无餐厅计划，仅用于判断是否需要
          真源餐厅（有已安排用餐才拉 food 工具）；
        - 增量矩阵起点 = **全部候选景点**（plan2 带餐厅重排可能换候选池内的
          景点，全量覆盖避免「新景点 ↔ 餐厅」缺行——线上曾因此降级超限回退假源，
          8.30 复验教训）；终点 = 真源餐厅；正交子矩阵仍远小于原全集合 n²；
        - 餐厅 / 增量矩阵 / 重排任何失败 → 直接用阶段 1 计划（无餐厅段），
          不整链回退假源（scenic / 酒店真源不受影响）。
        """
        if not _collect_meal_anchors(plan1):
            return plan1  # 无已安排的用餐 → 不需要真源餐厅
        resolver = self._build_live_restaurants(name_to_coord)
        if resolver is None:
            return plan1
        from data_transmission.live_data import _coord_str, make_live_matrix_fn

        # 8.30 聚类共享 + 按簇拆矩阵（方案一）：相邻锚点（<1km）一簇一次附近
        # 查询（北京 20 锚点 → ~8 簇，请求数砍 60%）；真源矩阵也随之按簇拆——
        # 每簇只查「簇内锚点 × 簇归属餐厅」的小矩阵（簇查询返回的全量候选），
        # 彻底消灭跨簇死对（天坛锚点 × 景山餐厅这类物理上选不到的组合）。
        # 单次大矩阵 20×56=1120 对 → ~8 簇 × 20~30 对 ≈ 200 对（-82%）。
        # 跨簇对/圈层外餐厅按 haversine 估算边预填（P0 教训：缺边累计降级会
        # 整链回退假源）。K=5（top1 进时间轴 + 去重换选 + TOP10 推荐余量）。
        hotel_names = {hotel.name for hotel in live_hotels}
        candidate_anchor_names = [
            name
            for name in name_to_coord
            if name not in hotel_names
        ]
        # 聚类批量查询（簇间 QPS 间隔在 nearby_clustered 内部控制）。resolver
        # 缓存按锚点 key 写入，后续 select() 命中缓存不再发起请求。
        nearby_by_anchor, clusters = resolver.nearby_clustered(
            candidate_anchor_names, k=_NEARBY_MATRIX_K
        )
        nearby_by_anchor = nearby_by_anchor or {}
        # 缺失锚点（坐标缺失/簇失败）补空，保持键完整（上层全池兜底）。
        for name in candidate_anchor_names:
            nearby_by_anchor.setdefault(name, [])

        # 全部已知餐厅（全池 ∪ 各簇候选）的坐标注册：矩阵名单内用真源边，
        # 跨簇/圈层外用估算边——两者都要能被 provider 查到坐标。
        all_restaurants = {
            restaurant.id: restaurant for restaurant in resolver.restaurants
        }
        for cluster in clusters:
            for restaurant in cluster.get("restaurants", []):
                all_restaurants.setdefault(restaurant.id, restaurant)

        # 按簇拆矩阵：每簇一次「簇内锚点 × 簇归属餐厅」的 batch_route 小矩阵。
        # destinations 收窄为**簇内各锚点 top-K 的并集**（非簇查询全量 25 家）——
        # B 侧 get_distances 按终点循环（每终点一次 /v3/distance + 0.3s 间隔），
        # 25 家全量 = 25 次请求 ≈ 8.5s 恒定耗时（8.30 复探实测：对数减了终点
        # 没减）；top-K 并集（~10-15 家）把每簇耗时砍半。圈层外候选仍走估算边。
        # 单簇矩阵失败 → 该簇走估算边，不影响其它簇。
        matrix_fn = make_live_matrix_fn(self._tool_provider, city=self.city)
        matrix2: Dict[Tuple[str, str], Tuple[float, int]] = {}
        for i, cluster in enumerate(clusters):
            members = [m for m in cluster.get("members", []) if m in name_to_coord]
            # 簇归属餐厅 = 簇内各锚点 top-K 候选的并集（去重保序）。
            cluster_restaurant_ids: List[str] = []
            seen_ids = set()
            for member in members:
                for restaurant in nearby_by_anchor.get(member, []):
                    if restaurant.id not in seen_ids:
                        seen_ids.add(restaurant.id)
                        cluster_restaurant_ids.append(restaurant.id)
            cluster_restaurants = [
                all_restaurants[rid]
                for rid in cluster_restaurant_ids
                if rid in all_restaurants and all_restaurants[rid].location
            ]
            if not members or not cluster_restaurants:
                continue
            cluster_rest_coords = {}
            for restaurant in cluster_restaurants:
                coord = _coord_str(
                    {"lat": restaurant.location[0], "lng": restaurant.location[1]}
                )
                if coord and restaurant.name not in cluster_rest_coords:
                    cluster_rest_coords[restaurant.name] = coord
            if not cluster_rest_coords:
                continue
            if i:
                time.sleep(_CLUSTER_MATRIX_INTERVAL)
            try:
                sub_matrix = matrix_fn(
                    {**name_to_coord, **cluster_rest_coords},
                    origins=members,
                    destinations=list(cluster_rest_coords),
                )
            except Exception as exc:  # noqa: BLE001  单簇失败走估算边，不阻断
                logger.warning("簇矩阵失败（members=%s），该簇走估算边：%s", members, exc)
                continue
            matrix2.update(sub_matrix)

        # 合并两阶段矩阵 + **估算边兜底**：真源矩阵没覆盖到的锚点→餐厅对
        # （跨簇对/圈层外/簇失败），按 haversine 预填——规划器查任何餐厅边
        # 都有值，不触发 provider 缺边降级计数（P0 教训）。
        merged = dict(base_matrix)
        for (origin_coord, dest_coord), value in matrix2.items():
            merged[(origin_coord, dest_coord)] = value
            merged[(dest_coord, origin_coord)] = value
        merged_n2c = dict(name_to_coord)
        for restaurant in all_restaurants.values():
            coord = _coord_str(
                {"lat": restaurant.location[0], "lng": restaurant.location[1]}
            )
            if coord and restaurant.name not in merged_n2c:
                merged_n2c[restaurant.name] = coord
        for anchor_name in candidate_anchor_names:
            anchor_coord = merged_n2c.get(anchor_name)
            if not anchor_coord:
                continue
            anchor_location = _coord_tuple(anchor_coord)
            if anchor_location is None:
                continue
            for restaurant in all_restaurants.values():
                coord = merged_n2c.get(restaurant.name)
                if not coord:
                    continue
                key = (anchor_coord, coord)
                if key in merged:
                    continue
                km = _haversine_km(anchor_location, restaurant.location)
                minutes = int(round(km * _ESTIMATED_MINUTES_PER_KM))
                merged[key] = (round(km, 2), minutes)
                merged[(coord, anchor_coord)] = (round(km, 2), minutes)
        self._travel_time_provider.set_matrix(merged, name_to_coord=merged_n2c)
        self._travel_time_provider.set_name_map(
            {rid: all_restaurants[rid].name for rid in all_restaurants}
        )
        try:
            return self._planner(
                self.requirement,
                spots,
                travel_time_provider=self._travel_time_provider,
                restaurants=resolver,
                first_day_start_time=first_day_start_time,
                last_day_end_minutes=last_day_end_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("带餐厅重排失败，降级为无餐厅计划（阶段 1）：%s", exc)
            return plan1

    def _build_live_restaurants(
        self, name_to_coord: Optional[Dict[str, str]] = None
    ) -> Optional[Any]:
        """B 端 FoodToolLive（真源餐厅）→ ``RestaurantResolver``；任何失败 → None。

        餐厅真源失败（工具缺失 / 搜索无结果 / 无坐标）只降级为「无真源餐厅」，
        不触发整链回退假源（scenic 真源照常）；矩阵内不并入餐厅坐标。
        8.31 P0：注入 ``nearby_pool``（锚点附近搜索，坐标直连）+ 锚点坐标映射
        （附近搜索与矩阵缺边时的 haversine 兜底都要用）；锚点信息缺失时
        nearby_pool 不注入（退化为 8.28 城市级全池口径）。
        """
        if self._tool_provider is None:
            return None
        try:
            from algorithoms._common import _food_preferences
            from data_transmission.live_data import (
                make_live_nearby_restaurants_pool,
                make_live_restaurants_provider,
            )
            from transport.restaurants import RestaurantResolver

            nearby_pool = None
            anchor_locations = {}
            if name_to_coord:
                # 坐标串 "lng,lat" → (lat, lng)，锚点 id 与景点同名（timeline 的
                # current_node[0] 即景点 id，live 模式下 id=scenic_N、name 为地名）。
                for name, coord in name_to_coord.items():
                    parts = str(coord).split(",")
                    if len(parts) == 2:
                        try:
                            anchor_locations[name] = (
                                float(parts[1]),
                                float(parts[0]),
                            )
                        except ValueError:
                            continue
                if anchor_locations:
                    nearby_pool = make_live_nearby_restaurants_pool(
                        self._tool_provider, city=self.city, k=10, radius=2000
                    )
            resolver = RestaurantResolver(
                self.city,
                food_preferences=_food_preferences(self.requirement),
                travel_time_provider=self._travel_time_provider,
                restaurant_provider=make_live_restaurants_provider(self._tool_provider),
                nearby_pool=nearby_pool,
            )
            if anchor_locations:
                resolver.set_anchor_locations(anchor_locations)
                # live 锚点是 scenic id：注入 id→景点名映射，附近搜索/缺边估算
                # 经「id → 名 → 坐标」链取锚点坐标（LiveSpotsSource.names）。
                spot_names = dict(getattr(self._live_spots_source, "names", {}) or {})
                if spot_names:
                    resolver.set_anchor_names(spot_names)
        except Exception as exc:  # noqa: BLE001
            logger.warning("真源餐厅解析失败，跳过餐厅真源化：%s", exc)
            return None
        # 全池空但 nearby_pool 已注入时仍保留 resolver——附近查询独立于全池
        # （8.30 归属制连边：候选餐厅主要来自锚点附近，全池只是兜底数据源）。
        if resolver.restaurants:
            return resolver
        return resolver if getattr(resolver, "_nearby_pool", None) else None