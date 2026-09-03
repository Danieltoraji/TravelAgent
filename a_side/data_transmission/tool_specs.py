"""ToolSpec 注册表（架构整理方案 P3-E：工具名/适配器/额度元数据单点定义）。

落点：ToolSpec 注册表在 A 侧、以 A 的类型为权威——B 侧 ``tools/`` 继续是
真源实现，只按这里的接口签名对齐（方案 §三 P3 拍板）。本模块集中描述 A 侧
消费的全部 B 工具，替代散落在 ``live_data.py`` / ``b_planner_hook.py`` 的
裸工具名字符串与预算默认值：

- ``TOOL_SPECS``：工具名 → ``ToolSpec`` 注册表（7 个 B 工具）；
- 预算默认值 / 缓存标志 / 适配器公开名都只在本表定义一次，工厂与挂载点
  引用注册表（``get_tool_spec(name)`` / ``tool_names()``），不再各自硬编码。

每个 ``ToolSpec``：
- ``name``：B 侧工具名（scenic / map / hotel / food / train_trip /
  train_ticket / flight_search）；
- ``description``：用途说明（给人看）；
- ``adapter``：B ToolResult → A 内部类型的适配器公开名（adapters.py 的
  normalize_* / pick_* / train_candidates_*，None 表示无独立适配器——
  map/train_trip 是组合降级链的一部分，转换在 live_data 工厂内联）；
- ``budget_default``：额度管家 per-mode 预算默认（None = 不限量）；
- ``cached``：是否走 ``QuotaManager.cached_call`` 的 ``(name,o,d,date)``
  缓存（P3-D2a 主链三真源）；
- ``mode``：关联的 Mode 枚举值（train/air；None = 非城际）；
- ``sanity_hints``：**合理性量尺**（P5.5 改动 4 / P5.6 技能层 S1 新增，
  可选 str）——该工具返回值的「典型合理量级」提示（如 train_trip 单程
  >12h 应怀疑），经 ``to_openai_tools()`` 拼进工具 description 作为 LLM
  审查（P5.5）时的量尺。**只是 hint 不是硬校验**——硬校验仍在策略层，
  双保险。

本表是现状同步（描述线上真实工具名/额度），不是空想目标——工具名必须与
``live_data.py`` 实际 ``tool_provider.call("...", ...)`` 的字符串逐一对应。

**边界声明**：本表 = **A 侧消费面**（A 侧 live_data 适配工厂群实际调用的
7 个 B 工具）。B 侧另有 **chat v2.2 白名单只读工具**（8 个，供 LLM tool
回路用，A 侧编排线不消费）不在本表——本表覆盖不了 B 侧全部工具，别把它
当成「B 侧工具全集」。B 侧 chat 白名单的权威定义在 B 仓库 tools 注册表，
与 A 侧本表按需对齐，不在这里强行合并。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from data_transmission.enums import Mode


class ToolSpec:
    """单个 B 工具的工具层元数据（A 侧权威定义）。"""

    __slots__ = (
        "name",
        "description",
        "adapter",
        "budget_default",
        "cached",
        "mode",
        "sanity_hints",
    )

    def __init__(
        self,
        name: str,
        description: str,
        adapter: Optional[str] = None,
        budget_default: Optional[int] = None,
        cached: bool = False,
        mode: Optional[str] = None,
        sanity_hints: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.adapter = adapter
        self.budget_default = budget_default
        self.cached = cached
        self.mode = mode
        self.sanity_hints = sanity_hints

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return (
            f"ToolSpec(name={self.name!r}, adapter={self.adapter!r}, "
            f"budget_default={self.budget_default!r}, cached={self.cached})"
        )


# ---------------------------------------------------------------------------
# 注册表（7 个 B 工具；与 live_data.py 实际调用的工具名逐一对应）
# ---------------------------------------------------------------------------

TOOL_SPECS: Dict[str, ToolSpec] = {
    "scenic": ToolSpec(
        "scenic",
        "景点搜索（城市 → 候选景点列表，坐标/时长/标签）",
        adapter="normalize_live_spot",
        budget_default=None,
        cached=False,
    ),
    "map": ToolSpec(
        "map",
        "高德转场/矩阵（action=route 单点 / action=batch_route 整矩阵）",
        adapter=None,  # 转换在 live_data 工厂内联（_coord_str/_minutes_from_payload）
        budget_default=None,
        cached=False,
    ),
    "hotel": ToolSpec(
        "hotel",
        "酒店搜索（城市 → 候选酒店，真实坐标/价格）",
        adapter="normalize_hotel",
        budget_default=None,
        cached=False,
    ),
    "food": ToolSpec(
        "food",
        "餐厅搜索（城市/坐标 → 候选餐厅，含附近锚点池）",
        adapter="normalize_restaurant",
        budget_default=None,
        cached=False,
    ),
    "train_trip": ToolSpec(
        "train_trip",
        "12306 城际火车（站名解析健壮 + 二等座真票价；城市对 → 代表边）",
        adapter=None,  # 转换在 live_data 工厂内联（_train_candidates_from_payload 辅助）
        budget_default=6,
        cached=True,
        mode=Mode.TRAIN.value,
        sanity_hints=(
            "合理性量尺（供审查，非硬校验）：相邻城市 30-120min、跨省 2-8h；"
            "单程超过 12h 通常意味着该方向无铁路直达、只剩驾驶/绕行边，应怀疑"
            "并考虑换 flight_search 或换站名重查。"
        ),
    ),
    "train_ticket": ToolSpec(
        "train_ticket",
        "12306 余票/时刻（多车次 + candidates 全量透传）",
        adapter="pick_representative_edge",
        budget_default=6,
        cached=True,
        mode=Mode.TRAIN.value,
        sanity_hints=(
            "合理性量尺（供审查，非硬校验）：车次历时量级同 train_trip"
            "（相邻城市 30-120min、跨省 2-8h）；二等座票价大体 ¥50-1000，"
            "异常低/异常高应怀疑。"
        ),
    ),
    "flight_search": ToolSpec(
        "flight_search",
        "juhe 聚合航班查询 1962（直飞班次 → 最短历时代表边）",
        adapter="pick_representative_edge",
        budget_default=6,
        cached=True,
        mode=Mode.AIR.value,
        sanity_hints=(
            "合理性量尺（供审查，非硬校验）：800km 以上航线票价大体 ¥300-2000，"
            "¥8 级票价必错；2000km 级航线 3h 内属正常；也可用于交叉验证铁路"
            "结果的耗时/价格量级。"
        ),
    ),
}


def get_tool_spec(name: str) -> Optional[ToolSpec]:
    """按工具名查注册表；未注册返回 None（兼容旧散落字符串不炸）。"""
    return TOOL_SPECS.get(name)


def tool_names() -> tuple:
    """已注册的全部 B 工具名（排序，便于验收/探针）。"""
    return tuple(sorted(TOOL_SPECS))


def intercity_mode_budget() -> Dict[str, int]:
    """城际三真源的 per-mode 预算默认（供 make_live_intercity_provider 用）。

    只收录注册表里 ``budget_default`` 非 None 的城际工具（train_trip /
    train_ticket / flight_search）——与 live_data 现状默认
    ``{"train_trip": 6, "train_ticket": 6, "flight_search": 6}`` 一致。
    """
    return {
        spec.name: spec.budget_default
        for spec in TOOL_SPECS.values()
        if spec.budget_default is not None
    }


# ---------------------------------------------------------------------------
# P5.2：ToolSpec 注册表 → OpenAI function calling tools（LLM agent loop 接线）
# ---------------------------------------------------------------------------

# 各工具的最小入参 Schema（P5.2 给 LLM 的工具面；城际校验子链路只暴露城际三工具，
# 其余工具仍可经 PlannerAgent 的任意子链路使用）
_TOOL_INPUT_SCHEMAS: Dict[str, Dict[str, Any]] = {
    "scenic": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，如 北京"},
        },
        "required": ["city"],
    },
    "map": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "description": "route 单点 / batch_route 矩阵"},
            "from": {"type": "string", "description": "起点坐标或地名"},
            "to": {"type": "string", "description": "终点坐标或地名"},
            "from_city": {"type": "string", "description": "起点城市（batch_route）"},
            "to_city": {"type": "string", "description": "终点城市（batch_route）"},
            "pairs": {"type": "array", "items": {"type": "object"},
                      "description": "坐标对列表（batch_route）"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["action"],
    },
    "hotel": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，如 北京"},
            "hotel_name": {"type": "string", "description": "按名称精确查找"},
        },
        "required": ["city"],
    },
    "food": {
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "城市名，如 北京"},
            "longitude": {"type": "number"},
            "latitude": {"type": "number"},
            "num": {"type": "integer", "description": "返回数量"},
        },
        "required": ["city"],
    },
    "train_trip": {
        "type": "object",
        "properties": {
            "from_city": {"type": "string", "description": "出发城市"},
            "to_city": {"type": "string", "description": "到达城市"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["from_city", "to_city", "date"],
    },
    "train_ticket": {
        "type": "object",
        "properties": {
            "from_city": {"type": "string", "description": "出发城市"},
            "to_city": {"type": "string", "description": "到达城市"},
            "from_station": {"type": "string", "description": "出发站名"},
            "to_station": {"type": "string", "description": "到达站名"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["from_city", "to_city", "date"],
    },
    "flight_search": {
        "type": "object",
        "properties": {
            "from_city": {"type": "string", "description": "出发城市"},
            "to_city": {"type": "string", "description": "到达城市"},
            "date": {"type": "string", "description": "YYYY-MM-DD"},
        },
        "required": ["from_city", "to_city", "date"],
    },
}


def to_openai_tools(
    names: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """ToolSpec 注册表 → OpenAI function calling tools 格式（P5.2）。

    供 ``LLMClient.generate(tools=...)`` 直接消费（``PlannerAgent`` 的 agent
    loop）；executor 由调用方以 ``tool_executor(name, arguments)`` 注入
    （推荐 B 侧 ``ToolProvider.call_json``，白名单已在其 ``list_for_llm`` 收口）。

    ``names`` 缺省 = 全部已注册工具；传子集名（如城际三工具）时只转这些。
    """
    selected = (
        [n for n in names if n in TOOL_SPECS]
        if names is not None
        else list(TOOL_SPECS)
    )
    tools: List[Dict[str, Any]] = []
    for name in sorted(selected):
        spec = TOOL_SPECS[name]
        description = spec.description
        if spec.sanity_hints:
            # P5.5 改动 4：量尺拼进工具 description，作为 LLM 审查时的参照。
            # 只改给 LLM 看的文案，不改 A 侧消费面 description（守旧行为）。
            description = f"{description}\n{spec.sanity_hints}"
        tools.append({
            "type": "function",
            "function": {
                "name": spec.name,
                "description": description,
                "parameters": _TOOL_INPUT_SCHEMAS.get(
                    name, {"type": "object", "properties": {}}
                ),
            },
        })
    return tools
