"""B 契约的 ``decision_hook``：A 侧 Decision Engine 对 B 的注入点实现。

README §4.1②：A 的决策引擎只需是**可调用对象**
``__call__(req: DecisionRequest) -> Optional[ReplanRequest]``，
B 侧代码零改动。本模块提供开箱即用的 ``BDecisionHook``：

- 消费 B 的 ``DecisionRequest``（内部把 ``MonitorEvent`` 翻译成 A 的事件格式）
- 调用 A 现有的 ``decide_replan``（LLM 影响评分 + closed/hotel 硬规则）判触发
- 未触发 → 返回 ``None``（B 侧视为忽略）；触发 → 运行 A 的 ``replan``
  并把结果包装成 ``ReplanRequest`` 返回（含 Explainable 的 reason/diff_summary）
- ``decision_fn`` / ``replan_fn`` / ``candidate_spots_provider`` 均可注入，
  便于离线测试与替换实现；缺省走 A 侧真实模块

联调接入示例（替换 B 仓库 django_server/runtime/a_interface.py 的 build_decision_hook）：

.. code-block:: python

    from call_llm.b_decision_hook import BDecisionHook

    def build_decision_hook(tool_provider=None):
        return BDecisionHook(
            requirement=requirement,          # A 侧结构化需求（Requirement）
            start_date="2026-08-21",
            candidate_spots_provider=load_candidate_spots,  # 返回 select_spots 结果
        )

**类身份约定**：契约导入与 ``data_transmission/b_contract.py`` 一致，全部用
顶层 ``import core.schemas``，保证 B 进程内 ``isinstance(replan, ReplanRequest)``
成立（详见 ``data_transmission/b_contract.py`` 模块 docstring）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

_REPO_ROOT = str(Path(__file__).resolve().parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from core.schemas import DecisionRequest, ReplanRequest  # noqa: E402
from data_transmission.b_contract import (  # noqa: E402
    monitor_events_to_a_events,
    replan_result_to_replan_request,
    trip_timeline_to_plan,
)
from data_transmission.decision import DECISION_THRESHOLD  # noqa: E402


class BDecisionHook:
    """A 侧 Decision Engine 的 B 契约实现（可调用对象）。"""

    def __init__(
        self,
        requirement: Dict[str, Any],
        *,
        decision_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        replan_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        candidate_spots_provider: Optional[Callable[[str], Any]] = None,
        start_date: Any = None,
        city: Optional[str] = None,
        plan_id: str = "",
        threshold: Optional[int] = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        tool_provider: Optional[Any] = None,
    ) -> None:
        """构造决策钩子。

        参数：
            requirement: A 侧结构化需求（含 ``content`` 键，与 A 现有管线一致）
            decision_fn: 决策函数 ``fn(requirement, events, *, threshold=None)``，
                缺省用 ``call_llm.decision_engine.decide_replan``
            replan_fn: 重规划函数 ``fn(requirement, current_plan, spots, events)``，
                缺省用 ``algorithoms.replanner.replan``
            candidate_spots_provider: ``fn(city) -> candidate_spots``，缺省用
                ``algorithoms.select_spots.select_spots`` 重新挑选；为 None 时
                只决策不重规划（返回无新时间轴的 ReplanRequest）
            start_date: 行程开始日期（str/datetime/date），缺省取需求里的 start_date
            city: 行程城市，缺省取需求的 destination
            plan_id: 产出 TripTimeline 的计划 ID
            threshold: 触发阈值，缺省优先取 ``DecisionRequest.context["impact_threshold"]``，
                再退到 DECISION_THRESHOLD（40）
            model_name/api_key/base_url: 传给决策 LLM 调用的参数（测试/联调用）
            tool_provider（8.30 酒店真源）: 执行期工具门面；非 None 时重规划注入
                真源酒店 provider（RollingGo MCP → 满房换宿用真源候选），失败由
                ``HotelSelector`` 内部回退假池
        """
        self.requirement = requirement if isinstance(requirement, dict) else {}
        content = self.requirement.get("content") or {}
        if not isinstance(content, dict):
            content = {}
        self._decision_fn = decision_fn
        self._replan_fn = replan_fn
        self._spots_provider = candidate_spots_provider
        self.city = city or str(content.get("destination") or "")
        self.plan_id = plan_id
        self.threshold = threshold
        self._model_name = model_name
        self._api_key = api_key
        self._base_url = base_url
        self.tool_provider = tool_provider
        if start_date is None:
            start_date = content.get("start_date")
        self.start_date = start_date
        # A 侧内部计划：首次重规划时由 current_timeline 逆向重建，此后以本值为主
        self._current_plan: Optional[Dict[str, Any]] = None

    # -- 内部 --------------------------------------------------------------

    def _effective_threshold(self, req: DecisionRequest) -> int:
        if self.threshold is not None:
            return max(1, int(self.threshold))
        ctx = req.context if isinstance(req.context, dict) else {}
        ctx_threshold = ctx.get("impact_threshold")
        if ctx_threshold is not None:
            try:
                return max(1, int(ctx_threshold))
            except (TypeError, ValueError):
                pass
        return DECISION_THRESHOLD

    def _decide(
        self,
        requirement: Dict[str, Any],
        events: Sequence[Dict[str, Any]],
        threshold: int,
    ) -> Dict[str, Any]:
        if self._decision_fn is not None:
            fn = self._decision_fn
            try:
                return fn(requirement, events, threshold=threshold)
            except TypeError:
                return fn(requirement, events)
        from call_llm.decision_engine import decide_replan

        return decide_replan(
            requirement,
            events,
            model_name=self._model_name,
            api_key=self._api_key,
            base_url=self._base_url,
            threshold=threshold,
            tool_provider=self.tool_provider,   # P4：USE_LLM_TOOLS 开启时供决策 LLM 查工具
        )

    def _replan(
        self,
        requirement: Dict[str, Any],
        current_plan: Dict[str, Any],
        spots: Any,
        events: Sequence[Dict[str, Any]],
        hotel_provider: Optional[Callable[[str], Any]] = None,
    ) -> Dict[str, Any]:
        if self._replan_fn is not None:
            # 8.30 酒店真源：外部注入的 replan_fn 若不接受 hotel_provider 则回退旧签名
            try:
                return self._replan_fn(
                    requirement, current_plan, spots, events,
                    hotel_provider=hotel_provider,
                )
            except TypeError:
                return self._replan_fn(requirement, current_plan, spots, events)
        from algorithoms.replanner import replan

        return replan(
            requirement, current_plan, spots, events,
            hotel_provider=hotel_provider,
        )

    def _live_hotel_provider_or_none(self) -> Optional[Any]:
        """真源酒店 provider（供重规划换宿注入）；未注入工具 → None。

        仅构造函数，执行失败由 ``HotelSelector`` 内部回退假池。
        """
        if self.tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                return make_live_hotel_provider(self.tool_provider)
            except Exception:  # noqa: BLE001
                return None
        return None

    # -- B 契约入口 --------------------------------------------------------

    def __call__(self, req: DecisionRequest) -> Optional[ReplanRequest]:
        """B 的 decision_hook 入口：返回 ``ReplanRequest``（触发）或 ``None``（忽略）。"""
        if req is None:
            return None
        events = req.events if isinstance(req.events, (list, tuple)) else []
        threshold = self._effective_threshold(req)
        a_events = monitor_events_to_a_events(events, impact_threshold=threshold)
        if not a_events:
            return None

        decision = self._decide(self.requirement, a_events, threshold)
        if not (isinstance(decision, dict) and decision.get("triggered")):
            return None

        try:
            score = max(0, min(100, int(decision.get("score", 0) or 0)))
        except (TypeError, ValueError):
            score = 0
        raw_reasons = decision.get("reasons") or []
        if isinstance(raw_reasons, (list, tuple)):
            reasons = [str(x) for x in raw_reasons]
        else:
            reasons = []
        reason = "；".join(reasons) or f"影响分 {score} 达到阈值 {threshold}"
        impact = score / 100.0

        # 只决策、不重规划（未提供候选池提供器）：返回无时间轴的重规划请求，
        # B 侧 apply_replan 会记录原因并保留原时间轴（见 execution_agent.apply_replan）。
        if self._spots_provider is None:
            return ReplanRequest(
                new_timeline=None,
                reason=reason,
                diff_summary=[],
                need_replan=True,
                impact=impact,
                affected_spots=[],
            )

        current_plan = self._current_plan
        if current_plan is None:
            current_plan = trip_timeline_to_plan(req.current_timeline)
        try:
            spots = self._spots_provider(self.city)
        except Exception:  # noqa: BLE001  候选池加载失败退化为只决策
            spots = None
        if spots is None:
            return ReplanRequest(
                new_timeline=None,
                reason=reason,
                diff_summary=[],
                need_replan=True,
                impact=impact,
                affected_spots=[],
            )

        result = self._replan(
            self.requirement,
            current_plan,
            spots,
            a_events,
            hotel_provider=self._live_hotel_provider_or_none(),
        )
        if isinstance(result, dict) and result.get("new_plan"):
            self._current_plan = result["new_plan"]
        return replan_result_to_replan_request(
            result if isinstance(result, dict) else {},
            city=self.city,
            start_date=self.start_date,
            plan_id=self.plan_id,
            reason_prefix=reason,
            impact=impact,
        )