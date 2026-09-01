"""PlannerAgent：规划子链路的 LLM 自主工具回路（方案 §三 P5.2）。

背景：A 侧规划/决策主链路（BPlannerHook/BDecisionHook）是**确定性编排**——
模型只输出结构（需求/影响分/意图），工具调用由 A 侧代码显式驱动，不经过
LLM 的 function calling。P5.2 补上 **LLM 自主工具回路**：一条规划子链路
（城际候选验证 ``intercity_verify``）让模型**自己决定**查哪些真源工具
（train_trip / train_ticket / flight_search）、查几次、看完结果给判断与
依据——工具面由 A 侧 ``ToolSpec`` 注册表（``data_transmission.tool_specs``）
提供，executor 过 ``QuotaManager``（B 侧 ToolProvider 包装，计数 + 节律）。

门控（与 decision_engine 同款）：``env USE_LLM_TOOLS in (1, true, yes)`` 且
注入 ``tool_provider`` 才把 tools 交给模型；**默认关**——关闭时行为与既往
完全一致（不回退、不查真源、零额度消耗），测试零回归。

形态适配：BaseClient.generate 的 ``tool_executor(name, arguments)`` 是单
dict 形态；B 侧 ``ToolProvider.call`` 是 kwargs 形态
（``QuotaManager.call(name, **kwargs)``），此处经 ``_tool_executor`` 桥接。
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from call_llm.client_factory import create_llm_client
from call_llm.decision_engine import _use_llm_tools
from data_transmission.enums import Mode
from data_transmission.tool_specs import TOOL_SPECS, to_openai_tools

# ---------------------------------------------------------------------------
# 城际候选验证子链路（intercity_verify）的提示词与响应 Schema
# ---------------------------------------------------------------------------

INTERCITY_VERIFY_SYSTEM = (
    "你是旅行规划助手。用户在核对两个城市之间某天的可行出行班次，"
    "你需要用给定的真源工具查询并给出结论。规则：\n"
    "1. 先调用 train_trip（城市对 → 代表班次）摸清该方向是否有火车直达、"
    "大概票价与历时；\n"
    "2. 必要时再调用 train_ticket（具体车站对 → 详细车次/余票）或"
    " flight_search（航班）比对时间与成本；\n"
    "3. 查询结果以 role=tool 消息回填给你，请基于**真实返回**给结论，"
    "不要编造班次或价格；\n"
    "4. 工具可能返回空结果或错误——如实说明「该方向无可用班次/查询失败」，"
    "不要硬凑；\n"
    "5. 最后输出 JSON：{chosen: 选定的班次对象或 null, reasons: 依据列表, "
    "checked: 你实际调用过的工具名列表}。"
)

INTERCITY_VERIFY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "chosen": {
            "type": "object",
            "description": "选定的班次（mode/train_no/flight_no/departure/arrival/"
            "duration_minutes/price 等，来自真实工具返回）；无可用班次时为 null",
            "properties": {
                "mode": {"type": "string", "enum": ["train", "air", "driving"]},
                "train_no": {"type": "string"},
                "flight_no": {"type": "string"},
                "departure": {"type": "string"},
                "arrival": {"type": "string"},
                "duration_minutes": {"type": "integer"},
                "price": {"type": "number"},
            },
            "additionalProperties": True,
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "选择依据（必须引用工具实际返回的班次/价格/历时）",
        },
        "checked": {
            "type": "array",
            "items": {"type": "string"},
            "description": "实际调用过的工具名列表",
        },
        "unavailable": {
            "type": "boolean",
            "description": "该方向无可用班次（真源均空/失败）时为 true",
        },
    },
    "required": ["chosen", "reasons", "checked", "unavailable"],
}


def _user_verify_prompt(
    from_city: str, to_city: str, date: str, preferred_mode: Optional[str]
) -> str:
    mode_hint = ""
    if preferred_mode:
        mode_hint = f"\n用户偏好交通方式：{preferred_mode}（仅供参考，无直达可换）"
    return (
        f"请核对 {from_city} → {to_city} 在 {date} 的可行班次。"
        f"{mode_hint}"
        "\n先查火车，再视情况比对航班，最后按 schema 输出 JSON。"
    )


class PlannerAgent:
    """LLM 自主工具回路执行器（P5.2，规划子链路接 agent loop）。

    使用方法：:

        agent = PlannerAgent(
            requirement=req,
            tool_provider=tool_provider,      # B 侧 ToolProvider（真源门面）
        )
        result = agent.intercity_verify("锦州", "常州", "2026-09-01")
        # -> {chosen, reasons, checked, unavailable, tool_rounds, tools_degraded}

    ``tool_provider`` 缺省 None + ``USE_LLM_TOOLS`` 缺省关闭 → 工具面不注入
    （LLM 纯文本回答，代码零回归）；注入后仍需 env 开关才启用（门控一致）。
    """

    def __init__(
        self,
        requirement: Dict[str, Any],
        *,
        tool_provider: Any = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        max_tool_rounds: int = 5,
    ):
        self.requirement = requirement
        self.tool_provider = tool_provider
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_tool_rounds = max_tool_rounds
        # executor 依赖 B 侧 ToolProvider；无 provider 时工具回路不可用
        self._executor: Optional[Callable[[str, Dict[str, Any]], Any]] = None
        if tool_provider is not None:
            from data_transmission.quota_manager import make_quota_manager

            self._quota = make_quota_manager(tool_provider)
            self._executor = self._tool_executor

    @property
    def tools_enabled(self) -> bool:
        """工具回路是否可用：注入 provider **且** env 门控开启（默认关）。"""
        return self._executor is not None and _use_llm_tools()

    def _tool_executor(self, name: str, arguments: Dict[str, Any]) -> Any:
        """BaseClient.generate 的 (name, arguments) 形态 → QuotaManager kwargs。"""
        return self._quota.call(name, **arguments)

    def intercity_verify(
        self,
        from_city: str,
        to_city: str,
        date: str,
        preferred_mode: Optional[str] = None,
    ) -> Dict[str, Any]:
        """城际候选验证子链路：LLM 自主查真源工具并给出结论。

        返回 dict：::

            chosen / reasons / checked / unavailable  模型结论（必填字段）
            tool_rounds / tools_degraded              BaseClient 工具回路观测
            tools_enabled                             本次是否启用工具面

        门控关闭时模型无法查真源——结论可能不准确（如凭常识猜测），
        ``tools_enabled=False`` 明确标注，调用方（探针/演示）自行判断可信度。
        """
        client = create_llm_client(
            model_name=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            ask_user_if_missing=False,
            system_instruction=INTERCITY_VERIFY_SYSTEM,
            max_tokens=1000,
        )
        messages = [
            {
                "role": "user",
                "content": _user_verify_prompt(from_city, to_city, date, preferred_mode),
            }
        ]
        tools: Optional[List[Dict[str, Any]]] = None
        tool_executor: Optional[Callable[[str, Dict[str, Any]], Any]] = None
        if self.tools_enabled:
            tools = to_openai_tools(names=["train_trip", "train_ticket", "flight_search"])
            tool_executor = self._executor

        result = client.generate(
            messages=messages,
            response_schema=INTERCITY_VERIFY_SCHEMA,
            tools=tools,
            tool_executor=tool_executor,
            max_tool_rounds=self.max_tool_rounds,
        )
        content = result["content"] or {}
        return {
            "chosen": content.get("chosen"),
            "reasons": list(content.get("reasons") or []),
            "checked": list(content.get("checked") or []),
            "unavailable": bool(content.get("unavailable")),
            "tool_rounds": int(result.get("tool_rounds") or 0),
            "tools_degraded": bool(result.get("tools_degraded")),
            "tools_enabled": bool(tools),
        }


def intercity_mode_names() -> List[str]:
    """城际三真源工具名（TrainT/Air 模式绑定，供探针/测试断言）。"""
    return [
        spec.name
        for spec in TOOL_SPECS.values()
        if spec.mode in (Mode.TRAIN.value, Mode.AIR.value)
    ]