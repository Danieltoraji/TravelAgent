"""Decision Engine：判断变化事件是否值得触发重新规划。

设计（8.19 确认，替换计划文档中的纯规则评分）：
- 把【用户需求】（先前 LLM 解析出的结构化 Requirement）与【变化情况】（刚注入的事件）
  一起发给大模型，由它结合上下文给出 0-100 的影响分与可解释依据。
  规则阈值只能看事件本身，无法理解「故宫排队 +20 分钟对历史偏好用户是大事、
  对最后一天的填充景点则无所谓」这类上下文——这正是交给 LLM 判断的原因。
- ``triggered = score >= DECISION_THRESHOLD``（阈值沿用计划文档的 40），
  由模块按阈值推导，保证演示时决策边界确定、可复现。
- 例外：景点关闭（closed）、酒店满房（hotel.full）意味着当前行程硬不可行，
  无需语义判断，规则直接触发（不调 LLM）。

流程：
    decide_replan(requirement, events)
        ├─ 空事件 → ValueError
        ├─ hard_rule_decision：任一 closed 事件 → 直接触发
        └─ 否则 → LLM 打分（decision_score_schema）→ 推导 triggered → 输出
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from call_llm.client_factory import create_llm_client
from data_transmission.decision import (
    DECISION_THRESHOLD,
    decision_score_schema,
    format_events_text,
)
from data_transmission.route_selection import format_requirement_context

DECISION_SYSTEM_INSTRUCTION = (
    "你是旅行决策引擎。系统正在执行一份已经排好的旅行行程，现在出现了若干变化事件"
    "（排队激增、景点关闭、天气变化、交通延误、预算变化）。"
    "你的任务：结合【用户需求】与【变化情况】，评估这些变化对当前行程的影响程度，"
    "判断是否值得重新规划。\n"
    "判断原则：\n"
    "1. 影响必须结合用户需求判断：同样大小的变化，对用户必去的核心景点、"
    "偏好强相关的景点影响大；对可替换的填充景点影响小。\n"
    "2. 考虑连锁影响：上午的延误可能拖垮整个下午；行程末段的微小变化不值得重排。\n"
    "3. 只评估影响，不要给出修改方案（修改由 RePlanner 负责）。\n"
    "4. score 是 0-100 的整数：0 表示完全无影响，100 表示行程完全不可行；"
    "reasons 必须具体、可解释，引用涉及的景点、事件与用户需求，供直接展示给用户。\n"
    "只输出一个符合给定 JSON Schema 的 JSON 对象，不要输出任何其它文字。"
)


def build_decision_messages(
    requirement: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """构造发给大模型的对话消息（不触发网络，可离线测试）。"""
    parts = [
        "系统正在执行一份已排好的旅行行程，现在出现了以下变化事件。",
        "请结合【用户需求】评估这些变化对行程的影响程度，判断是否值得重新规划"
        "（只评估影响，不输出修改方案）：",
        "",
        "【用户需求】",
        format_requirement_context(requirement),
        "",
        "【变化情况】",
        format_events_text(events),
    ]
    return [{"role": "user", "content": "\n".join(parts)}]


def hard_rule_decision(events: Sequence[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """景点关闭 / 酒店满房 → 行程硬不可行，规则直接触发重规划（不调 LLM）。

    返回决策字典；若没有硬规则命中则返回 None。
    """
    for event in events:
        event_type = event.get("event_type")
        if event_type == "closed":
            spot = event.get("spot") or "某景点"
            return {
                "triggered": True,
                "score": 100,
                "threshold": DECISION_THRESHOLD,
                "reasons": [f"{spot} 关闭，当前行程硬不可行，必须重新规划"],
                "decision_source": "hard_rule",
            }
        if event_type == "hotel" and int((event.get("metrics") or {}).get("hotel_full") or 0):
            hotel = event.get("spot") or "所选酒店"
            return {
                "triggered": True,
                "score": 100,
                "threshold": DECISION_THRESHOLD,
                "reasons": [f"{hotel} 满房，当前住宿安排硬不可行，必须重新规划"],
                "decision_source": "hard_rule",
            }
    return None


def decide_replan(
    requirement: Dict[str, Any],
    events: Sequence[Dict[str, Any]],
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 60,
    threshold: Optional[int] = None,
) -> Dict[str, Any]:
    """判断变化事件是否值得触发重规划，返回结构化决策结果。

    参数：
        threshold：触发阈值，缺省用 DECISION_THRESHOLD（40）。
            联调时 B 会把自身的 ``impact_threshold`` 放进
            ``DecisionRequest.context``，A 侧 hook 传入该值对齐判定口径。
    返回：
        triggered：是否触发重规划
        score：影响分（0-100，LLM 给出，或 closed 硬规则置 100）
        threshold：本次生效的触发阈值
        reasons：可解释依据
        decision_source：hard_rule（景点关闭硬规则）或 llm（大模型打分）
    """
    events = list(events)
    if not events:
        raise ValueError("没有变化事件可供决策")
    threshold = DECISION_THRESHOLD if threshold is None else max(1, int(threshold))

    hard = hard_rule_decision(events)
    if hard is not None:
        return hard

    client = create_llm_client(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        ask_user_if_missing=False,
        system_instruction=DECISION_SYSTEM_INSTRUCTION,
        max_tokens=800,
    )
    messages = build_decision_messages(requirement, events)
    result = client.generate(messages=messages, response_schema=decision_score_schema)
    content = result["content"]

    score = content.get("score")
    reasons = list(content.get("reasons") or [])
    if not isinstance(score, (int, float)):
        score = 0
        reasons.append("未能获得有效影响分，按不触发处理")
    score = max(0, min(100, int(score)))

    return {
        "triggered": score >= threshold,
        "score": score,
        "threshold": threshold,
        "reasons": reasons,
        "decision_source": "llm",
    }
