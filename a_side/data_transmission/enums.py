"""架构口径统一模块（P1，2026-09-02）。

**为什么存在**：`source`/`mode` 曾散落全仓字符串字面量，存在两种拼写
（estimate/estimated、rail/train）与多套语义混用。本模块集中定义全部
枚举，全仓一律 ``import`` 引用，杜绝再手写字面量。

**口径（与 架构整理/架构整理.md §5 一致）**：

1. ``Source``：**逐段数据来源**，收敛为 4 种
   ``live / estimated / mock / demo_fixture``；
   - 旧 ``estimate`` → ``estimated``（拼写归并）；
   - 旧 ``fake`` / ``""``（假表旧边）→ ``mock``（对齐 B 侧 ToolResult.source）；
   - ``live_fallback`` / ``mixed`` 不再进逐段 source，由 ``PipelineSource``
     状态字段与 legs 聚合表达；
2. ``Mode``：**工具/Edge 语义**，统一 ``train / air / driving``；
   - 偏好层 ``rail``（用户输入枚举）保留为 ``Preference.RAIL``，
     进工具层一律映射成 ``Mode.TRAIN``（rail 是偏好语义、train 是工具语义）；
3. ``Preference``：C 端 ``travel_priority`` 用户输入枚举
   ``rail / air / speed / earliest / cost``；
4. ``PipelineSource``：**整链回退状态字段**（``BPlannerHook.last_data_source``）
   ``fake / live / live_fallback``——记录「这次规划最终用的数据源」，
   是 hook 级监控状态，不是逐段 source。
"""

from __future__ import annotations

from enum import Enum


class Source(str, Enum):
    """逐段数据来源（Edge.source / Spot.source / leg.source）。"""

    LIVE = "live"                # 真源
    ESTIMATED = "estimated"      # 估算表（旧拼写 estimate 已归并）
    MOCK = "mock"                # mock/假表旧边（对齐 B 侧 ToolResult.source）
    DEMO_FIXTURE = "demo_fixture"  # Demo 固定场景


class Mode(str, Enum):
    """工具/Edge 语义：mode 统一 train/air/driving（rail 属偏好，见 Preference）。"""

    TRAIN = "train"
    AIR = "air"
    DRIVING = "driving"


class Preference(str, Enum):
    """C 端 travel_priority 用户输入枚举（偏好语义，进工具层映射成 Mode）。"""

    RAIL = "rail"                # 高铁优先
    AIR = "air"                  # 飞机优先
    SPEED = "speed"              # 速度最快（当前与 earliest 等价）
    EARLIEST = "earliest"        # 最早到达
    COST = "cost"                # 人均费用最低


class PipelineSource(str, Enum):
    """整链回退状态字段（BPlannerHook.last_data_source）。"""

    FAKE = "fake"                # 假数据管线
    LIVE = "live"                # 真源
    LIVE_FALLBACK = "live_fallback"  # 真源失败整链回退假源
