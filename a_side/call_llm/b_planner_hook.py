"""B 契约的 ``planner_hook``：A 侧 Planner / Route Planner 的 B 契约实现。

README §4.1①：B 侧生成初始行程只需一个可调用对象
``__call__(...) -> TripTimeline``，B 侧代码零改动。本模块提供开箱即用的
``BPlannerHook``：

- ``planner_output()``：A 的结构化需求 → B 的 ``PlannerOutput``（最小编制字段
  city / days / budget / interests / avoid，需求细节仍留在 A 的 Requirement 里）
- ``generate_timeline()``：完整跑 A 管线（``select_spots`` → ``plan_multi_day``
  → ``plan_to_trip_timeline``），产出 B 的 ``TripTimeline``——**不调 LLM，可离线**
- ``__call__``：B 的单一入口，等价 ``generate_timeline``；可选 ``planner_input``
  入参（如 B 侧已解析的 ``PlannerOutput``），规划始终以构造时的 ``requirement`` 为准
- ``spots_provider`` / ``planner_fn`` 可注入，便于离线测试与替换实现；缺省走
  A 侧真实模块。任一环节失败 → 返回**空 ``TripTimeline``**（含 city/日期占位），
  错误记录在 ``last_error``，由 B 侧决定是否提示用户

联调接入示例（替换 B 仓库 django_server/runtime/a_interface.py 的
build_planner_hook，与 ``build_decision_hook`` 并列）：

.. code-block:: python

    from call_llm.b_planner_hook import BPlannerHook

    def build_planner_hook(tool_provider=None):
        return BPlannerHook(
            requirement=requirement,          # A 侧结构化需求（Requirement）
            city="北京",
            start_date="2026-08-21",
            plan_id="plan_001",
        )

**类身份约定**：契约导入与 ``data_transmission/b_contract.py`` 一致，全部用
顶层 ``import core.schemas``，保证 B 进程内 ``isinstance(timeline, TripTimeline)``
成立（详见 ``data_transmission/b_contract.py`` 模块 docstring）。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

logger = logging.getLogger("call_llm.b_planner_hook")

from core.schemas import PlannerOutput, TripTimeline  # noqa: E402
from data_transmission.b_contract import (  # noqa: E402
    _as_date,
    plan_to_trip_timeline,
    requirement_to_planner_output,
)


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


class BPlannerHook:
    """A 侧 Planner 的 B 契约实现（可调用对象）。

    构造参数：
        requirement: A 侧结构化需求（含 ``content`` 键，与 A 现有管线一致）
        city: 行程城市，缺省取需求的 destination
        plan_id: 产出 ``TripTimeline`` 的计划 ID
        start_date: 行程开始日期（str/datetime/date），缺省取需求的 start_date
        spots_provider: ``fn(city) -> candidate_spots``，缺省用
            ``algorithoms.select_spots.select_spots`` 重新挑选（ask_user_on_conflict=False）
        planner_fn: ``fn(requirement, spots) -> plan dict``，缺省用
            ``algorithoms.planner.plan_multi_day``
        ask_user_on_conflict: 传给缺省 spots_provider 的冲突询问开关（默认关）
        tool_provider: B 侧工具门面（真实数据接入时传入；配合 USE_LIVE_DATA=1
            走真源，失败自动回退假数据，见 generate_timeline）
    """

    def __init__(
        self,
        requirement: Dict[str, Any],
        *,
        city: Optional[str] = None,
        plan_id: str = "",
        start_date: Any = None,
        spots_provider: Optional[Callable[[str], Any]] = None,
        planner_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        ask_user_on_conflict: bool = False,
        tool_provider: Any = None,
    ) -> None:
        """构造参数见类 docstring。

        ``tool_provider``：B 侧工具门面（``ToolProvider.call("scenic"/"map"/...)``）。
        当传入且环境变量 ``USE_LIVE_DATA`` 开启时，规划层走**真实数据**：
        - 候选池：``live_data.LiveSpotsSource``（scenic 工具）→ 失败回退假数据；
        - 交通矩阵：``LiveTravelTimeProvider``（map 工具 ETA，双向缓存）；
        - 餐厅/未映射节点（如本地假餐厅 id）不发真实请求，按 0 通勤降级；
        - 结果通过 ``last_data_source`` 记录（"live" / "fake" / "live_fallback"）。
        未传 ``tool_provider`` 或 ``USE_LIVE_DATA`` 关闭时，行为与既往完全一致（假数据）。
        """
        self.requirement = requirement if isinstance(requirement, dict) else {}
        content = self.requirement.get("content") or {}
        if not isinstance(content, dict):
            content = {}
        self.city = city or str(content.get("destination") or "")
        self.plan_id = plan_id
        if start_date is None:
            start_date = content.get("start_date")
        self.start_date = start_date
        self._ask_user_on_conflict = bool(ask_user_on_conflict)
        self._planner_fn = planner_fn
        self.last_error: Optional[str] = None
        # 数据源记录：fake（假数据）/ live（真实数据）/ live_fallback（真源失败回退假）
        self.last_data_source: str = "fake"
        # A 侧内部计划缓存：首次规划后保留，可被决策钩子（replan）复用
        self._current_plan: Optional[Dict[str, Any]] = None
        self._current_timeline: Optional[TripTimeline] = None

        if spots_provider is None:
            requirement_ref = self.requirement
            ask = self._ask_user_on_conflict

            def _default_loader(_city: str) -> Any:
                from algorithoms.select_spots import select_spots

                return select_spots(
                    requirement_ref,
                    ask_user_on_conflict=ask,
                )

            spots_provider = _default_loader
        self._spots_provider: Callable[[str], Any] = spots_provider

        # 真实数据接入（USE_LIVE_DATA=1 且给了 tool_provider）
        from data_transmission.live_data import (
            LiveDataError,
            _coord_str,
            make_live_eta_fn,
            make_live_matrix_fn,
            make_live_spots_provider,
            use_live_data,
        )
        from transport.providers import LiveTravelTimeProvider

        self._live_data_error = LiveDataError
        self._use_live = bool(tool_provider) and use_live_data()
        self._live_spots_provider: Optional[Callable[[str], Any]] = None
        self._travel_time_provider: Optional[LiveTravelTimeProvider] = None
        if self._use_live:
            self._tool_provider = tool_provider
            live_source = make_live_spots_provider(tool_provider)
            ask = self._ask_user_on_conflict

            def _live_loader(_city: str) -> Any:
                from algorithoms.select_spots import select_spots

                return select_spots(
                    self.requirement,
                    ask_user_on_conflict=ask,
                    spots_provider=live_source,
                )

            self._live_spots_provider = _live_loader
            self._live_spots_source = live_source
            self._travel_time_provider = LiveTravelTimeProvider(
                make_live_eta_fn(tool_provider, city=self.city),
                name_by_id={},
            )

    # -- 内部 --------------------------------------------------------------

    def _planner(
        self,
        requirement: Dict[str, Any],
        spots: Any,
        travel_time_provider: Any = None,
        restaurants: Any = None,
    ) -> Dict[str, Any]:
        if self._planner_fn is not None:
            # 自定义 planner_fn 保持原契约 (requirement, spots)；真源接线由注入方负责
            return self._planner_fn(requirement, spots)
        from algorithoms.planner import plan_multi_day

        # 8.28：restaurants 可为真源 RestaurantResolver（meal 段锚定真实餐厅）；
        # 为 None 时 plan_multi_day 内部照旧走 _resolve_restaurants（假池）。
        if travel_time_provider is not None:
            return plan_multi_day(
                requirement,
                spots,
                travel_time_provider=travel_time_provider,
                restaurants=restaurants,
            )
        return plan_multi_day(requirement, spots, restaurants=restaurants)

    def _empty_timeline(self) -> TripTimeline:
        start = _as_date(self.start_date)
        return TripTimeline(
            id=self.plan_id,
            city=self.city,
            start_date=start,
            end_date=start,
            days=[],
        )

    # -- 对外能力 ----------------------------------------------------------

    def planner_output(self) -> PlannerOutput:
        """A 的结构化需求 → B 的 ``PlannerOutput``（只含最小编制字段）。"""
        return requirement_to_planner_output(self.requirement)

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

    def generate_timeline(self, *, regenerate: bool = False) -> TripTimeline:
        """完整跑 A 管线并返回 B 的 ``TripTimeline``。

        - 已缓存且非 ``regenerate`` → 直接返回缓存（幂等）；
        - ``USE_LIVE_DATA=1`` 且注入 ``tool_provider`` → 真源优先：候选池 / 规划
          任一步失败自动回退假数据管线（``last_data_source="live_fallback"``）；
        - 任一步骤失败 → 记录 ``last_error`` 并返回空时间轴（不抛异常）。
        """
        if not regenerate and self._current_timeline is not None:
            return self._current_timeline
        if self._use_live:
            return self._generate_live_or_fallback()
        return self._run_pipeline(self._spots_provider, None, source="fake")

    def _generate_live_or_fallback(self) -> TripTimeline:
        """真源优先：候选池 / 规划任一步失败 → 回退假数据管线（保留失败原因）。

        8.30 矩阵瘦身（两阶段，替代先前 36 节点一次性整矩阵）：
        - 阶段 1：一次 ``batch_route`` 只含「候选景点 + 假池酒店」（不再预置 20 家
          餐厅）→ 排一版**无餐厅**计划，确定每天用餐窗口临近的景点（锚点）；
        - 阶段 2：只对「计划内景点 × 真源餐厅」补一张正交小矩阵（远小于全集合
          n²），合并后带餐厅重新规划；餐厅 / 增量矩阵失败 → 沿用阶段 1 计划
          （无餐厅段），不拖累 scenic / 酒店真源链路。
        """
        from data_transmission.live_data import _coord_str, make_live_matrix_fn

        try:
            spots = self._live_spots_provider(self.city)
        except Exception as exc:  # noqa: BLE001
            reason = f"真实数据接入失败，已回退假数据：{exc}"
            timeline = self._run_pipeline(self._spots_provider, None, source="fake")
            self.last_data_source = "live_fallback"
            self.last_error = reason
            return timeline
        base_matrix: Dict[Tuple[str, str], Tuple[float, int]] = {}
        live_hotels = self._live_hotel_pool()  # 8.29：假池酒店候选（坐标并入矩阵 → 通勤真源）
        try:
            if self._travel_time_provider is not None:
                self._travel_time_provider.set_name_map(
                    self._live_spots_source.names
                )
                # 阶段 1：scenic 已返回真实坐标 → 一次 batch_route 取候选+酒店矩阵。
                # 坐标直连（B 侧跳过地理编码）：消灭 QPS 突刺（10021）与怪名 POI
                # 编码失败（30001）；矩阵构建失败与规划失败同走回退假源。
                source_spots = self._live_spots_source.spots or self._live_spots_source(
                    self.city
                )
                name_to_coord = {
                    spot["name"]: coord
                    for spot in source_spots
                    if spot.get("name") and (coord := _coord_str(spot.get("location")))
                }
                # 8.29 酒店通勤真源化：假池酒店候选坐标并入矩阵（B4 HotelTool
                # 就绪前酒店本体仍是候选源；通勤先真源化）→ HotelSelector 走矩阵分钟。
                for hotel in live_hotels:
                    coord = _coord_str(
                        {"lat": hotel.location[0], "lng": hotel.location[1]}
                    )
                    if coord and hotel.name not in name_to_coord:
                        name_to_coord[hotel.name] = coord
                if name_to_coord:
                    base_matrix = make_live_matrix_fn(
                        self._tool_provider, city=self.city
                    )(name_to_coord)
                    self._travel_time_provider.set_matrix(
                        base_matrix, name_to_coord=name_to_coord
                    )
                    # 酒店 id → 点名（与 scenic 增量合并，set_name_map 为 update 语义）
                    self._travel_time_provider.set_name_map(
                        {hotel.id: hotel.name for hotel in live_hotels}
                    )
                # 阶段 1 规划：restaurants=None → meal 段抽象无餐厅（plan_multi_day
                # 不自行拉假池餐厅），只用于确定用餐锚点 + 计划内景点集合。
                plan1 = self._planner(
                    self.requirement,
                    spots,
                    travel_time_provider=self._travel_time_provider,
                    restaurants=None,
                )
                # 阶段 2：锚点确定后，只对候选景点 × 真源餐厅补增量矩阵再重排
                plan = self._live_plan_with_restaurants(
                    plan1, spots, name_to_coord, base_matrix, live_hotels
                )
            else:
                plan = self._planner(self.requirement, spots)
        except Exception as exc:  # noqa: BLE001
            reason = f"真实数据接入失败，已回退假数据：{exc}"
            timeline = self._run_pipeline(self._spots_provider, None, source="fake")
            self.last_data_source = "live_fallback"
            self.last_error = reason
            return timeline
        if not isinstance(plan, dict) or not plan.get("days"):
            reason = "真实数据接入失败（规划未产出可用计划），已回退假数据"
            timeline = self._run_pipeline(self._spots_provider, None, source="fake")
            self.last_data_source = "live_fallback"
            self.last_error = reason
            return timeline

        self._current_plan = plan
        self.last_error = None
        self.last_data_source = "live"
        self._attach_hotels(plan)
        timeline = plan_to_trip_timeline(
            plan,
            city=self.city,
            start_date=self.start_date,
            plan_id=self.plan_id,
        )
        self._current_timeline = timeline
        return timeline

    def _live_plan_with_restaurants(
        self,
        plan1: Dict[str, Any],
        spots: Any,
        name_to_coord: Dict[str, str],
        base_matrix: Dict[Tuple[str, str], Tuple[float, int]],
        live_hotels: Sequence[Any],
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
        resolver = self._build_live_restaurants()
        if resolver is None:
            return plan1
        from data_transmission.live_data import _coord_str, make_live_matrix_fn

        rest_coords: Dict[str, str] = {}
        for restaurant in resolver.restaurants:
            coord = _coord_str(
                {"lat": restaurant.location[0], "lng": restaurant.location[1]}
            )
            if coord:
                rest_coords[restaurant.name] = coord
        hotel_names = {hotel.name for hotel in live_hotels}
        # 全部候选景点作起点（排除酒店与餐厅；酒店↔餐厅无需边，酒店↔景点在矩阵1）
        origin_names = [
            name
            for name in name_to_coord
            if name not in hotel_names and name not in rest_coords
        ]
        if not origin_names or not rest_coords:
            return plan1
        try:
            matrix2 = make_live_matrix_fn(self._tool_provider, city=self.city)(
                {**name_to_coord, **rest_coords},
                origins=origin_names,
                destinations=list(rest_coords),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("真源餐厅增量矩阵失败，保留无餐厅计划：%s", exc)
            return plan1
        # 合并两阶段矩阵（坐标对为键；增量矩阵补双向），换入后重排
        merged = dict(base_matrix)
        for (origin_coord, dest_coord), value in matrix2.items():
            merged[(origin_coord, dest_coord)] = value
            merged[(dest_coord, origin_coord)] = value
        merged_n2c = dict(name_to_coord)
        merged_n2c.update(rest_coords)
        self._travel_time_provider.set_matrix(merged, name_to_coord=merged_n2c)
        self._travel_time_provider.set_name_map(
            {restaurant.id: restaurant.name for restaurant in resolver.restaurants}
        )
        try:
            return self._planner(
                self.requirement,
                spots,
                travel_time_provider=self._travel_time_provider,
                restaurants=resolver,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("带餐厅重排失败，降级为无餐厅计划（阶段 1）：%s", exc)
            return plan1

    def _build_live_restaurants(self) -> Optional[Any]:
        """B 端 FoodToolLive（真源餐厅）→ ``RestaurantResolver``；任何失败 → None。

        餐厅真源失败（工具缺失 / 搜索无结果 / 无坐标）只降级为「无真源餐厅」，
        不触发整链回退假源（scenic 真源照常）；矩阵内不并入餐厅坐标。
        """
        if self._tool_provider is None:
            return None
        try:
            from algorithoms._common import _food_preferences
            from data_transmission.live_data import make_live_restaurants_provider
            from transport.restaurants import RestaurantResolver

            resolver = RestaurantResolver(
                self.city,
                food_preferences=_food_preferences(self.requirement),
                travel_time_provider=self._travel_time_provider,
                restaurant_provider=make_live_restaurants_provider(self._tool_provider),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("真源餐厅解析失败，跳过餐厅真源化：%s", exc)
            return None
        return resolver if resolver.restaurants else None

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
            except Exception as exc:  # noqa: BLE001
                logger.warning("hotel 真源失败，回退假池：%s", exc)
        try:
            from data_transmission.hotel import load_hotels

            return list(load_hotels(self.city))
        except Exception:  # noqa: BLE001
            return []

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

    def _run_pipeline(
        self,
        spots_provider: Callable[[str], Any],
        travel_time_provider: Any,
        source: str,
    ) -> TripTimeline:
        """假数据（或回退）管线：候选池 → 规划 → 时间轴；失败降级为空时间轴。"""
        # 1) 候选池
        try:
            spots = spots_provider(self.city)
        except Exception as exc:  # noqa: BLE001  失败降级为空时间轴
            self.last_error = f"候选池加载失败：{exc}"
            timeline = self._empty_timeline()
            self._current_timeline = timeline
            return timeline

        # 2) 规划
        try:
            plan = self._planner(
                self.requirement, spots, travel_time_provider=travel_time_provider
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"规划失败：{exc}"
            timeline = self._empty_timeline()
            self._current_timeline = timeline
            return timeline
        if not isinstance(plan, dict) or not plan.get("days"):
            self.last_error = "规划未产出可用计划"
            timeline = self._empty_timeline()
            self._current_timeline = timeline
            return timeline

        self._current_plan = plan
        self.last_error = None
        self.last_data_source = source
        self._attach_hotels(plan)
        timeline = plan_to_trip_timeline(
            plan,
            city=self.city,
            start_date=self.start_date,
            plan_id=self.plan_id,
        )
        self._current_timeline = timeline
        return timeline

    # -- B 契约入口 --------------------------------------------------------

    def __call__(self, planner_input: Any = None) -> TripTimeline:
        """B 的 planner_hook 入口：返回 ``TripTimeline``。

        ``planner_input`` 可传 B 侧已解析的 ``PlannerOutput`` 等对象，仅作
        调用约定占位，规划内容以构造时的 ``requirement`` 为准。
        """
        return self.generate_timeline()