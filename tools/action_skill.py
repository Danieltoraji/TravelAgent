"""动作技能基类：两段式 prepare/commit（P3，见 docs/tool_encapsulation_design_20260828.md §4）。

与查询技能（query-skill）的区别：
- ``safety="action"`` / ``readonly=False``——天然不进 LLM 与只读白名单；
- **两段式**：``prepare`` 幂等组装预订意图（纯计算，无真实副作用，任何调用方
  可发起）；``commit`` 是真实副作用，**唯一合法触发方是批准链路**
  （ActionQueue approve → 执行器注册表），本基类默认拒绝直接调用；
- 支付恒 MANUAL（"Agent 不代付"安全边界，README §安全边界）。
"""

from __future__ import annotations

from typing import Any, Dict

from tools.skill import Skill


class ActionSkill(Skill):
    """动作技能基类。子类实现 ``prepare`` 组装意图；``commit`` 由子类按
    领域能力覆盖（有真实下单通道时），默认拒绝直调。"""

    safety = "action"
    readonly = False

    def prepare(self, **kwargs: Any) -> Dict[str, Any]:
        """组装预订意图（幂等、无副作用）。子类必须实现。"""
        raise NotImplementedError("ActionSkill 子类必须实现 prepare()")

    def commit(self, **kwargs: Any) -> Any:
        """真实副作用——仅批准链路（execute_action / approve）可触发。

        有真实下单通道的子类覆盖本方法；未覆盖时调用即拒绝。
        """
        raise RuntimeError(
            f"{self.name}.commit 需经批准链路（ActionQueue approve → 执行器）"
            "执行；直接调用被拒绝。支付恒为 MANUAL（Agent 不代付）。"
        )

    def _run(self, action: str = "prepare", **kwargs: Any) -> Dict[str, Any]:
        if action == "prepare":
            return self.prepare(**kwargs)
        if action == "commit":
            # 直调 commit 同样被基类/子类守卫拦截（真实提交仅批准链路触发），
            # 子类的具体拒绝原因（如"12306 无购票 API"）在此透出
            result = self.commit(**kwargs)
            return result if isinstance(result, dict) else {"committed": bool(result)}
        raise ValueError(f"未知 action: {action}（可选 prepare/commit）")

    def _validate_kwargs(self, kwargs: dict) -> None:
        """C5 校验仅在 prepare 时生效。

        commit / 未知 action 的参数集与 prepare 不同（如 commit 无需
        hotel_name），基类按 prepare 的 required 校验会在分派前误拦。
        非 prepare 动作跳过校验，由 ``_run``/``commit`` 自行报错。
        """
        if kwargs.get("action", "prepare") != "prepare":
            return
        super()._validate_kwargs(kwargs)
