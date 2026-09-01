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
- ``mode``：关联的 Mode 枚举值（train/air；None = 非城际）。

本表是现状同步（描述线上真实工具名/额度），不是空想目标——工具名必须与
``live_data.py`` 实际 ``tool_provider.call("...", ...)`` 的字符串逐一对应。

**边界声明**：本表 = **A 侧消费面**（A 侧 live_data 适配工厂群实际调用的
7 个 B 工具）。B 侧另有 **chat v2.2 白名单只读工具**（8 个，供 LLM tool
回路用，A 侧编排线不消费）不在本表——本表覆盖不了 B 侧全部工具，别把它
当成「B 侧工具全集」。B 侧 chat 白名单的权威定义在 B 仓库 tools 注册表，
与 A 侧本表按需对齐，不在这里强行合并。
"""

from __future__ import annotations

from typing import Dict, Optional

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
    )

    def __init__(
        self,
        name: str,
        description: str,
        adapter: Optional[str] = None,
        budget_default: Optional[int] = None,
        cached: bool = False,
        mode: Optional[str] = None,
    ):
        self.name = name
        self.description = description
        self.adapter = adapter
        self.budget_default = budget_default
        self.cached = cached
        self.mode = mode

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
    ),
    "train_ticket": ToolSpec(
        "train_ticket",
        "12306 余票/时刻（多车次 + candidates 全量透传）",
        adapter="pick_representative_edge",
        budget_default=6,
        cached=True,
        mode=Mode.TRAIN.value,
    ),
    "flight_search": ToolSpec(
        "flight_search",
        "juhe 聚合航班查询 1962（直飞班次 → 最短历时代表边）",
        adapter="pick_representative_edge",
        budget_default=6,
        cached=True,
        mode=Mode.AIR.value,
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
