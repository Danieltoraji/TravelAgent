"""技能基类：面向意图的组合工具（P2，见 docs/tool_encapsulation_design_20260828.md §3）。

Skill = BaseTool 子类——构造注入其他工具/客户端实例并组合调用，**不是新运行时**：
照常注册进 ToolRegistry、返回 ToolResult、支持 Mock/Live 双版本。

与原子工具（atomic）的区别：
- 输出为意图级结构（output_schema 声明，单位统一为分钟/元/布尔）；
- 领域知识（单位换算、选班次、排序规则）内聚在 ``_run``，消费方零重复；
- 白名单：query-skill 自动进入 ``ToolProvider.list_for_llm()``。
"""

from __future__ import annotations

from tools.base_tool import BaseTool


class Skill(BaseTool):
    """意图级组合工具基类。子类约定：

    - 构造函数注入被组合的工具实例或共享 client；
    - ``_run`` 内组合调用并聚合为单一意图结果；
    - 每个内层调用单独判 ``ToolStatus``，单段失败降级为空段而非整体失败
      （聚合视图的健壮性优先于完整性）。
    """

    kind = "skill"
