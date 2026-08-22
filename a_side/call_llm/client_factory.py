"""按环境变量 ``LLM_PROVIDER`` 统一创建大模型客户端。

- ``LLM_PROVIDER=deepseek``（默认）→ :class:`~call_llm.llm_clients.DSClient.DSClient`
- ``LLM_PROVIDER=glm`` → :class:`~call_llm.llm_clients.GLMClient.GLMClient`

模型 ID、API_KEY、BASE_URL 均按各自 Client 的默认值解析：
- deepseek：``DEEPSEEK_MODEL``（默认 deepseek-v4-flash），``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL``
- glm：``GLM_MODEL``（默认 GLM-5.2），``GLM_API_KEY`` / ``GLM_BASE_URL``
"""

from __future__ import annotations

from typing import Any, Optional


def create_llm_client(
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 60,
    ask_user_if_missing: bool = True,
    system_instruction: Optional[str] = None,
    max_clarifications: int = 1,
    max_tokens: int = 1200,
) -> Any:
    """按 ``LLM_PROVIDER`` 返回对应客户端实例。

    显式传入的 ``model_name`` / ``api_key`` / ``base_url`` 优先于环境变量。
    """
    from call_llm.config import (
        DEEPSEEK_API_KEY,
        DEEPSEEK_BASE_URL,
        DEEPSEEK_MODEL,
        GLM_API_KEY,
        GLM_BASE_URL,
        GLM_MODEL,
        LLM_PROVIDER,
    )

    provider = LLM_PROVIDER

    if provider == "glm":
        from call_llm.llm_clients.GLMClient import GLMClient

        return GLMClient(
            model_name=model_name or GLM_MODEL,
            api_key=api_key or GLM_API_KEY,
            base_url=base_url or GLM_BASE_URL,
            timeout=timeout,
            ask_user_if_missing=ask_user_if_missing,
            system_instruction=system_instruction,
            max_clarifications=max_clarifications,
            max_tokens=max_tokens,
        )

    if provider == "deepseek":
        from call_llm.llm_clients.DSClient import DSClient

        return DSClient(
            model_name=model_name or DEEPSEEK_MODEL,
            api_key=api_key or DEEPSEEK_API_KEY,
            base_url=base_url or DEEPSEEK_BASE_URL,
            timeout=timeout,
            ask_user_if_missing=ask_user_if_missing,
            system_instruction=system_instruction,
            max_clarifications=max_clarifications,
            max_tokens=max_tokens,
        )

    raise ValueError(f"未知的 LLM_PROVIDER：{provider!r}（可选 glm 或 deepseek）")
