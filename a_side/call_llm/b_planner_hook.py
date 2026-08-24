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

import sys
from pathlib import Path
from typing import Any, Callable, Dict, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.schemas import PlannerOutput, TripTimeline  # noqa: E402
from data_transmission.b_contract import (  # noqa: E402
    _as_date,
    plan_to_trip_timeline,
    requirement_to_planner_output,
)


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
    ) -> Dict[str, Any]:
        if self._planner_fn is not None:
            # 自定义 planner_fn 保持原契约 (requirement, spots)；真源接线由注入方负责
            return self._planner_fn(requirement, spots)
        from algorithoms.planner import plan_multi_day

        if travel_time_provider is not None:
            return plan_multi_day(
                requirement, spots, travel_time_provider=travel_time_provider
            )
        return plan_multi_day(requirement, spots)

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
        return self._run_pipeline(self._spots_provider, None, source="fake")

    def _generate_live_or_fallback(self) -> TripTimeline:
        """真源优先：候选池 / 规划任一步失败 → 回退假数据管线（保留失败原因）。"""
        from data_transmission.live_data import _coord_str, make_live_matrix_fn

        try:
            spots = self._live_spots_provider(self.city)
        except Exception as exc:  # noqa: BLE001
            reason = f"真实数据接入失败，已回退假数据：{exc}"
            timeline = self._run_pipeline(self._spots_provider, None, source="fake")
            self.last_data_source = "live_fallback"
            self.last_error = reason
            return timeline
        try:
            if self._travel_time_provider is not None:
                self._travel_time_provider.set_name_map(
                    self._live_spots_source.names
                )
                # B 档（8.25）：scenic 已返回真实坐标 → 一次 batch_route 取整矩阵。
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
                if name_to_coord:
                    matrix = make_live_matrix_fn(self._tool_provider, city=self.city)(
                        name_to_coord
                    )
                    self._travel_time_provider.set_matrix(
                        matrix, name_to_coord=name_to_coord
                    )
            plan = self._planner(
                self.requirement,
                spots,
                travel_time_provider=self._travel_time_provider,
            )
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
        timeline = plan_to_trip_timeline(
            plan,
            city=self.city,
            start_date=self.start_date,
            plan_id=self.plan_id,
        )
        self._current_timeline = timeline
        return timeline

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