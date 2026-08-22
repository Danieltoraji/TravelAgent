"""把多条候选路线交给大模型筛选并解释。

输入 ``generate_route_candidates`` 产出的多条可行路线，输出结构化结果：
- ``selected_route_index``：选中的路线序号（从 1 开始）
- ``ranking``：每条路线的排序、概括、优缺点
- ``reasons``：选中理由
- ``explanation``：面向用户的可解释说明
- ``selected_route``：额外附带的、选中的那条具体路线（便于直接使用）

与 ``generate_requirement`` 不同，这里关闭了「缺失信息澄清」循环，只做单次筛选。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_transmission.route_selection import (
    format_requirement_context,
    format_routes_text,
    route_selection_schema,
)

ROUTE_SELECTION_SYSTEM_INSTRUCTION = (
    "你是旅行路线筛选助手。给定用户旅行需求与若干条已满足时间、预算硬约束的候选路线，"
    "请站在用户角度比较这些路线，结合用户偏好选出最优的一条，并给出可解释的理由。"
    "不得虚构候选路线中不存在的景点或时间；评分与理由必须能对应到具体路线。"
)


def build_route_selection_messages(
    requirement: Dict[str, Any],
    routes: Sequence[Dict[str, Any]],
    user_text: Optional[str] = None,
) -> List[Dict[str, str]]:
    """构造发给大模型的对话消息（不触发网络，可离线测试）。"""
    parts = [
        "请比较下面的候选路线，选出最符合用户需求的一条，并给出可解释的理由。",
        "",
        "【用户需求】",
        format_requirement_context(requirement),
    ]
    if user_text:
        parts += ["", "【用户原始描述】", user_text]
    parts += ["", "【候选路线】", format_routes_text(routes)]
    return [{"role": "user", "content": "\n".join(parts)}]


def rank_routes(
    requirement: Dict[str, Any],
    routes: Sequence[Dict[str, Any]],
    user_text: Optional[str] = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 60,
) -> Dict[str, Any]:
    """把候选路线交给大模型筛选并解释，返回结构化结果。

    返回内容在 ``route_selection_schema`` 基础上额外附带 ``selected_route``。
    由环境变量 ``LLM_PROVIDER``（glm/deepseek）选择客户端，需要 ``GLM_API_KEY``
    （或显式传入 ``api_key``）。
    """
    from call_llm.client_factory import create_llm_client

    routes = list(routes)
    if not routes:
        raise ValueError("没有候选路线可供筛选")

    client = create_llm_client(
        model_name=model_name,
        api_key=api_key,
        base_url=base_url,
        timeout=timeout,
        ask_user_if_missing=False,
        system_instruction=ROUTE_SELECTION_SYSTEM_INSTRUCTION,
        max_tokens=2000,
    )
    messages = build_route_selection_messages(requirement, routes, user_text)
    result = client.generate(
        messages=messages, response_schema=route_selection_schema
    )
    selection = result["content"]

    selected_index = selection.get("selected_route_index")
    if isinstance(selected_index, int) and 1 <= selected_index <= len(routes):
        selection["selected_route"] = routes[selected_index - 1]
    else:
        selection["selected_route"] = None
    return selection
