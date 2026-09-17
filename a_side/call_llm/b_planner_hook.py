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

**P4 拆分（2026-09-02）**：本模块 BPlannerHook 只剩**编排**——构造装配、
规划器包装、两态分派（live → fake 回退）。城际段 / 餐厅 / 酒店 / 数据源三态
四大职责拆到 ``call_llm/planner_parts/`` 包（各为 mixin，``BPlannerHook`` 继承）：
``TripSegmentAttacher`` / ``RestaurantOrchestrator`` / ``HotelAttacher`` /
``DataSourceResolver``。对外接口签名**不变**（B 侧 ``a_interface.py`` 零改动），
既有测试不变（私有方法经继承保留；模块级纯函数在本文件 re-export）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import logging

logger = logging.getLogger("call_llm.b_planner_hook")


def _merge_extra_results(
    live_source: Any, spots_result: Any, extra: Any
) -> Any:
    """补搜结果并入候选池，并重建 live_source 状态（2026-09-17 补搜污染修复）。

    两件事，缺一不可：

    1. **id 续编**：B 侧 scenic 工具每次搜索都从 ``scenic_0`` 起编号
       （``scenic_tool.py``），补搜批与主搜批 id 空间重叠——直接并入会出现
       同 id 不同名，矩阵/排程节点混淆。新增景点 id 从现有池最大序号 +1 续编。
    2. **全池不变量**：``LiveSpotsSource.__call__`` 是整批替换语义
       （``names``/``spots`` = 最近一次拉取），补搜后主搜批的 id→名称/坐标
       映射全部丢失（2026-09-16 西安事变/李自成实测：规划期取主搜批景点
       通勤边抛「缺少节点名称映射」→ 整链回退假源 → 400）。并入后把
       ``names``/``spots`` 重建为**恒等于当前全池**，保证供给期矩阵与名称
       映射覆盖全部候选。

    ``extra`` 非 list / 无新增时也重建（只要补搜调用发生过，状态就需要归位）。
    返回合并后的池。
    """
    pool = list(spots_result) if isinstance(spots_result, list) else []
    added: list = []
    if isinstance(extra, list):
        seen = {str(s.get("name") or "") for s in pool if isinstance(s, dict)}
        added = [
            s for s in extra
            if isinstance(s, dict) and str(s.get("name") or "") not in seen
        ]
    if added:
        base = 0
        for s in pool:
            sid = str(s.get("id") or "")
            if sid.startswith("scenic_"):
                try:
                    base = max(base, int(sid.split("_", 1)[1]) + 1)
                except ValueError:
                    pass
        for offset, s in enumerate(added):
            s["id"] = f"scenic_{base + offset}"
        pool = pool + added
    # 状态重建：names/spots 恒等于全池（含 id 续编后的最终形态）
    if live_source is not None:
        live_source.names = {
            str(s.get("id") or s["name"]): s["name"] for s in pool
        }
        live_source.spots = pool
    return pool

from core.schemas import PlannerOutput, TripTimeline  # noqa: E402
from data_transmission.b_contract import (  # noqa: E402
    _as_date,
    requirement_to_planner_output,
)
from data_transmission.enums import PipelineSource  # noqa: E402

from call_llm.planner_parts import (  # noqa: E402
    DataSourceResolver,
    HotelAttacher,
    RestaurantOrchestrator,
    TripSegmentAttacher,
)

# 模块级纯函数 re-export（历史命名空间兼容：测试直接
# ``from TravelAgent.call_llm.b_planner_hook import ...``）
from call_llm.planner_parts.restaurants import (  # noqa: E402
    _collect_meal_anchors,
    _collect_plan_spot_names,
)
from call_llm.planner_parts.trip_segments import (  # noqa: E402
    _first_day_start_from_segments,
    _last_day_end_from_segments,
    _realize_outbound_with_schedule,
    _rebuild_return_with_schedule,
    _select_outbound_combination,
    _select_return_combination,
    _find_return_segment,
    _windowed_last_day_end,
)


class BPlannerHook(
    TripSegmentAttacher, HotelAttacher, RestaurantOrchestrator, DataSourceResolver
):
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

    职责（P4 拆分后）：构造装配 + 规划器包装 + 编排分派；
    城际段/餐厅/酒店/数据源三态由 ``planner_parts`` 各 mixin 承载。
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
        search_planner: Optional[Any] = None,
    ) -> None:
        """构造参数见类 docstring。

        ``search_planner``（9.2 十二节 A，可选）：LLM 定制候选池搜索计划器
        （``plan_for(destination, days, preferred_tags, must_visit) -> dict|None``）。
        缺省 None → 内部建 ``ScenicSearchPlanner()``（``USE_LLM_TOOLS`` 门控
        默认关 → 返回 None → 走 B 侧固定词表，零回归）；传入实例可注入/测试。

        ``tool_provider``：B 侧工具门面（``ToolProvider.call("scenic"/"map"/...)``）。
        当传入且环境变量 ``USE_LIVE_DATA`` 开启时，规划层走**真实数据**：
        - 候选池：``live_data.LiveSpotsSource``（scenic 工具）→ 失败回退假数据；
        - 交通矩阵：``LiveTravelTimeProvider``（map 工具 ETA，双向缓存）；
        - 餐厅/未映射节点（如本地假餐厅 id）不发真实请求，按 0 通勤降级；
        - 结果通过 ``last_data_source`` 记录（live / fake / live_fallback，见
          ``data_transmission.enums.PipelineSource``）。
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
        # 9.2 十二节 A：LLM 定制候选池搜索计划器（缺省内部建；gate off → None）
        if search_planner is None:
            from call_llm.scenic_search_planner import ScenicSearchPlanner

            search_planner = ScenicSearchPlanner()
        self._search_planner: Optional[Any] = search_planner
        self._search_plan: Optional[Dict[str, Any]] = None
        self._search_plan_tried: bool = False
        # 数据源记录（PipelineSource）：fake（假数据）/ live（真实数据）/
        # live_fallback（真源失败回退假）
        self.last_data_source: str = PipelineSource.FAKE.value
        # 阶段 b（2026-09-14）：编排门控开时最近一次编排循环的完整结果
        # （PlanOrchestrator.run() 返回 dict：plan/accepted/quality_history/
        # preferred_stations/...）——探针/复验读编排现场，门控关恒为 None
        self._orchestration_result: Optional[Dict[str, Any]] = None
        # 降级告知（真源查不到 → 告知用户 + 假源/估算替代，2026-09-14）：
        # 人话清单，随计划透出（B 侧 plan 响应/status 的 notices 字段）
        self.fallback_notices: List[str] = []
        # 候选池 LLM 轨迹（plan_trace 工作项，2026-09-16）：ScenicSearchPlanner
        # 的 generate 轨迹经 data_source 透传，B 侧 runtime 收集进 plan trace；
        # 门控关 / LLM 失败保持 None（B 侧按缺席降级）
        self.last_llm_trace: Optional[Dict[str, Any]] = None
        # LLM 兜底两件套（2026-09-16，用户设计：选完点交大模型审核，过了加
        # 时长字段、没过重选——迭代）——USE_LLM_TOOLS 门控，失败 = 保持既往
        self._pool_review = None         # 池审核：verdict/exclude/补搜建议词
        self._duration_estimator = None  # 通过后加时间字段（估时）
        if tool_provider is not None:
            try:
                from call_llm.duration_estimator import build_llm_duration_estimator
                from call_llm.scenic_filter import build_llm_pool_review

                self._pool_review = build_llm_pool_review()
                self._duration_estimator = build_llm_duration_estimator()
            except Exception:  # noqa: BLE001
                self._pool_review = None
                self._duration_estimator = None
        # A 侧内部计划缓存：首次规划后保留，可被决策钩子（replan）复用
        self._current_plan: Optional[Dict[str, Any]] = None
        self._current_timeline: Optional[TripTimeline] = None
        # P5.7-S3：live 路径的 LLM 按日簇中心计划（center_schedule）——由
        # _live_loader 惰性赋值，_planner 据此构造 affinity_fn 按日择优。
        self._live_center_schedule: Optional[list] = None

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
            make_live_eta_fn,
            make_live_spots_provider,
            use_live_data,
        )
        from transport.providers import LiveTravelTimeProvider

        self._live_data_error = LiveDataError
        self._use_live = bool(tool_provider) and use_live_data()
        # 工具门面无条件保存：live 分支用 USE_LIVE_DATA 门控；固定 Demo 候选链路
        # （锦州→上海 fixture，断网可复现）与开关无关，也经 _tool_provider 取工具。
        # P3-D1 组装入口收敛：全部真源调用统一过额度管家（QuotaManager）——
        # 无预算包一层仅计数（stats 可观察，探针/验收用）；per-mode 预算语义由
        # make_live_intercity_provider 内层 QuotaManager 承载（预算嵌套，外层
        # 不限量）。行为零变化：无预算时不限额、不节律，仅多一层计数。
        self._tool_provider = tool_provider
        if tool_provider is not None:
            from data_transmission.quota_manager import make_quota_manager

            self._tool_provider = make_quota_manager(tool_provider)
        self._live_spots_provider: Optional[Callable[[str], Any]] = None
        self._travel_time_provider: Optional[LiveTravelTimeProvider] = None
        if self._use_live:
            live_source = make_live_spots_provider(self._tool_provider)
            ask = self._ask_user_on_conflict

            def _live_loader(_city: str) -> Any:
                from algorithoms.select_spots import select_spots

                city_plan = getattr(self, "city_plan", None) or []

                def _single_city_select(target_city: str) -> Any:
                    # 单城原路径（方案 b / 无多城计划时，语义零变化）
                    search_plan = self._search_plan_once(target_city)
                    trip_center = None
                    center_schedule = None
                    if isinstance(search_plan, dict):
                        trip_center = search_plan.get("trip_center")
                        cs = search_plan.get("center_schedule")
                        if isinstance(cs, list) and cs:
                            center_schedule = cs
                    self._live_center_schedule = center_schedule

                    def _source_with_limit(city: str):
                        return self._build_city_pool(
                            city, self._pool_days_limit(), search_plan
                        )

                    return select_spots(
                        self.requirement,
                        ask_user_on_conflict=ask,
                        spots_provider=_source_with_limit,
                        trip_center=trip_center,
                        center_schedule=center_schedule,
                    )

                if not city_plan:
                    return _single_city_select(_city)

                # 多城（方案 c 阶段 2，2026-09-17）：逐城「主搜+审池迭代+估时」
                # → 逐城 select_spots（per-city requirement：destination/days
                # 换成该城）→ 按 select_spots 契约**三元组**合并（must/conflicts/
                # scored；conflicts 为各城未决冲突，直接丢弃）→ spot.city 落地
                # → plan_multi_day 城市亲和门控分天（_planner /
                # build_city_affinity_fn）。降级（D8）：某城失败剔城并告知；
                # 全部失败回退单城原路径。
                merged_must: List[Dict[str, Any]] = []
                merged_scored: List[Dict[str, Any]] = []
                seen_names: set = set()
                union_pools: List[Dict[str, Any]] = []
                union_names: Dict[str, str] = {}
                for cp in city_plan:
                    city = str(cp.get("city") or "")
                    cdays = int(cp.get("day_to") or 0) - int(cp.get("day_from") or 0) + 1
                    if not city or cdays < 1:
                        continue
                    try:
                        pool, triple = self._select_city_spots(city, cdays)
                        must, _conflicts, scored = triple
                    except Exception as exc:  # noqa: BLE001  剔城不阻断
                        logger.warning("多城候选构建失败（%s），剔城：%s", city, exc)
                        if callable(getattr(self, "_add_notice", None)):
                            self._add_notice(f"{city} 候选获取失败，已从连游中剔除")
                        continue
                    for s in pool or []:
                        name = str(s.get("name") or "")
                        if name and name not in union_names:
                            union_names[name] = name
                            s["city"] = city     # 城市字段落地（亲和门控依赖）
                            union_pools.append(s)
                    for bucket, spots_part in (
                        (merged_must, must), (merged_scored, scored),
                    ):
                        for s in spots_part or []:
                            name = str(s.get("name") or "")
                            if not name or name in seen_names:
                                continue
                            seen_names.add(name)
                            s["city"] = city
                            bucket.append(s)
                if not merged_must and not merged_scored:
                    logger.warning("多城候选全部失败，回退单城路径（%s）", _city)
                    return _single_city_select(_city)
                # 全池不变量：供给期矩阵/名称映射覆盖全部候选（跨城合并语义）
                live_source.spots = union_pools
                live_source.names = {
                    str(s.get("id") or s.get("name") or ""): str(s.get("name") or "")
                    for s in union_pools
                }
                return [merged_must, [], merged_scored]

            self._live_spots_provider = _live_loader
            self._live_spots_source = live_source
            self._travel_time_provider = LiveTravelTimeProvider(
                make_live_eta_fn(self._tool_provider, city=self.city),
                name_by_id={},
            )

    # -- 内部 --------------------------------------------------------------

    def _planner(
        self,
        requirement: Dict[str, Any],
        spots: Any,
        travel_time_provider: Any = None,
        restaurants: Any = None,
        first_day_start_time: Optional[str] = None,
        last_day_end_minutes: Optional[int] = None,
        min_spots: int = 0,
    ) -> Dict[str, Any]:
        if self._planner_fn is not None:
            # 自定义 planner_fn 保持原契约 (requirement, spots)；真源接线由注入方负责
            return self._planner_fn(requirement, spots)
        from algorithoms.planner import plan_multi_day

        # P5.7-S3：center_schedule（多中心按日）→ 按日 affinity_fn 传给分配器；
        # None（门控关/单中心/LLM 失败）→ affinity_fn=None，原路径零回归。
        # P5.7 修复 A（2026-09-04）：同一份 schedule 解析出的按日锚点（day_anchors）
        # 同时喂 must 分天（beam 中心偏离惩罚）——此前 must 分天不感知簇计划，
        # 必去被排进与簇冲突的天（张掖实测：平山湖落 Day1 市区簇日、Day5-6
        # 平山湖簇日被掏空），可选分配的亲和救不了已错位的锚。
        affinity_fn = None
        day_anchors = None
        city_plan = getattr(self, "city_plan", None) or []
        if spots and city_plan:
            # 方案 c 阶段 2：城市门控亲和（非当日城大负分，实现「哪几天在
            # 哪个城就选哪个城的景点」）；POI 级中心计划暂不叠加（多城城内
            # 分布由评分与时间窗决定，阶段 2.5 叠 per-city center_schedule）
            from algorithoms.select_spots import build_city_affinity_fn

            affinity_fn = build_city_affinity_fn(city_plan)
        elif spots and getattr(self, "_live_center_schedule", None):
            from algorithoms.select_spots import (
                build_center_affinity_fn,
                resolve_day_anchors,
            )

            day_count = int((requirement.get("content") or {}).get("days") or 0)
            day_anchors = resolve_day_anchors(
                self._live_center_schedule, spots, day_count,
            )
            affinity_fn = build_center_affinity_fn(
                self._live_center_schedule, spots, day_count,
            )

        # 8.28：restaurants 可为真源 RestaurantResolver（meal 段锚定真实餐厅）；
        # 为 None 时 plan_multi_day 内部照旧走 _resolve_restaurants（假池）。
        if travel_time_provider is not None:
            return plan_multi_day(
                requirement,
                spots,
                travel_time_provider=travel_time_provider,
                restaurants=restaurants,
                first_day_start_time=first_day_start_time,
                last_day_end_minutes=last_day_end_minutes,
                affinity_fn=affinity_fn,
                day_anchors=day_anchors,
                min_spots=min_spots,
            )
        return plan_multi_day(
            requirement,
            spots,
            restaurants=restaurants,
            first_day_start_time=first_day_start_time,
            last_day_end_minutes=last_day_end_minutes,
            affinity_fn=affinity_fn,
            day_anchors=day_anchors,
            min_spots=min_spots,
        )

    def _build_city_pool(
        self, city: str, limit: int, search_plan: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """单城候选池构建（主搜 + 审池迭代 + 补搜 + 估时）。

        从 `_source_with_limit` 抽出（方案 c 阶段 2）：单城路径与多城路径
        共用同一套迭代流。语义与既往完全一致（含 `_merge_extra_results`
        全池不变量修复）。
        """
        spots_result = self._live_spots_source(
            city,
            limit=limit,
            ensure_spots=self._must_visit_names(),
            search_plan=search_plan,
        )
        # 审核迭代（2026-09-16 用户设计）：LLM 审池 → fail 则剔除并按建议词
        # 补搜重选 → 过了才加时间字段。上限 2 轮；审核失败/门控关 → 当前池
        if isinstance(spots_result, list) and self._pool_review is not None:
            for _round in range(2):
                try:
                    spots_result, meta = self._pool_review(spots_result)
                except Exception:  # noqa: BLE001
                    break
                if meta.get("verdict") == "pass":
                    break
                # fail：按建议词补搜重选（仅第一轮 fail 后补搜）
                more = meta.get("more_keywords") or []
                if _round == 0 and more:
                    extra_plan = {"buckets": [
                        {"keywords": [city, "景点"], "quota_ratio": 0.5},
                        {"keywords": more[:4], "quota_ratio": 0.5},
                    ]}
                    try:
                        extra = self._live_spots_source(
                            city, limit=limit,
                            ensure_spots=[], search_plan=extra_plan,
                        )
                        # 2026-09-17 补搜污染修复：id 续编 + names/spots
                        # 全池不变量重建
                        spots_result = _merge_extra_results(
                            self._live_spots_source, spots_result, extra
                        )
                    except Exception:  # noqa: BLE001
                        pass
        # 过了审核（或迭代耗尽）→ 加时间字段（LLM 估时，原地填 duration）
        if self._duration_estimator is not None and isinstance(spots_result, list):
            try:
                self._duration_estimator(spots_result)
            except Exception:  # noqa: BLE001
                pass
        return spots_result

    def _select_city_spots(
        self, city: str, cdays: int
    ) -> tuple:
        """多城管线的每城步骤：城内池构建 → select_spots 按该城天数挑选。

        per-city requirement：destination/days 换成该城（select_spots 内部
        按 destination 过滤池子、天数驱动分配）；必去收敛到该城池内（其他
        城的必去不进本城选择）。返回 ``(pool, selected)``——pool 供多城
        合并全池（矩阵/名称映射），selected 供跨城合并分天。
        """
        from algorithoms.select_spots import select_spots

        search_plan = self._search_plan_once(city)
        limit = max(10, cdays * 5)
        pool = self._build_city_pool(city, limit, search_plan)
        content = self.requirement.get("content") or {}
        pool_names = {str(s.get("name") or "") for s in pool or []}
        req_city = {
            **self.requirement,
            "content": {
                **content,
                "destination": city,
                "days": cdays,
                "constraints": {
                    **(content.get("constraints") or {}),
                    "must_visit": [
                        m for m in (content.get("constraints") or {}).get("must_visit") or []
                        if str(m) in pool_names
                    ],
                },
            },
        }
        selected = select_spots(
            req_city,
            ask_user_on_conflict=self._ask_user_on_conflict,
            spots_provider=lambda _c: pool,
        )
        return pool, selected

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

    def generate_timeline(self, *, regenerate: bool = False) -> TripTimeline:
        """完整跑 A 管线并返回 B 的 ``TripTimeline``。

        - 已缓存且非 ``regenerate`` → 直接返回缓存（幂等）；
        - ``USE_LIVE_DATA=1`` 且注入 ``tool_provider`` → 真源优先：候选池 / 规划
          任一步失败自动回退假数据管线（``last_data_source="live_fallback"``）；
        - 任一步骤失败 → 记录 ``last_error`` 并返回空时间轴（不抛异常）。
        """
        if not regenerate and self._current_timeline is not None:
            return self._current_timeline
        self.fallback_notices = []  # 每次规划 fresh（告知只描述本次降级）
        # 地名映射方案 b 完整版（2026-09-17）：目的地/出发地归一**先行**——
        # 此前归一只在 _build_trip_segments（城际构建）里做，目的地为区域名
        # （「东北」）时景点池已按原名搜出垃圾或直接 LiveDataError。提前到
        # 管线入口：LLM 地址→主城 + 二次确认写回 + self.city 联动（池/矩阵/
        # 城际全链读干净地名）。_build_trip_segments 内的调用保留（幂等：
        # 已归一城市二次 normalize 直接命中，不再触发 LLM）。
        try:
            content0 = self.requirement.get("content")
            if isinstance(content0, dict) and (
                content0.get("origin") or content0.get("destination")
            ):
                self._normalize_intercity_places(content0)
        except Exception as exc:  # noqa: BLE001  归一失败不阻断规划
            logger.warning("入口地名归一失败（用原值）：%s", exc)
        if self._use_live:
            # 阶段 b（2026-09-14）：编排门控开 → LLM 主导编排路径（未接受/
            # 异常真回落固定管线）；默认关 → 原固定管线零回归。
            # 方案 c 阶段 2（2026-09-17）：多城 city_plan 在场时跳过编排——
            # 编排器工作台按单城假设搭建，多城走固定管线（城市亲和门控）。
            from call_llm.orchestrator import use_llm_orchestrator

            if use_llm_orchestrator() and not getattr(self, "city_plan", None):
                return self._generate_orchestrated()
            return self._generate_live_or_fallback()
        return self._run_pipeline(
            self._spots_provider, None, source=PipelineSource.FAKE.value
        )

    # -- B 契约入口 --------------------------------------------------------

    def __call__(self, planner_input: Any = None) -> TripTimeline:
        """B 的 planner_hook 入口：返回 ``TripTimeline``。

        ``planner_input`` 可传 B 侧已解析的 ``PlannerOutput`` 等对象，仅作
        调用约定占位，规划内容以构造时的 ``requirement`` 为准。
        """
        return self.generate_timeline()