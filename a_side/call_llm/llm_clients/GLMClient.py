import json
from typing import Any, Dict, List, Optional

import openai

from .BaseClient import LLMClient


class GLMClient(LLMClient):
    def __init__(
        self,
        model_name: str,
        api_key: str,
        base_url: str,
        timeout: int = 60,
        ask_user_if_missing: bool = True,
        system_instruction: Optional[str] = None,
        max_clarifications: int = 1,
        max_tokens: int = 1200,
    ):
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
        """Build a request using GLM's supported JSON output format.

        GLM accepts ``response_format={"type": "json_object"}``, but does not
        receive the JSON Schema through that parameter. Therefore the complete
        schema is included in the system instruction for the model to follow.
        """
        prompt_messages = self._build_prompt_messages(
            list(messages), response_schema
        )

        if response_schema is not None:
            schema_instruction = (
                "\n\n请严格按照下面的 JSON Schema 生成结果：\n"
                + json.dumps(response_schema, ensure_ascii=False, indent=2)
                + "\n只返回一个有效的 JSON 对象，不要使用 Markdown 代码块，"
                "不要输出解释或其他文字。"
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
            "temperature": 0.0,
            "max_tokens": self.max_tokens,
        }
        if response_schema is not None:
            params["response_format"] = {"type": "json_object"}
        if tools:
            params["tools"] = tools
            params["tool_choice"] = "auto"
        return params

    def _request_completion(self, create_params: Dict[str, Any]) -> Any:
        return self._sdk_client.chat.completions.create(**create_params)
