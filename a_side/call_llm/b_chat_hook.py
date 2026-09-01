"""B 契约的 ``chat_hook``：A 侧对话编排入口（P5.1，chat 线回 A）。

方案 §六.7：chat v2.2 编排迁回 A 编排层，``update_timeline`` 从「模型输出
整份新时间轴整体替换」改为「模型输出修改意图 → A 翻译成事件/约束 →
RePlanner 增量修复 / A 规划器全量重排」；C 端请求/响应契约零变化，仅内部
链路收敛，B 侧 ``views.py`` 改动由 A 侧提供补丁经 a_side 同步。

本模块提供开箱即用的 ``BChatHook``（与 ``BDecisionHook`` 并列的可调用/编排
对象）：

- 消费模型输出的「修改意图」（``{intents: [{action, spot, day?, time?}]}``，
  见 ``chat_intents.CHAT_INTENTS_SCHEMA``）
- 翻译成 A 侧事件 / 约束（``translate_chat_intents``）后分路执行：
  - ``remove``    → ``closed`` 事件 → RePlanner ``replan`` 增量修复（现成能力）
  - ``add``       → requirement 约束修改（must_visit 追加）→ 全量重排
    （``BPlannerHook`` 管线，含城际/住宿/餐饮回灌）
  - ``reschedule``→ 在 A 计划 dict 上做时段锚点替换
    （RePlanner 无「时段约束事件」翻译，v1 由编排层直接落位；深度可行性
    校验由 B 侧 validator 在最终时间轴上执行）
- 汇总成 ``ReplanRequest`` 返回（含 Explainable 的 reason/diff_summary），
  B 侧落地层（``agent.apply_replan`` + 监控重建 + 记录）不变

``replan_fn`` / ``spots_provider`` / ``planner`` 均可注入，便于离线测试与
替换实现；缺省走 A 侧真实模块（与 ``BDecisionHook`` 同款注入面）。

**类身份约定**：与 ``data_transmission/b_contract.py`` 一致，契约导入用
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

from core.schemas import ReplanRequest  # noqa: E402
from data_transmission.b_contract import (  # noqa: E402
    plan_to_trip_timeline,
    replan_result_to_replan_request,
    trip_timeline_to_plan,
)
from call_llm.chat_intents import (  # noqa: E402
    CHAT_INTENTS_SCHEMA,
    apply_reschedule_ops,
    translate_chat_intents,
)


def _planner_default(
    requirement: Dict[str, Any],
    *,
    city: Optional[str] = None,
    start_date: Any = None,
    plan_id: str = "",
    tool_provider: Any = None,
):
    """默认 A 侧规划器：BPlannerHook（延迟导入防循环）。

    返回 ``callable(requirement) -> TripTimeline``：按传入 requirement 全量重排。
    每次调用新建 hook（requirement 已含约束修改），保证不加缓存残留。
    """

    def plan(req: Dict[str, Any]) -> Any:
        from call_llm.b_planner_hook import BPlannerHook

        hook = BPlannerHook(
            requirement=req,
            city=city or "",
            start_date=start_date,
            plan_id=plan_id,
            ask_user_on_conflict=False,
            tool_provider=tool_provider,
        )
        return hook.generate_timeline(regenerate=True)

    return plan


class BChatHook:
    """A 侧对话编排入口：修改意图 → ReplanRequest（P5.1）。"""

    def __init__(
        self,
        requirement: Dict[str, Any],
        *,
        replan_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        spots_provider: Optional[Callable[[str], Any]] = None,
        planner: Optional[Callable[[], Any]] = None,
        start_date: Any = None,
        city: Optional[str] = None,
        plan_id: str = "",
        tool_provider: Any = None,
    ) -> None:
        """构造对话编排钩子。

        - ``replan_fn``  增量修复实现（remove 意图用）；缺省走 ``replanner.replan``
        - ``spots_provider``  候选池提供器（remove 意图增量修复用）
        - ``planner``    全量重排实现（add 意图用）：``callable(requirement) -> 
          TripTimeline``；缺省懒构造 ``BPlannerHook`` 管线（requirement 取变更后副本）
        - ``tool_provider``  真源工具门面（透传规划器/增量修复，酒店换宿等）
        """
        self.requirement = requirement or {}
        self._replan_fn = replan_fn
        self._spots_provider = spots_provider
        self._planner = planner
        self.start_date = start_date
        self.city = city
        self.plan_id = plan_id
        self.tool_provider = tool_provider
        self._planner_cache: Optional[Callable[[], Any]] = None

    # -- 内部 --------------------------------------------------------------

    def _effective_planner(self, requirement: Dict[str, Any]) -> Callable[[], Any]:
        """返回针对 ``requirement`` 的全量重排可调用（缓存首个，避免重复构造）。"""
        if self._planner is not None:
            return self._planner
        if self._planner_cache is None:
            self._planner_cache = _planner_default(
                requirement,
                city=self.city,
                start_date=self.start_date,
                plan_id=self.plan_id,
                tool_provider=self.tool_provider,
            )
        return self._planner_cache

    def _replan_events(
        self,
        requirement: Dict[str, Any],
        current_plan: Dict[str, Any],
        events: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """remove 意图的增量修复：翻译后的事件喂 ``replan``（复用 BDecisionHook 链）。"""
        from algorithoms.replanner import replan as replan_fn_default

        if self._replan_fn is not None:
            return self._replan_fn(requirement, current_plan, events)
        spots = None
        if self._spots_provider is not None:
            spots = self._spots_provider(self.city)
        return replan_fn_default(
            requirement,
            current_plan,
            [list(g) for g in (spots or [[], [], []])],
            events,
            hotel_provider=self._live_hotel_provider_or_none(),
        )

    def _live_hotel_provider_or_none(self) -> Optional[Any]:
        if self.tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                return make_live_hotel_provider(self.tool_provider)
            except Exception:  # noqa: BLE001
                return None
        return None

    # -- 主入口 ------------------------------------------------------------

    def apply(
        self,
        intents: Sequence[Dict[str, Any]],
        current_timeline: Any = None,
    ) -> ReplanRequest:
        """对话修改意图 → ``ReplanRequest``。

        ``current_timeline``（B 侧 ``TripTimeline``）为 None 时仅能处理 add
        （全量重排不需要当前时间轴）；remove/reschedule 需要它作为增量基底。
        意图翻译错误（缺 time/spot、时段越界、目标天不存在、景点不存在）以
        ``reason`` 与 ``diff_summary`` 透出错误，由 B 侧回填 LLM 调整重试。
        """
        translated = translate_chat_intents(list(intents or []), self.requirement)
        notes = list(translated["notes"])
        errors = list(translated["errors"])

        current_plan: Optional[Dict[str, Any]] = None
        if current_timeline is not None:
            try:
                current_plan = trip_timeline_to_plan(current_timeline)
            except Exception:  # noqa: BLE001  解析失败按无基底处理
                errors.append("当前时间轴解析失败")

        final_timeline: Optional[Any] = None
        diff_hints: List[str] = []
        need_replan = bool(translated["events"] or translated["requirement_override"]
                           or translated["reschedule_ops"])

        # 1) reschedule：时段锚点替换（在 A 计划 dict 上就地落位）
        if translated["reschedule_ops"]:
            if current_plan is None:
                errors.append("reschedule 需要当前时间轴")
            else:
                new_plan, op_errors = apply_reschedule_ops(
                    current_plan, translated["reschedule_ops"]
                )
                if op_errors:
                    errors.extend(op_errors)
                else:
                    current_plan = new_plan
                    diff_hints.append(
                        "调整了 "
                        + "、".join(
                            str(op.get("spot")) for op in translated["reschedule_ops"]
                        )
                        + " 的到达时段"
                    )

        # 2) remove：closed 事件 → RePlanner 增量修复
        if translated["events"] and not errors:
            if current_plan is None:
                errors.append("remove 需要当前时间轴")
            else:
                replan_failed = False
                try:
                    result = self._replan_events(
                        self.requirement, current_plan, translated["events"]
                    )
                except Exception as exc:  # noqa: BLE001  增量修复失败降级全量
                    notes.append(f"增量修复失败（{exc}），降级全量重排")
                    result = {}
                    replan_failed = True
                if isinstance(result, dict) and result.get("new_plan"):
                    current_plan = result["new_plan"]
                    from data_transmission.b_contract import changes_to_diff_summary

                    diff_hints.extend(changes_to_diff_summary(result.get("changes") or []))
                    notes.extend(result.get("notes") or [])
                elif replan_failed:
                    # 真正走一次全量重排作为 remove 的兜底（结果成为最终时间轴）
                    try:
                        planner = self._effective_planner(self.requirement)
                        final_timeline = planner(self.requirement)
                    except Exception as exc2:  # noqa: BLE001
                        errors.append(f"全量重排失败：{exc2}")

        if errors:
            return ReplanRequest(
                new_timeline=None,
                reason="修改意图无法应用：" + "；".join(errors[:5]),
                diff_summary=[],
                need_replan=False,
                impact=0.0,
                affected_spots=[],
            )

        # 3) add：约束修改（must_visit 追加）→ 全量重排
        if translated["requirement_override"] is not None:
            eff_req = translated["requirement_override"]
            planner = self._effective_planner(eff_req)
            try:
                final_timeline = planner(eff_req)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"全量重排失败：{exc}")
        elif final_timeline is None and current_plan is not None:
            final_timeline = plan_to_trip_timeline(
                current_plan,
                city=self.city or str(self.requirement.get("destination") or ""),
                start_date=self.start_date,
                plan_id=self.plan_id,
            )

        if errors:
            return ReplanRequest(
                new_timeline=None,
                reason="修改意图无法应用：" + "；".join(errors[:5]),
                diff_summary=[],
                need_replan=False,
                impact=0.0,
                affected_spots=[],
            )
        if final_timeline is None:
            return ReplanRequest(
                new_timeline=None,
                reason="没有可应用的修改，未变更行程。",
                diff_summary=[],
                need_replan=False,
                impact=0.0,
                affected_spots=[],
            )

        reason = "对话调整：" + ("；".join(diff_hints) if diff_hints else "已按对话要求调整行程")
        if notes:
            reason += "（" + "；".join(notes[:3]) + "）"
        return ReplanRequest(
            new_timeline=final_timeline,
            reason=reason,
            diff_summary=diff_hints,
            need_replan=True,
            impact=0.0,
            affected_spots=list(
                dict.fromkeys(
                    str(s) for s in translated["reschedule_ops"]  # type: ignore[union-attr]
                )
            ),
        )