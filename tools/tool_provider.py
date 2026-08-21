"""ToolProvider：给 A 侧 LLM 提供“可直接调用工具”的进程内接口。

设计：
- 只暴露工具元数据（ToolSpec），不暴露 ToolRegistry 内部实现；
- 默认只允许调用只读工具，避免 LLM 直接触发预约等副作用操作；
- ``call_json()`` 是 LLM 友好的入口：接收 JSON 风格参数字典，返回 JSON 可序列化 dict。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Set

from core.schemas import ToolResult, ToolSpec, to_dict
from tools.base_tool import ToolRegistry


class ToolProvider:
    """A 侧 LLM 可用的工具门面（Facade）。

    - ``list_tools()``  返回工具清单（name / description / input_schema）
    - ``call()``        按工具名调用，返回 ToolResult
    - ``call_json()``   按工具名 + 参数字典调用，返回纯 dict（供 JSON 序列化）
    """

    def __init__(self, registry: ToolRegistry,
                 allowlist: Optional[Set[str]] = None) -> None:
        self._registry = registry
        if allowlist is None:
            # 默认白名单 = 所有只读工具；有副作用工具（如 booking）不暴露给 LLM
            allowlist = {
                spec.name for spec in registry.list_specs() if spec.readonly
            }
        self._allowlist = set(allowlist)

    # -- 元数据 -----------------------------------------------------------

    def list_tools(self) -> List[ToolSpec]:
        """返回 LLM 可见的工具元数据列表（按名称排序）。"""
        return self._registry.list_specs(self._allowlist)

    def list_tools_json(self) -> List[Dict[str, Any]]:
        """返回 LLM 可见工具元数据的 JSON 可序列化列表。"""
        return [to_dict(spec) for spec in self.list_tools()]

    def get_tool(self, name: str) -> ToolSpec:
        """获取单个工具元数据；不在白名单内则抛 KeyError。"""
        self._check_allowed(name)
        return self._registry.get_spec(name)

    # -- 调用 -------------------------------------------------------------

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        """调用工具，返回统一 ToolResult。"""
        self._check_allowed(name)
        return self._registry.call(name, **kwargs)

    def call_json(self, name: str, arguments: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """LLM 友好入口：``arguments`` 为参数字典，返回 ToolResult 的 dict。"""
        result = self.call(name, **(arguments or {}))
        return result.to_dict()

    # -- 内部 -------------------------------------------------------------

    def _check_allowed(self, name: str) -> None:
        if name not in self._allowlist:
            raise KeyError(
                f"Tool '{name}' is not exposed to A-side LLM. "
                f"Allowed: {sorted(self._allowlist)}"
            )
