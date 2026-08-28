"""把前端用户输入（input.json）解析成结构化 Requirement。

前端提交的 ``input.json`` 只是"接近" ``requirement_schema``：其中的标签是用户随手
写的原始词（未必是 ``tags.md`` 标准名），还带一个 ``free_text_requirement`` 补充说明。
本模块把这份原始输入整体转发给 LLM，让它：
- 把原始标签语义映射到标准标签名；
- 把 ``free_text_requirement`` 的语义归并到 preferred_tags / avoid_tags / dismissed_tags
  或 constraints 对应字段；
- 保留已符合 Schema 的字段原样。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_transmission.requirement import requirement_schema

SYSTEM_INSTRUCTION_TEMPLATE = (
    "你是旅行需求解析器。输入是一段来自前端表单的结构化 JSON（用户输入），"
    "请把它解析成目标 JSON Schema 的结构化旅行需求。\n"
    "解析规则：\n"
    "1. destination、start_date、days、visitor_number、constraints 里已经符合 Schema 的字段原样保留，"
    "不要改动预算、时长、人数、天数等数值。\n"
    "2. preferences.preferred_tags、preferences.avoid_tags、constraints.required_tags、"
    "constraints.dismissed_tags 里的词是用户随手写的原始标签，不一定标准，"
    "必须语义映射到下面「知识库标准标签」里的标准名称；一个原始标签可映射到一个或多个最接近的标准标签。\n"
    "3. free_text_requirement 是一段补充自然语言，请理解其语义：表达喜欢的并入 preferred_tags，"
    "表达回避/不喜欢的并入 avoid_tags 或 dismissed_tags，明确的数量/金额/时长等硬要求并入 constraints 对应字段；"
    "提到饮食偏好/菜系/忌口（如想吃什么菜、素食、不吃辣等）的并入 preferences.food_preferences（用简洁中文词），没有则为 []。\n"
    "4. 解析出发地 origin（用户从哪里出发，如「天津」）与出行时段 travel_schedule：去程/返程必须用"
    "标准日期 YYYY-MM-DD 与 24 小时制时刻 HH:MM（如 departure_date=2026-08-21、departure_time=20:00、"
    "return_date=2026-08-23、return_time=20:00）；用户只提到星期（如「周五」）或模糊时间"
    "（如「周末」「晚上」）而没有具体日期和时刻时，travel_schedule 填 null"
    "（由后续步骤追问，力求精确到日期和时刻）；没有出发地信息时 origin 填 null。\n"
    "5. 解析酒店偏好 hotel_preferences（用户提到价位段如「经济型/舒适/豪华」→ price_level；"
    "提到位置如「近地铁/胡同/市中心」→ location_preferences；提到星级如「四星以上」→ min_star）；"
    "没有酒店偏好信息时 hotel_preferences 填 null。\n"
    "5.5 解析城际交通偏好 preferences.travel_priority（可量化四维 + 省钱）："
    "用户明确表达坐高铁/倾向高铁（如「坐高铁去」「高铁优先」）→ rail；"
    "明确表达坐飞机（如「坐飞机」「飞机优先」）→ air；"
    "表达最快（如「最快/越快越好/少在路上」）→ speed；"
    "表达到达早（如「最早到/早点到/越早到越好」）→ earliest；"
    "表达省钱（如「省钱/便宜/预算紧张（指交通）」）→ cost；"
    "用户没有对交通方式的偏好或表达时，省略该字段（不要填 null）。\n"
    "6. 不要输出 free_text_requirement 这个字段；确实没有对应信息的字段填 null，列表填 []。\n"
    "知识库标准标签：{tags}\n"
    "只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出任何其它文字。"
)


def _load_standard_tags() -> List[str]:
    tags_path = Path(__file__).resolve().parent.parent / "fake_spots" / "tags.md"
    try:
        with open(tags_path, encoding="utf-8") as file:
            tags: List[str] = []
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


def build_system_instruction(tags: Optional[List[str]] = None) -> str:
    tags = tags if tags is not None else _load_standard_tags()
    return SYSTEM_INSTRUCTION_TEMPLATE.format(tags="、".join(tags))


def build_user_message(raw_input: Dict[str, Any]) -> str:
    return (
        "请把下面的用户输入解析成结构化旅行需求：\n"
        + json.dumps(raw_input, ensure_ascii=False, indent=2)
    )


def parse_requirement_input(
    raw_input: Dict[str, Any],
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """把 input.json 的原始内容转发给 LLM，返回 ``generate`` 的结构化结果。

    返回值的 ``content`` 字段即 ``requirement_schema`` 结构，可直接作为
    ``select_spots`` / ``generate_route_candidates`` 的 ``requirement`` 入参。
    """
    from call_llm.client_factory import create_llm_client

    client = create_llm_client(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        ask_user_if_missing=False,
        system_instruction=build_system_instruction(),
    )
    messages = [{"role": "user", "content": build_user_message(raw_input)}]
    return client.generate(messages=messages, response_schema=requirement_schema)
