"""统一工具抽象层：BaseTool 抽象基类 + ToolRegistry 注册表。

设计（对应《任务整理.md》模块3 Tool Agents）：
- 每个领域 Tool 只负责自己的 API；
- 所有 Tool 返回统一的 ToolResult 契约，由上层（Execution Agent / 外部调用方）组合编排；
- 真实 API 与 Mock 实现遵循同一签名，切换时调用方零改动。
"""

from __future__ import annotations

import abc
import inspect
import logging
import time
from typing import Any, ClassVar
from urllib.error import URLError

from core.schemas import ToolResult, ToolSpec, ToolStatus

logger = logging.getLogger("tools.base")

# 这些异常被视为网络错误，值得重试；其他异常（如 ValueError）是业务错误，不重试
_RETRYABLE_ERRORS = (URLError, TimeoutError, ConnectionError, OSError)


def _type_matches(value: Any, expected: Any) -> bool:
    """JSON Schema 基本类型 → Python 类型判定（支持类型数组，如 ["string","integer"]）。"""
    for t in (expected if isinstance(expected, (list, tuple)) else [expected]):
        if t == "string" and isinstance(value, str):
            return True
        if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
            return True
        if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
            return True
        if t == "boolean" and isinstance(value, bool):
            return True
        if t == "array" and isinstance(value, (list, tuple)):
            return True
        if t == "object" and isinstance(value, dict):
            return True
    return False


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
    readonly: bool = True           # 是否只读；有副作用工具应设为 False
    input_schema: ClassVar[dict] = {}

    def __init__(self) -> None:
        self._registry_ref: ToolRegistry | None = None

    def spec(self) -> ToolSpec:
        """返回该工具的元数据，供 A 侧 LLM 理解与调用。"""
        return ToolSpec(
            name=self.name,
            description=self.description,
            input_schema=dict(self.input_schema),
            readonly=self.readonly,
            source=self.source,
        )


    def execute(self, **kwargs: Any) -> ToolResult:
        """统一的执行入口：计时 + 异常捕获 + 重试 + 统一返回契约。

        网络错误（URLError / timeout / ConnectionError）自动重试，指数退避；
        业务错误（ValueError 等）不重试，直接返回 ERROR。
        """
        from config.settings import settings

        max_retries = settings.max_retries
        backoff_base = settings.retry_backoff_base

        start = time.perf_counter()

        # C5：轻量 schema 校验（required + 基本类型，不引入 jsonschema）。
        # 校验失败属业务错误，直接返回 ERROR 不重试。
        try:
            self._validate_kwargs(kwargs)
        except ValueError as exc:
            logger.warning("%s 参数校验失败: %s", self.name, exc)
            return ToolResult(
                tool=self.name, status=ToolStatus.ERROR, data=None,
                error=str(exc), source=self.source, elapsed_ms=0.0,
            )

        last_exc: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                data = self._run(**kwargs)
                elapsed_ms = (time.perf_counter() - start) * 1000
                return ToolResult(
                    tool=self.name,
                    status=ToolStatus.OK,
                    data=data,
                    error=None,
                    source=self.source,
                    elapsed_ms=round(elapsed_ms, 2),
                )
            except _RETRYABLE_ERRORS as exc:
                last_exc = exc
                if attempt < max_retries:
                    wait = backoff_base * (2 ** attempt)
                    logger.warning(
                        "%s attempt %d/%d failed (%s), retrying in %.1fs",
                        self.name, attempt + 1, max_retries + 1, exc, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("%s failed after %d attempts: %s", self.name, max_retries + 1, exc)
            except Exception as exc:  # noqa: BLE001
                # 业务错误不重试
                last_exc = exc
                break

        elapsed_ms = (time.perf_counter() - start) * 1000
        return ToolResult(
            tool=self.name,
            status=ToolStatus.ERROR,
            data=None,
            error=str(last_exc) if last_exc else "unknown error",
            source=self.source,
            elapsed_ms=round(elapsed_ms, 2),
        )

    @abc.abstractmethod
    def _run(self, **kwargs: Any) -> Any:
        """子类实现真实逻辑。"""
        raise NotImplementedError

    def _validate_kwargs(self, kwargs: dict) -> None:
        """C5：按 input_schema 做 required + 基本类型校验（Mock/Live 同受约束）。

        - required 缺失：若 `_run` 实现侧对该参数有默认值，则允许省略
          （如 hotel/scenic 的 `action` 缺省即 search，live_data/booking_manager
          均依赖此缺省）；否则报错。
        - 类型：仅校验 schema 中声明了的入参；未声明的多余参数不拦
          （保持 **kwargs 直传兼容）。
        """
        schema = self.input_schema
        if not schema:
            return
        props = schema.get("properties") or {}
        params = inspect.signature(self._run).parameters

        for key in schema.get("required") or []:
            if key in kwargs:
                if kwargs[key] is None or kwargs[key] == "":
                    raise ValueError(f"必填参数为空: {key}")
                continue
            param = params.get(key)
            if param is not None and param.default is not inspect.Parameter.empty:
                continue
            raise ValueError(f"缺少必填参数: {key}")

        for key, value in kwargs.items():
            spec = props.get(key)
            if spec is None:
                continue
            expected = spec.get("type")
            if expected and value is not None and not _type_matches(value, expected):
                raise ValueError(
                    f"参数 {key} 类型应为 {expected}，实际为 {type(value).__name__}"
                )


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

    def list_specs(self, allowlist: set[str] | None = None) -> list[ToolSpec]:
        """返回全部（或白名单内）工具的元数据，按名称排序。"""
        names = self.names()
        if allowlist is not None:
            names = [n for n in names if n in allowlist]
        return [self._tools[n].spec() for n in names]

    def get_spec(self, name: str) -> ToolSpec:
        return self.get(name).spec()


    def call(self, name: str, **kwargs: Any) -> ToolResult:
        return self.get(name).execute(**kwargs)

    def __contains__(self, name: str) -> bool:
        return name in self._tools
