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
    _rebuild_return_with_schedule,
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
    ) -> None:
        """构造参数见类 docstring。

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
        # 数据源记录（PipelineSource）：fake（假数据）/ live（真实数据）/
        # live_fallback（真源失败回退假）
        self.last_data_source: str = PipelineSource.FAKE.value
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

                # select_spots 的 spots_provider 是 fn(city) 单参：这里用闭包
                # 注入天数联动的 limit + 必去景点强拉名单（LiveSpotsSource 支持）。
                def _source_with_limit(city: str):
                    return live_source(
                        city,
                        limit=self._pool_days_limit(),
                        ensure_spots=self._must_visit_names(),
                    )

                return select_spots(
                    self.requirement,
                    ask_user_on_conflict=ask,
                    spots_provider=_source_with_limit,
                )

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
                first_day_start_time=first_day_start_time,
                last_day_end_minutes=last_day_end_minutes,
            )
        return plan_multi_day(
            requirement,
            spots,
            restaurants=restaurants,
            first_day_start_time=first_day_start_time,
            last_day_end_minutes=last_day_end_minutes,
        )

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
        if self._use_live:
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