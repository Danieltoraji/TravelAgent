"""DeepSeek 官方 API 客户端（https://api.deepseek.com）。

- JSON 输出：``response_format={"type": "json_object"}``（完整 Schema 通过系统指令下发给模型）
- 思考模式：``extra_body={"thinking": {"type": "enabled"|"disabled"}}`` 手动开关（默认关闭），
  开启时可配合 ``reasoning_effort``
- 默认模型 ID：``deepseek-v4-flash``
- API_KEY / BASE_URL：默认读 ``DEEPSEEK_API_KEY`` / ``DEEPSEEK_BASE_URL``
"""

import json
from typing import Any, Dict, List, Optional

import openai

from .BaseClient import LLMClient

DEFAULT_MODEL = "deepseek-v4-flash"


class DSClient(LLMClient):
    def __init__(
        self,
        model_name: str = DEFAULT_MODEL,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        ask_user_if_missing: bool = True,
        system_instruction: Optional[str] = None,
        max_clarifications: int = 1,
        max_tokens: int = 1200,
        enable_thinking: bool = False,
        reasoning_effort: Optional[str] = None,
    ):
        if api_key is None or base_url is None:
            from call_llm.config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL

            api_key = api_key or DEEPSEEK_API_KEY
            base_url = base_url or DEEPSEEK_BASE_URL
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            ask_user_if_missing=ask_user_if_missing,
            system_instruction=system_instruction,
            max_clarifications=max_clarifications,
        )
        self.max_tokens = max_tokens
        self.enable_thinking = enable_thinking
        self.reasoning_effort = reasoning_effort
        self._sdk_client = openai.OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )

    def _build_request_params(
        self,
        messages: List[Dict[str, str]],
        response_schema: Optional[Dict[str, Any]] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """DeepSeek 官方 API：JSON 输出 + 手动思考模式开关。

        与 GLM 一样，DeepSeek 不通过 ``response_format`` 接收 Schema，因此把
        完整 Schema 写进系统指令交给模型遵循。
        """
        prompt_messages = self._build_prompt_messages(list(messages), response_schema)

        if response_schema is not None:
            schema_instruction = (
                "\n\n请严格按照下面的 JSON Schema 生成结果：\n"
                + json.dumps(response_schema, ensure_ascii=False, indent=2)
                + "\n只返回一个有效的 JSON 对象，不要使用 Markdown 代码块，"
                "不要输出解释、前后缀或任何其它文字。"
            )
            if prompt_messages and prompt_messages[0].get("role") == "system":
                prompt_messages[0] = {
                    **prompt_messages[0],
                    "content": prompt_messages[0]["content"] + schema_instruction,
                }
            else:
                prompt_messages.insert(
                    0, {"role": "system", "content": schema_instruction.strip()}
                )

        params: Dict[str, Any] = {
            "model": self.model_name,
            "messages": prompt_messages,
            "max_tokens": self.max_tokens,
        }
        # 思考模式关闭时用 temperature 做确定性输出；开启时改用 reasoning_effort。
        if not self.enable_thinking:
            params["temperature"] = 0.0
        if response_schema is not None:
            params["response_format"] = {"type": "json_object"}
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"

        # thinking 是 DeepSeek 自定义参数，必须放进 extra_body 传给 OpenAI SDK。
        params["extra_body"] = {
            "thinking": {"type": "enabled" if self.enable_thinking else "disabled"}
        }
        if self.enable_thinking and self.reasoning_effort:
            params["reasoning_effort"] = self.reasoning_effort
        return params

    def _request_completion(self, create_params: Dict[str, Any]) -> Any:
        return self._sdk_client.chat.completions.create(**create_params)
