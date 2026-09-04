"""SkillRunner：技能层通用执行器（架构整理方案 §三 P5.6 形态 A，S3 落地）。

技能 = SkillSpec（``call_llm/skill_specs.py`` 注册表元数据）+ executor 实现
（模块级函数，注册表存 ``module:attr`` 点路径）。本模块提供执行与工具面：

- ``run_skill(name, params, ...)``——**程序化入口**：按注册表取技能、按点路径
  惰性导入 executor、按技能子工具面注入额度纪律，再调用 executor(ctx, params)；
- ``to_openai_skill_tools()`` / ``skill_tool_executor(...)``——**形态 A 工具面**：
  把技能作为 ``skill__<name>`` 条目放进外层 agent 的工具面，agent 自主决定
  调用哪个技能（executor 把 ``skill__x`` 转接到 ``run_skill``）。

executor 契约（ctx 鸭子类型，S3 定，后续技能遵守；首例见
``call_llm/planner_agent.intercity_verify_executor``）：

    ctx.tools              技能子工具名列表（注册表 tools）
    ctx.review_enabled     是否启用审查轮（P5.5；透传给 BaseClient review_schema）
    ctx.tool_executor      (name, arguments) → 结果；None = 门控关（不注入工具面）
    ctx.model_name / api_key / base_url / timeout / max_tool_rounds  模型参数

    executor(ctx, params) -> dict   （技能的结果对象，自定形状）

**门控**（与 PlannerAgent / decision_engine 同款）：注入 ``tool_provider``
**且** env ``USE_LLM_TOOLS`` 开启才注入工具面；默认关 → ``tool_executor=None``，
executor 行为 = 纯结构/纯文本回答（零额度、零回归）。**额度纪律**：工具执行
走 ``QuotaManager.cached_call``（同参命中缓存 = P5.5 护栏 1），per-mode 预算
从 ToolSpec 注册表 ``budget_default`` 按技能子工具面取值；``QuotaExceeded`` /
``LiveDataError`` 接成结构化 error 回填给模型（错误被回路消费，不静默兜底）。
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace
from typing import Any, Callable, Dict, List, Optional

from call_llm.decision_engine import _use_llm_tools
from call_llm.skill_specs import (
    SKILL_PREFIX,
    SKILL_SPECS,
    SkillSpec,
    get_skill_spec,
    skill_names,
)
from data_transmission.tool_specs import TOOL_SPECS


def _spec_mode_budget(spec: SkillSpec) -> Dict[str, int]:
    """技能子工具面的 per-mode 预算默认（取 ToolSpec.budget_default，None 不收录）。"""
    return {
        tool: TOOL_SPECS[tool].budget_default
        for tool in spec.tools
        if tool in TOOL_SPECS and TOOL_SPECS[tool].budget_default is not None
    }


def _build_tool_executor(tool_provider: Any, spec: SkillSpec) -> Callable[[str, Dict[str, Any]], Any]:
    """按技能子工具面建 (name, arguments) → 结果 的桥接。

    一次 ``run_skill`` 一个共享 cache dict（同参命中不耗额度不计数）；
    ``LiveDataError``（含 ``QuotaExceeded`` 超预算）接成结构化错误回填——
    BaseClient 会把 dict 序列化进 role=tool 消息，模型据此换思路/如实说明。
    """
    from data_transmission.live_errors import LiveDataError
    from data_transmission.quota_manager import make_quota_manager

    quota = make_quota_manager(
        tool_provider,
        mode_budget=_spec_mode_budget(spec),
        cache={},
    )

    def executor(name: str, arguments: Dict[str, Any]) -> Any:
        try:
            return quota.cached_call(name, **arguments)
        except LiveDataError as exc:
            return {
                "status": "error",
                "error": type(exc).__name__,  # quota_exceeded 等，机器可读
                "detail": f"{name}: {exc}",
            }

    return executor


def _resolve_executor(dotted: str) -> Callable[..., Any]:
    """惰性解析 executor 点路径（module:attr）——避免模块级互相 import。"""
    module, _, attr = dotted.partition(":")
    if not module or not attr:
        raise ValueError(f"executor 需为 module:attr 点路径，实际: {dotted!r}")
    executor = getattr(importlib.import_module(module), attr)
    if not callable(executor):
        raise TypeError(f"executor 目标不可调用: {dotted!r}")
    return executor


def run_skill(
    name: str,
    params: Optional[Dict[str, Any]] = None,
    *,
    tool_provider: Any = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 60,
    max_tool_rounds: int = 5,
) -> Dict[str, Any]:
    """执行一个已注册技能（P5.6 形态 A 程序化入口）。

    ``params`` 按注册表 ``params_schema`` 传入（由 executor 自行校验/兜底）。
    返回 executor 的结果 dict（自定形状）；门控关时 executor 不注入工具面。
    未注册技能抛 ``KeyError``（失败要显眼，不做静默降级）。
    """
    spec = get_skill_spec(name)
    if spec is None:
        raise KeyError(f"未注册技能: {name}（已注册: {skill_names()}）")
    executor = _resolve_executor(spec.executor)  # executor 必填（守卫已保证）
    ctx = SimpleNamespace(
        spec=spec,
        tools=list(spec.tools),
        review_enabled=spec.review_enabled,
        tool_executor=(
            _build_tool_executor(tool_provider, spec)
            if tool_provider is not None and _use_llm_tools()
            else None
        ),
        tool_provider=tool_provider,
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        max_tool_rounds=max_tool_rounds,
    )
    return executor(ctx, params or {})


# ---------------------------------------------------------------------------
# 形态 A 工具面：技能作为外层 agent 可自主调用的具名能力
# ---------------------------------------------------------------------------


def to_openai_skill_tools(
    names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """技能注册表 → OpenAI tools 形态（外层 agent 工具面上的 ``skill__<name>``）。

    ``names`` 缺省 = 全部已注册技能。每个技能条目 = 普通 function tool，
    executor 侧由 ``skill_tool_executor`` 转接到 ``run_skill``。
    """
    selected = [n for n in (names or list(SKILL_SPECS)) if n in SKILL_SPECS]
    tools: List[Dict[str, Any]] = []
    for skill in sorted(selected):
        spec = SKILL_SPECS[skill]
        tools.append({
            "type": "function",
            "function": {
                "name": f"{SKILL_PREFIX}{spec.name}",
                "description": (
                    f"{spec.description}。何时用：{spec.when_to_use}"
                ),
                "parameters": spec.params_schema,
            },
        })
    return tools


def skill_tool_executor(
    *,
    tool_provider: Any = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 60,
    max_tool_rounds: int = 5,
) -> Callable[[str, Dict[str, Any]], Any]:
    """形态 A executor：把 BaseClient 工具回路的 ``skill__<name>`` 调用转接
    ``run_skill``——外层 agent 自主决定调哪个技能（比调扁平真源更高一层的
    可解释决策）。门控语义由 ``run_skill`` 统一承担（默认关零回归）。
    """
    def executor(name: str, arguments: Dict[str, Any]) -> Any:
        skill = name[len(SKILL_PREFIX):] if name.startswith(SKILL_PREFIX) else name
        return run_skill(
            skill,
            arguments,
            tool_provider=tool_provider,
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            max_tool_rounds=max_tool_rounds,
        )

    return executor
