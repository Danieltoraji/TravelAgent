"""技能层注册表（架构整理方案 §三 P5.6 技能层）。

背景与拍板（2026-09-08）：
- **形态 A**：技能 = TravelAgent 产品运行时 agent **可直接调用的具名能力**。
  外层 agent 的工具面上会出现 ``skill__<名>`` 条目（命名空间防与真源工具
  重名），agent 自主决定调用哪个技能——比「调哪个扁平工具」更高一层的
  可解释决策；
- **审查内置**：结果审查 + 可疑重调 + ``uncertain`` 诚实标记（P5.5）是
  SkillRunner 的**共用横切环节**，每个技能自带，不复制；
- **首批技能 = 城际班次核验**（``intercity_verify``，S3 已注册，executor =
  ``call_llm.planner_agent:intercity_verify_executor``）；ScenicSearchPlanner /
  trip_center / 决策打分等暂不技能化（范围未定，S6 搁置）。

本模块 = 技能层的**数据面**（仿 ``data_transmission/tool_specs.py`` 模式）：
- ``SkillSpec``：单个技能的元数据——名称 / 何时用 / 入参出参 schema /
  子工具面（tools）/ 审查开关 / 实现入口，**A 侧权威定义**；
- ``SKILL_SPECS``：技能注册表（S1 空骨架，S3 起含真实条目）；
- ``validate_skill_spec``：守卫校验（工具子集必须落在 ToolSpec 注册表内、
  技能名不得与真源工具重名/不得带 ``skill__`` 前缀、schema 形状合法、
  已注册技能必须接 executor 点路径），供守卫测试逐条断言——技能层不依赖
  AI 自觉。

通用执行器 ``SkillRunner``（``call_llm/skill_runner.py``）按 ``executor``
点路径惰性导入并执行技能；``params_schema``/``result_schema`` 直接引用
执行器模块的权威 schema（此处 import planner_agent 仅为取常量，无循环：
planner_agent 不 import 本模块）。
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from call_llm.planner_agent import (
    INTERCITY_INPUT_SCHEMA,
    INTERCITY_VERIFY_SCHEMA,
)
from data_transmission.tool_specs import TOOL_SPECS

# 工具面命名空间：技能以外层 ``skill__<name>`` 形态暴露，防止与真源工具
# （train_trip 等）或 TOOL_SPECS 既有名字撞名。
SKILL_PREFIX = "skill__"
_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


class SkillSpec:
    """单个技能的元数据（A 侧权威定义；S3 起含真实条目）。"""

    __slots__ = (
        "name",
        "description",
        "when_to_use",
        "params_schema",
        "result_schema",
        "tools",
        "review_enabled",
        "executor",
    )

    def __init__(
        self,
        name: str,
        description: str,
        when_to_use: str,
        params_schema: Dict[str, Any],
        result_schema: Optional[Dict[str, Any]] = None,
        tools: Optional[List[str]] = None,
        review_enabled: bool = True,
        executor: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.when_to_use = when_to_use
        self.params_schema = params_schema
        self.result_schema = result_schema
        self.tools = list(tools) if tools else []
        self.review_enabled = review_enabled
        self.executor = executor

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"SkillSpec(name={self.name!r}, tools={self.tools!r}, "
            f"review_enabled={self.review_enabled}, executor={self.executor!r})"
        )


# ---------------------------------------------------------------------------
# 注册表（S3：首个技能「城际班次核验」；新增技能 = 在此加一条 + 实现 executor）
# ---------------------------------------------------------------------------

SKILL_SPECS: Dict[str, SkillSpec] = {
    "intercity_verify": SkillSpec(
        name="intercity_verify",
        description=(
            "城际班次核验：核验两个城市之间某一天的可行出行班次"
            "（铁路/航空的直达、历时、票价）并给出选定结论"
        ),
        when_to_use=(
            "需要核对/验证两城之间某天可行班次时：有无直达、耗时与价格量级、"
            "偏好方式是否可行（如出发地小城市需查中转）"
        ),
        params_schema=INTERCITY_INPUT_SCHEMA,
        result_schema=INTERCITY_VERIFY_SCHEMA,
        tools=["train_trip", "train_ticket", "flight_search"],
        review_enabled=True,
        executor="call_llm.planner_agent:intercity_verify_executor",
    ),
}


def get_skill_spec(name: str) -> Optional[SkillSpec]:
    """按技能名（不带 ``skill__`` 前缀）查注册表；未注册返回 None。"""
    return SKILL_SPECS.get(name)


def skill_names() -> tuple:
    """已注册的全部技能名（排序）。"""
    return tuple(sorted(SKILL_SPECS))


def validate_skill_spec(spec: SkillSpec) -> List[str]:
    """守卫校验：返回问题列表（空列表 = 通过）。

    检查项（S1 定义，S3 起守卫测试对每条真实条目强约束）：
    - 技能名合法（小写蛇形）、不带 ``skill__`` 前缀、不得与真源工具重名；
    - description / when_to_use 非空（否则外层 agent 无法判断何时用）；
    - params_schema 为 JSON object 形状（properties 为 dict）；
    - tools 子工具名必须落在 ``TOOL_SPECS`` 注册表内（工具面隔离的前提）；
    - 已注册技能必须接 executor 点路径（``SkillRunner`` 按它执行）。
    """
    problems: List[str] = []
    name = spec.name
    if not name:
        problems.append("技能名不能为空")
    else:
        if name.startswith(SKILL_PREFIX):
            problems.append(f"技能名 {name!r} 不应带 {SKILL_PREFIX} 前缀（注册表存裸名）")
        if name in TOOL_SPECS:
            problems.append(f"技能名 {name!r} 与真源工具重名（真源工具名保留）")
        if not _NAME_RE.match(name):
            problems.append(f"技能名 {name!r} 非法（需小写蛇形 ^[a-z_][a-z0-9_]*$）")
    if not spec.description:
        problems.append(f"{name!r} 缺 description（外层 agent 摘要需要）")
    if not spec.when_to_use:
        problems.append(f"{name!r} 缺 when_to_use（外层 agent 判断何时调用的依据）")
    if not spec.executor:
        problems.append(f"{name!r} 缺 executor 点路径（SkillRunner 按它惰性导入执行）")
    elif ":" not in spec.executor or not spec.executor.partition(":")[0] \
            or not spec.executor.partition(":")[2]:
        problems.append(f"{name!r} executor 需为 module:attr 点路径，实际: {spec.executor!r}")
    schema = spec.params_schema
    if not isinstance(schema, dict) or schema.get("type") != "object":
        problems.append(f"{name!r} params_schema 需为 JSON object schema（type=object）")
    elif not isinstance(schema.get("properties"), dict):
        problems.append(f"{name!r} params_schema.properties 需为 dict")
    if spec.result_schema is not None and (
        not isinstance(spec.result_schema, dict)
        or spec.result_schema.get("type") != "object"
    ):
        problems.append(f"{name!r} result_schema 需为 JSON object schema 或 None")
    for tool in spec.tools:
        if tool not in TOOL_SPECS:
            problems.append(
                f"{name!r} 子工具 {tool!r} 未注册（tools 必须落在 TOOL_SPECS 内）"
            )
    return problems
