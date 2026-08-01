"""统一工具抽象层：BaseTool 抽象基类 + ToolRegistry 注册表。

设计（对应《任务整理.md》模块3 Tool Agents）：
- 每个领域 Tool 只负责自己的 API；
- 所有 Tool 返回统一的 ToolResult 契约，由上层（Execution Agent / 外部调用方）组合编排；
- 真实 API 与 Mock 实现遵循同一签名，切换时调用方零改动。
"""

from __future__ import annotations

import abc
import time
from typing import Any, ClassVar

from core.schemas import ToolResult, ToolStatus


class BaseTool(abc.ABC):
    """所有领域 Tool 的基类。

    子类必须实现：
      - name        工具唯一名（注册键）
      - description 工具说明（供上层 / LLM 理解）
      - input_schema 入参说明（JSON Schema 风格，供上层校验与展示）
      - _run(**kwargs) 真正的执行逻辑
    """

    name: str = "base"
    description: str = "Base tool"
    source: str = "mock"
    input_schema: ClassVar[dict] = {}

    def __init__(self) -> None:
        self._registry_ref: ToolRegistry | None = None

    def execute(self, **kwargs: Any) -> ToolResult:
        """统一的执行入口：计时 + 异常捕获 + 统一返回契约。"""
        start = time.perf_counter()
        try:
            data = self._run(**kwargs)
            status, error = ToolStatus.OK, None
        except Exception as exc:  # noqa: BLE001
            data, status, error = None, ToolStatus.ERROR, str(exc)
        elapsed_ms = (time.perf_counter() - start) * 1000
        return ToolResult(
            tool=self.name,
            status=status,
            data=data,
            error=error,
            source=self.source,
            elapsed_ms=round(elapsed_ms, 2),
        )

    @abc.abstractmethod
    def _run(self, **kwargs: Any) -> Any:
        """子类实现真实逻辑。"""
        raise NotImplementedError


class ToolRegistry:
    """工具注册表：按 name 注册 / 查询 / 列出全部工具。"""

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> BaseTool:
        if tool.name in self._tools:
            raise ValueError(f"Tool '{tool.name}' already registered")
        tool._registry_ref = self
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> BaseTool:
        if name not in self._tools:
            raise KeyError(f"Tool '{name}' not registered")
        return self._tools[name]

    def names(self) -> list[str]:
        return sorted(self._tools)

    def call(self, name: str, **kwargs: Any) -> ToolResult:
        return self.get(name).execute(**kwargs)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
