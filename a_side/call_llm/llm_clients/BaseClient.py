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

# ---------------------------------------------------------------------------
# P5.6-S2 / P5.5：工具结果审查轮（review_schema 开启时，每批 role=tool 结果
# 回填后插一轮强制审查——模型按世界常识判断结果是否合理，可疑即重调，
# 否则继续主线。默认关：不传 review_schema 行为与既往完全一致。）
# ---------------------------------------------------------------------------

REVIEW_TURN_PROMPT = (
    "请审查刚才工具返回的结果是否合理（时长/价格量级、方向正确性、结果自洽；"
    "可参考各工具 description 附带的「合理性量尺」）。只输出一个符合给定 JSON "
    "Schema 的对象：{review: \"ok\" 或 \"suspicious\", reason: 一句理由}。"
    "ok = 结果合理，可继续主线；suspicious = 结果不合理，"
    "你下一步必须换参数或换工具重新查询。"
)
REVIEW_OK_GUIDE = (
    "审查结论为 ok。如需更多信息可继续调用工具；否则直接给出最终结论。"
)
REVIEW_SUSPICIOUS_GUIDE = (
    "审查结论为 suspicious：请换参数或换工具重新查询，不要原样重复同一调用；"
    "若确无其他可查，直接给出最终结论并如实说明。"
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
        expect_json: bool = True,
        review_schema: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """P4：支持 function calling 的生成入口。

        ``tools`` 为 OpenAI tools 格式；``tool_executor(name, arguments) -> JSON
        可序列化结果`` 由调用方注入（B 侧为 ``ToolProvider.call_json``，白名单
        已在 ``list_for_llm`` 收口）。模型返回 ``finish_reason=="tool_calls"``
        时逐个执行并回填 ``role=tool`` 消息后续问，最多 ``max_tool_rounds`` 轮；
        轮次超限或请求表明不支持 tools 时降级为纯文本 JSON 模式。

        ``expect_json=False``（2026-09-01，chat v2）：工具回路结束后直接返回
        模型的自然语言回复（不再强制 JSON 解析）——用于对话 + 私有工具场景；
        默认 True 保持原行为（JSON Schema 抽取）。

        ``review_schema``（2026-09-08，P5.6-S2 / P5.5）：可选 JSON object
        schema（含 ``review: ok|suspicious`` + ``reason``）。开启后**每批**
        ``role=tool`` 结果回填完插一轮强制审查（tools=None + 该 schema 收口），
        审查结论与分支指引写回对话，模型据此决定换参重调 / 换工具 / 收尾；
        返回 ``reviews``（逐轮审查记录，与 ``tool_trace`` 对齐）、
        ``uncertain``（最终轮审查为 suspicious 且模型直接收尾 = 诚实接受可疑
        结果）。**默认 None = 零行为变化**（chat/决策/旧调用方不受影响）。
        """
        conversation = list(messages)
        last_message = None
        choice = None
        clarification_count = 0
        retries = 0
        tool_rounds = 0
        tools_degraded = False
        missing_fields: List[Dict[str, Any]] = []
        reviews: List[Dict[str, Any]] = []
        tool_trace: List[Dict[str, Any]] = []

        def _final_uncertain() -> bool:
            """诚实边界：最后一次审查为 suspicious 且模型已收尾（无新调用）。"""
            return bool(reviews) and reviews[-1].get("review") == "suspicious"

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
                executed: List[Dict[str, Any]] = []
                for call in tool_calls:
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except ValueError:
                        arguments = {}
                    result = tool_executor(call.function.name, arguments)
                    executed.append({
                        "name": call.function.name,
                        "arguments": arguments,
                        "result": result,
                    })
                    conversation.append({
                        "role": "tool",
                        "tool_call_id": call.id,
                        "content": json.dumps(
                            result, ensure_ascii=False, default=str,
                        )[:8000],
                    })
                tool_trace.append({"round": tool_rounds, "calls": executed})
                # P5.6-S2 / 9.4 审查轮（可选开启）：
                # - 成本控制：整批都是结构化 error（服务端明确说不行、无"常识
                #   合理性"可审）→ 跳过该批审查（不额外花一次完整 LLM 调用）；
                # - 降级：审查是增强能力——审查轮 LLM 失败绝不拖垮主线，记
                #   warning 后按"无审查"继续（与 scenic_search_planner
                #   「失败→None+warning」同哲学，P5.5 设计意图的落实）。
                if review_schema is not None:
                    results_only = [call["result"] for call in executed]
                    all_service_errors = bool(results_only) and all(
                        isinstance(r, dict) and r.get("status") == "error"
                        for r in results_only
                    )
                    if not all_service_errors:
                        try:
                            review = self._request_review(
                                conversation, review_schema, max_retries
                            )
                        except Exception as exc:  # noqa: BLE001 - 审查失败只降级
                            logger.warning(
                                "审查轮失败（%s），降级为无审查继续主线: %s",
                                type(exc).__name__, exc,
                            )
                            continue
                        reviews.append({
                            "round": tool_rounds,
                            "tools": [call["name"] for call in executed],
                            "review": review.get("review"),
                            "reason": review.get("reason"),
                        })
                        conversation.append({
                            "role": "assistant",
                            "content": f"[审查] {json.dumps(review, ensure_ascii=False)}",
                        })
                        conversation.append({
                            "role": "user",
                            "content": (
                                REVIEW_SUSPICIOUS_GUIDE
                                if review.get("review") == "suspicious"
                                else REVIEW_OK_GUIDE
                            ),
                        })
                continue

            if not expect_json:
                # 对话 + 私有工具模式（chat v2）：工具回路结束后直接返回
                # 自然语言回复，跳过 JSON Schema 解析（generate 默认仍是
                # JSON 模式，此处仅当调用方显式关闭时生效）。
                return {
                    "content": str(raw_content or "").strip(),
                    "tool_calls": getattr(last_message, "tool_calls", None),
                    "finish_reason": getattr(choice, "finish_reason", None),
                    "tool_rounds": tool_rounds,
                    "tools_degraded": tools_degraded,
                    "reviews": reviews,
                    "uncertain": _final_uncertain(),
                    "tool_trace": tool_trace,
                    "raw": {"model_output": getattr(last_message, "content", None)},
                }

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
            "reviews": reviews,
            "uncertain": _final_uncertain(),
            "tool_trace": tool_trace,
            "raw": {"model_output": getattr(last_message, "content", None)},
        }

    def _request_review(
        self,
        conversation: List[Dict[str, Any]],
        review_schema: Dict[str, Any],
        max_retries: int,
    ) -> Dict[str, Any]:
        """对刚回填的 role=tool 结果发起一轮强制审查（tools=None + schema 收口）。

        审查结论（{review: ok|suspicious, reason}）不写回 conversation 之外的
        任何状态，仅由 generate 主循环负责记录与续写指引——本方法只负责取回
        一条结构化审查结论；解析/取值非法时按既有重试纪律回传重试。
        """
        review_messages = list(conversation)
        review_messages.append({"role": "user", "content": REVIEW_TURN_PROMPT})
        last_raw = None
        for _ in range(max_retries + 1):
            response = self._request_completion(
                self._build_request_params(review_messages, review_schema, None)
            )
            if not getattr(response, "choices", None):
                raise RuntimeError("LLM response contains no choices")
            raw = getattr(response.choices[0].message, "content", None)
            last_raw = raw
            parsed = self._extract_json_payload(raw)
            if parsed is None:
                self._append_retry_turn(review_messages, raw, RETRY_NOT_JSON_MESSAGE)
                continue
            normalized = self._normalize_to_schema(parsed, review_schema)
            if normalized.get("review") not in ("ok", "suspicious"):
                self._append_retry_turn(
                    review_messages,
                    raw,
                    "审查输出必须含 review 且取值 ok 或 suspicious，以及一句 reason。"
                    "请重新只输出 JSON。",
                )
                continue
            return normalized
        raise ValueError(
            f"LLM 审查轮在 {max_retries} 次重试后仍未给出有效结论: {last_raw!r}"
        )
