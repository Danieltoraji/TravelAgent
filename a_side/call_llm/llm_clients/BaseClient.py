import json
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("call_llm.llm_client")

# 默认系统指令：旅行需求解析。其它用途（例如路线筛选）可通过
# ``system_instruction`` 覆盖，避免复用需求解析的措辞。
DEFAULT_REQUIREMENT_SYSTEM_INSTRUCTION = (
    "你是旅行需求解析器。仅提取用户明确提供的信息，不得猜测。"
    "未知字段必须返回 null；未提及的列表返回空列表。"
    "日期使用 YYYY-MM-DD，金额使用人民币元，时长使用分钟。"
    "只输出一个符合给定 JSON Schema 的 JSON 对象，"
    "不要使用 Markdown 代码块，不要输出任何解释或前后缀文字。"
)

# 解析失败 / 全空抽取时回传给模型的重试反馈（作为 assistant 之后的 user 消息）。
RETRY_NOT_JSON_MESSAGE = (
    "你上一次的输出不是合法的 JSON。请重新输出，"
    "只返回一个有效的 JSON 对象，不要使用 Markdown 代码块，"
    "不要输出任何解释或前后缀文字。"
)
RETRY_BLANK_MESSAGE = (
    "你上一次输出的字段几乎全为空（null/[]），说明没有从用户输入中提取到信息。"
    "请重新仔细阅读用户输入，提取所有能确定的信息，"
    "并只输出一个完整的 JSON 对象。"
)


class LLMClient:
    """Provider-independent chat client with schema extraction and clarification."""

    def __init__(
        self,
        model_name: str,
        api_key: str,
        base_url: Optional[str] = None,
        timeout: int = 60,
        ask_user_if_missing: bool = True,
        user_input_fn: Optional[Callable[[str], str]] = None,
        max_clarifications: int = 1,
        system_instruction: Optional[str] = None,
    ):
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.ask_user_if_missing = ask_user_if_missing
        self.user_input_fn = user_input_fn or input
        self.max_clarifications = max_clarifications
        self._tags = self._load_tags()
        self.system_instruction = system_instruction or self._default_system_instruction()

    def _default_system_instruction(self) -> str:
        instruction = DEFAULT_REQUIREMENT_SYSTEM_INSTRUCTION
        if self._tags:
            instruction += " 标签字段只能选用以下知识库标签：" + "、".join(self._tags)
        return instruction

    def _load_tags(self) -> List[str]:
        tags_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "fake_spots", "tags.md"
        )
        try:
            with open(os.path.abspath(tags_path), encoding="utf-8") as file:
                tags = []
                for line in file:
                    normalized = line.strip()
                    if not normalized or normalized.startswith("#"):
                        continue
                    if normalized.startswith("以下标签与"):
                        continue
                    tag = normalized.lstrip("-* ").strip()
                    if tag and tag not in tags:
                        tags.append(tag)
                return tags
        except OSError:
            return []

    def _build_prompt_messages(
        self, messages: List[Dict[str, str]], response_schema: Optional[Dict[str, Any]]
    ) -> List[Dict[str, str]]:
        if response_schema is None:
            return messages
        return [{"role": "system", "content": self.system_instruction}, *messages]

    def _build_request_params(self, messages, response_schema=None, tools=None):
        raise NotImplementedError

    def _request_completion(self, create_params: Dict[str, Any]) -> Any:
        raise NotImplementedError

    def _extract_json_payload(self, text: Any) -> Optional[Dict[str, Any]]:
        if isinstance(text, dict):
            return text
        if not text:
            return None
        normalized = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(text).strip(), flags=re.S)
        try:
            value = json.loads(normalized)
            return value if isinstance(value, dict) else None
        except json.JSONDecodeError:
            decoder = json.JSONDecoder()
            for index, char in enumerate(normalized):
                if char == "{":
                    try:
                        value, _ = decoder.raw_decode(normalized[index:])
                        if isinstance(value, dict):
                            return value
                    except json.JSONDecodeError:
                        continue
        return None

    @staticmethod
    def _schema_types(schema_node: Dict[str, Any]) -> List[str]:
        value = schema_node.get("type")
        return value if isinstance(value, list) else [value] if value else []

    def _collect_missing_fields(self, result: Any, schema: Dict[str, Any], path=""):
        """Collect only required fields whose value is absent/null/blank."""
        missing = []
        if "object" not in self._schema_types(schema) or not isinstance(result, dict):
            return missing
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            child_schema = properties.get(key, {})
            child_path = f"{path}.{key}" if path else key
            value = result.get(key)
            blank = value is None or (isinstance(value, str) and not value.strip())
            if blank:
                missing.append({"path": child_path, "schema": child_schema})
            elif "object" in self._schema_types(child_schema):
                missing.extend(self._collect_missing_fields(value, child_schema, child_path))
        return missing

    def _normalize_to_schema(self, parsed: Any, schema: Optional[Dict[str, Any]]):
        if not schema or not isinstance(parsed, dict):
            return parsed if isinstance(parsed, dict) else {}

        def normalize(value: Any, node: Dict[str, Any]):
            types = self._schema_types(node)
            if value is None:
                return None
            if "object" in types:
                source = value if isinstance(value, dict) else {}
                return {
                    key: normalize(source.get(key), child)
                    for key, child in node.get("properties", {}).items()
                }
            if "array" in types:
                if not isinstance(value, list):
                    return []
                return [normalize(item, node.get("items", {})) for item in value]
            if "integer" in types:
                try:
                    return int(value)
                except (TypeError, ValueError):
                    return None
            if "number" in types:
                try:
                    return float(value)
                except (TypeError, ValueError):
                    return None
            if "boolean" in types:
                return value if isinstance(value, bool) else None
            if "string" in types:
                return str(value)
            return value

        # Semantic mapping belongs to the LLM. This method only enforces the
        # schema's structure and primitive JSON types.
        return normalize(parsed, schema)

    @staticmethod
    def _question_for_missing(missing: List[Dict[str, Any]]) -> str:
        labels = [item["schema"].get("title") or item["path"] for item in missing]
        return "还需要补充以下信息：" + "、".join(labels) + "。请用自然语言回答："

    @classmethod
    def _is_blank(cls, value: Any) -> bool:
        """递归判断一个值是否「空」：null、空白串、空容器或全空的嵌套结构。

        数字（含 0）和布尔值（含 False）视为有效信息，不算空。
        """
        if value is None:
            return True
        if isinstance(value, str):
            return not value.strip()
        if isinstance(value, dict):
            return all(cls._is_blank(v) for v in value.values())
        if isinstance(value, list):
            return all(cls._is_blank(v) for v in value)
        return False

    @staticmethod
    def _append_retry_turn(
        conversation: List[Dict[str, str]], raw_content: Any, feedback: str
    ) -> None:
        conversation.append({"role": "assistant", "content": raw_content or ""})
        conversation.append({"role": "user", "content": feedback})

    def chat_text(
        self,
        messages: List[Dict[str, str]],
        max_retries: int = 2,
    ) -> str:
        """纯文本对话（无 JSON Schema / 无工具）：返回模型回复文本。

        2026-09-01：面向 C 端对话接口（POST /api/chat/）新增。
        ``generate()`` 是 JSON 强制模式，对话场景用本方法直接取
        ``choice.message.content``；系统指令由调用方作为消息列表首条传入。
        """
        conversation = list(messages)
        for attempt in range(max_retries):
            try:
                response = self._request_completion(
                    self._build_request_params(conversation, None, None)
                )
            except Exception as exc:
                raise RuntimeError(
                    f"LLM request failed: model={self.model_name}, "
                    f"base_url={self.base_url}, detail={exc}"
                ) from exc
            if not getattr(response, "choices", None):
                raise RuntimeError("LLM response contains no choices")
            content = getattr(response.choices[0].message, "content", None)
            if content and str(content).strip():
                return str(content)
        raise RuntimeError(
            f"LLM returned empty content after {max_retries} retries"
        )

    def generate(
        self,
        messages,
        response_schema=None,
        tools=None,
        max_retries: int = 2,
        tool_executor=None,
        max_tool_rounds: int = 3,
    ) -> Dict[str, Any]:
        """P4：支持 function calling 的生成入口。

        ``tools`` 为 OpenAI tools 格式；``tool_executor(name, arguments) -> JSON
        可序列化结果`` 由调用方注入（B 侧为 ``ToolProvider.call_json``，白名单
        已在 ``list_for_llm`` 收口）。模型返回 ``finish_reason=="tool_calls"``
        时逐个执行并回填 ``role=tool`` 消息后续问，最多 ``max_tool_rounds`` 轮；
        轮次超限或请求表明不支持 tools 时降级为纯文本 JSON 模式。
        """
        conversation = list(messages)
        last_message = None
        choice = None
        clarification_count = 0
        retries = 0
        tool_rounds = 0
        tools_degraded = False
        missing_fields: List[Dict[str, Any]] = []

        while True:
            try:
                response = self._request_completion(
                    self._build_request_params(conversation, response_schema, tools)
                )
            except Exception as exc:
                # 降级：模型/网关不支持 tools（首次且尚未降级时）→ 去掉工具重试一次
                if tools and tool_executor is not None and not tools_degraded \
                        and "tool" in str(exc).lower():
                    tools_degraded = True
                    tools = None
                    tool_executor = None
                    logger.warning(
                        "LLM 不支持 tools，已降级为纯文本 JSON 模式: %s", exc,
                    )
                    continue
                raise RuntimeError(
                    f"LLM request failed: model={self.model_name}, base_url={self.base_url}, detail={exc}"
                ) from exc
            if not getattr(response, "choices", None):
                raise RuntimeError("LLM response contains no choices")
            choice = response.choices[0]
            last_message = choice.message
            raw_content = getattr(last_message, "content", None)

            # P4：tool_calls 回路
            tool_calls = getattr(last_message, "tool_calls", None) or []
            if tool_calls and tool_executor is not None:
                if tool_rounds >= max_tool_rounds:
                    raise ValueError(
                        f"工具调用轮次超过上限 {max_tool_rounds}，已降级拒绝继续"
                    )
                tool_rounds += 1
                conversation.append({
                    "role": "assistant",
                    "content": raw_content or "",
                    "tool_calls": [
                        {
                            "id": call.id,
                            "type": "function",
                            "function": {
                                "name": call.function.name,
                                "arguments": call.function.arguments,
                            },
                        }
                        for call in tool_calls
                    ],
                })
                for call in tool_calls:
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except ValueError:
                        arguments = {}
                    result = tool_executor(call.function.name, arguments)
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            result, ensure_ascii=False, default=str,
                        )[:8000],
                    })
                continue

            parsed = self._extract_json_payload(raw_content)
            if parsed is None:
                if retries < max_retries:
                    retries += 1
                    self._append_retry_turn(
                        conversation, raw_content, RETRY_NOT_JSON_MESSAGE
                    )
                    continue
                raise ValueError(
                    f"LLM did not return a JSON object after {max_retries} retries: {raw_content!r}"
                )

            content = self._normalize_to_schema(parsed, response_schema)
            if self._is_blank(content) and retries < max_retries:
                retries += 1
                self._append_retry_turn(
                    conversation, raw_content, RETRY_BLANK_MESSAGE
                )
                continue

            missing = self._collect_missing_fields(content, response_schema or {})
            if not missing or not self.ask_user_if_missing:
                break
            if clarification_count >= self.max_clarifications:
                break

            question = self._question_for_missing(missing)
            answer = self.user_input_fn(question).strip()
            if not answer:
                break
            conversation.extend(
                [
                    {"role": "assistant", "content": raw_content},
                    {
                        "role": "user",
                        "content": "针对缺失信息的补充回答：" + answer + "。请合并此前需求并输出完整 JSON。",
                    },
                ]
            )
            clarification_count += 1

        return {
            "content": content,
            "missing_fields": [item["path"] for item in missing],
            "clarification_count": clarification_count,
            "tool_calls": getattr(last_message, "tool_calls", None),
            "finish_reason": getattr(choice, "finish_reason", None),
            "tool_rounds": tool_rounds,
            "tools_degraded": tools_degraded,
            "raw": {"model_output": getattr(last_message, "content", None)},
        }
