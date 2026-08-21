"""A 侧接入预留接口。

当前 Demo 使用 B 自带的确定性 stub（decision/decision_engine.py）作为 decision_hook。
A 侧正式接入时，只需实现 ADecisionEngine 并替换 build_decision_hook() 的返回对象，
B/Django 侧代码无需改动。
"""

from __future__ import annotations

from typing import Any, Optional

from core.schemas import DecisionRequest, ReplanRequest
from decision.decision_engine import DecisionEngine


class ADecisionEngine:
    """A 侧 Decision Engine 必须实现的接口。

    A 的 LLM 版本可参考：
        class MyLLMDecisionEngine(ADecisionEngine):
            def __init__(self, tool_provider=None):
                self.tool_provider = tool_provider
                self.llm = ...

            def __call__(self, req: DecisionRequest) -> Optional[ReplanRequest]:
                # 1. 从 req.context["tool_specs"] 看到可用工具
                # 2. 通过 self.tool_provider.call_json(...) 调用工具
                # 3. 返回 ReplanRequest（或 None 表示不重规划）
                ...
    """

    def __call__(self, req: DecisionRequest) -> Optional[ReplanRequest]:
        raise NotImplementedError


class PlaceholderDecisionEngine(ADecisionEngine):
    """Demo 占位实现：委托给 B 现有的确定性 stub，保证闭环可跑。

    A 正式接入时替换这个类即可。
    """

    def __init__(self, impact_threshold: float = 50.0) -> None:
        self._engine = DecisionEngine(impact_threshold=impact_threshold)

    def __call__(self, req: DecisionRequest) -> Optional[ReplanRequest]:
        return self._engine(req)


def build_decision_hook(tool_provider: Any = None) -> Any:
    """返回当前可用的 decision_hook。

    ``tool_provider`` 由 AgentRuntime 注入，A 的 LLM 版本可直接使用。

    A 接入时把这里改成：
        return MyLLMDecisionEngine(tool_provider=tool_provider)
    """
    # Demo 阶段：tool_provider 暂时不使用，B 的 stub 直接返回确定性结果。
    return PlaceholderDecisionEngine()
