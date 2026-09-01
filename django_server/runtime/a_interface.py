"""A 侧接入接口（AB 合码方案 §三.3，README §4.1 承诺的注入点）。

合码后：
- ``build_decision_hook`` 返回 A 的 ``call_llm.b_decision_hook.BDecisionHook``
  （LLM 影响评分 + closed/hotel 硬规则 + RePlanner，见 A 侧实现 docstring）；
- 新增 ``build_planner_hook(requirement)`` 返回 A 的
  ``call_llm.b_planner_hook.BPlannerHook``（requirement → TripTimeline，离线可跑）；
- 新增 ``build_chat_hook(tool_provider)`` 返回 A 的
  ``call_llm.b_chat_hook.BChatHook``（P5.1：chat 修改意图 → ReplanRequest，
  ``update_timeline`` 的编排从 B 侧 view 迁回 A 编排层，见方案 §六.7）。

B/Django 其余代码零改动。风险对策（方案 §八）：
- **循环导入**：``agent_runtime`` → ``a_interface`` 已有依赖边，本模块内对
  runtime 单例与 A 侧模块一律**函数级延迟导入**（反向边必须延迟）；
- **requirement 生命周期**：``build_decision_hook`` 构造时从 runtime 单例读
  ``requirement``（``POST /api/plan/`` 提交时由 ``AgentRuntime`` 存入），
  因此在 ``init_timeline`` 之前调用方必须先设置 ``runtime.requirement``。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from core.schemas import DecisionRequest, ReplanRequest

DEFAULT_PLAN_ID = "plan_001"


def _requirement_from_runtime() -> Dict[str, Any]:
    """从 runtime 单例读取当前 requirement（函数内延迟导入防循环导入）。"""
    from runtime.agent_runtime import runtime

    requirement = getattr(runtime, "requirement", None)
    return requirement if isinstance(requirement, dict) else {}


def _content(requirement: Dict[str, Any]) -> Dict[str, Any]:
    content = (requirement or {}).get("content")
    return content if isinstance(content, dict) else {}


def _candidate_spots_provider(requirement: Dict[str, Any]):
    """BDecisionHook 的候选池提供器：A 的 select_spots（冲突询问关）。

    规划层候选池与执行期一致，保证 replan 在 B 进程内可完整运行。
    """

    def provider(_city: str) -> Any:
        from algorithoms.select_spots import select_spots

        return select_spots(requirement, ask_user_on_conflict=False)

    return provider


def build_decision_hook(tool_provider: Any = None) -> Any:
    """返回 A 侧 Decision Engine（BDecisionHook）。

    ``tool_provider`` 由 AgentRuntime 注入（执行期实时工具门面）；requirement
    从 runtime 单例读取（调用方须先设置，见模块 docstring）。
    """
    from call_llm.b_decision_hook import BDecisionHook

    requirement = _requirement_from_runtime()
    content = _content(requirement)
    return BDecisionHook(
        requirement=requirement,
        start_date=content.get("start_date"),
        city=str(content.get("destination") or ""),
        plan_id=DEFAULT_PLAN_ID,
        candidate_spots_provider=_candidate_spots_provider(requirement),
        tool_provider=tool_provider,   # 8.30 酒店真源：重规划换宿用 RollingGo 真源候选
    )


def build_planner_hook(
    requirement: Optional[Dict[str, Any]] = None,
    tool_provider: Any = None,
) -> Any:
    """A 侧 Planner（BPlannerHook）：requirement → TripTimeline（离线可跑）。

    ``requirement`` 缺省从 runtime 单例读取；``tool_provider`` 由 AgentRuntime
    注入并**透传给 BPlannerHook**（方案 §4.3 B1：实时数据接入时规划层用真源；
    修复 0825：此前签名收参但未透传，导致服务器上 USE_LIVE_DATA 即使为 true
    也永远走假源）。
    """
    from call_llm.b_planner_hook import BPlannerHook

    if requirement is None:
        requirement = _requirement_from_runtime()
    content = _content(requirement)
    return BPlannerHook(
        requirement=requirement,
        city=str(content.get("destination") or ""),
        start_date=content.get("start_date"),
        plan_id=DEFAULT_PLAN_ID,
        ask_user_on_conflict=False,
        tool_provider=tool_provider,   # 真源 provider（USE_LIVE_DATA=1 时 BPlannerHook 内部启用）
    )


def build_chat_hook(tool_provider: Any = None) -> Any:
    """A 侧对话编排入口（BChatHook，P5.1）：chat 修改意图 → ``ReplanRequest``。

    方案 §六.7：``update_timeline`` 的编排从 B 侧 view 迁回 A 编排层——模型
    输出「修改意图」而非整份新时间轴，A 翻译成事件/约束后走 RePlanner 增量
    修复或 A 规划器全量重排；C 端请求/响应契约零变化。B 侧 ``_exec_chat_timeline``
    只做「解析 arguments → build_chat_hook() → apply() → 应用 ReplanRequest」。
    requirement 从 runtime 单例读取（与 ``build_decision_hook`` 同款生命周期约定）。
    """
    from call_llm.b_chat_hook import BChatHook

    requirement = _requirement_from_runtime()
    content = _content(requirement)
    return BChatHook(
        requirement=requirement,
        spots_provider=_candidate_spots_provider(requirement),
        start_date=content.get("start_date"),
        city=str(content.get("destination") or ""),
        plan_id=DEFAULT_PLAN_ID,
        tool_provider=tool_provider,   # add/reschedule 全量与增量修复的真源门面
    )