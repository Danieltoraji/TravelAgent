# 契约锚点副本（A 仓库）：内容与 B 仓库 core/schemas.py 保持一致。
# 同步来源：TravelAgent-main@72b4f0d（2026-08-21，B 的 master 分支）。
# 维护约定（README §6.4）：任何字段改动前先与 B 对齐并递增版本号，
# 禁止单方面修改本文件；联调以本文件 = B 仓库文件为准。
# 8.30 预算口径：本文件已并入 B 侧 B2 扩展（Place.restaurant_name/cuisine/
# average_cost），并新增预算字段（Place.guide_price、TripTimeline.cost_breakdown
# /total_cost 口径）；改后必须同步回 B 仓库 core/schemas.py（A→B 不 vendor core/）。
"""全项目共享的 JSON 接口契约 (Interface Contracts)。

这些数据结构是 A(智能决策)/B(工具与执行)/C(产品与展示) 之间的对齐锚点，
对应《任务整理.md》中"技术负责人需要定义 JSON 接口格式"的要求。

设计决策：
- 只依赖标准库，用 dataclass + asdict 实现序列化，核心代码零第三方依赖；
- 未来若需校验/文档化，可平滑迁移到 pydantic（字段名不变即可）。
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import date, datetime, time
from enum import Enum
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

class ToolStatus(str, Enum):
    """统一 Tool 返回状态。"""
    OK = "ok"
    ERROR = "error"
    NO_DATA = "no_data"


class EventType(str, Enum):
    """Monitor 监控的事件类型。"""
    WEATHER = "weather"
    TRAFFIC = "traffic"
    SCENIC = "scenic"
    FOOD = "food"
    BOOKING = "booking"
    CALENDAR = "calendar"


class ActionStatus(str, Enum):
    """Action Queue 中一项动作的状态（C 负责展示/确认）。"""
    PENDING = "pending"          # 待用户确认
    APPROVED = "approved"        # 已确认，待执行
    EXECUTED = "executed"        # 已执行
    REJECTED = "rejected"        # 用户拒绝
    BLOCKED = "blocked"          # 禁止执行（如付款）


class PermissionLevel(str, Enum):
    """权限等级（对应 Permission Manager 三档决策）。"""
    AUTO = "auto"                # 直接执行（查询类）
    CONFIRM = "confirm"          # 加入 Action Queue，等待用户确认后执行
    MANUAL = "manual"            # 提醒用户自己执行（如付款）


class BookingStatus(str, Enum):
    """预约状态机状态。"""
    DRAFT = "draft"
    PENDING_CONFIRM = "pending_confirm"
    SUBMITTED = "submitted"
    CONFIRMED = "confirmed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# ---------------------------------------------------------------------------
# 序列化工具
# ---------------------------------------------------------------------------

def _json_default(obj: Any) -> Any:
    """json.dumps 默认转换器：处理 Enum / datetime / 嵌套 dataclass。"""
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, (datetime, date, time)):
        return obj.isoformat()
    if is_dataclass(obj):
        return asdict(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


def to_dict(obj: Any) -> Any:
    """把 (可能是 dataclass 的) 对象递归转换为纯 dict。"""
    if is_dataclass(obj):
        return asdict(obj)
    return obj


def to_json(obj: Any, indent: int = 2) -> str:
    """把对象序列化为 JSON 字符串（utf-8，不转义中文）。"""
    return json.dumps(to_dict(obj), ensure_ascii=False, indent=indent, default=_json_default)


# ---------------------------------------------------------------------------
# 工具层契约
# ---------------------------------------------------------------------------

@dataclass
class ToolResult:
    """所有 Tool 的统一返回结构（人物B 的工具层锚点）。"""
    tool: str
    status: ToolStatus
    data: Any = None
    error: Optional[str] = None
    source: str = "mock"            # mock / real_api
    elapsed_ms: float = 0.0
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    def to_json(self) -> str:
        return to_json(self)

@dataclass
class ToolSpec:
    """工具元数据契约：供 A 侧 LLM 理解并直接调用 B 封装的工具。

    - ``input_schema`` 直接复用各 Tool 的 JSON Schema 风格入参说明；
    - ``readonly`` 标记该工具是否只读；有副作用工具（如 booking）默认不应让 LLM 直调。

    P0 三轴分类（0829，见 docs/tool_encapsulation_design_20260828.md §2）：
    - ``domain``  领域轴：weather / map / traffic / scenic / food / hotel /
      booking / train / web（代码组织与配置开关归属）；
    - ``kind``    层次轴：atomic（原子工具）/ skill（意图级组合）/ internal（管道）；
    - ``safety``  安全轴：query（只读，可进 LLM 白名单）/ action（副作用，必须过
      权限闸）——readonly 为其推导属性（safety=="query"）；
    - ``output_schema`` 出参契约（意图级单位：分钟/元/布尔，别名链收敛依据）；
    - ``internal_actions`` 多动作工具中的内部管道动作名（如 map.batch_route）——
      含此项的工具不进 LLM 白名单。
    """
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    readonly: bool = True
    source: str = "mock"            # mock / live
    domain: str = "general"
    kind: str = "atomic"            # atomic / skill / internal
    safety: str = "query"           # query / action
    output_schema: Dict[str, Any] = field(default_factory=dict)
    internal_actions: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)

    def to_json(self) -> str:
        return to_json(self)



# ---------------------------------------------------------------------------
# Planner / Route Planner 契约（A 负责产出，B 据此消费）
# ---------------------------------------------------------------------------

@dataclass
class PlannerOutput:
    """模块1 Planner 的输出契约。"""
    city: str
    days: int
    budget: float
    interests: List[str] = field(default_factory=list)
    avoid: List[str] = field(default_factory=list)


@dataclass
class Place:
    """行程中的一个地点。"""
    id: str = ""                    # 景点 ID（如 "BJ_001"），供 A/C 引用
    name: str = ""                  # 景点名称，B 内部用于 Tool 调用
    lat: float = 0.0
    lng: float = 0.0
    category: str = "scenic"        # scenic / food / hotel / transport / shopping
    arrival: str = "09:00"          # 到达时间 HH:MM
    end_time: str = ""              # 离开时间 HH:MM（对应 A/C 的 end 字段）
    open_time: str = "09:00-17:00"
    queue_min: int = 0
    ticket_required: bool = False
    price: float = 0.0              # 门票单价（scenic）/ 每晚房费（hotel）
    guide_price: float = 0.0        # 讲解费单价（每人，scenic；无讲解为 0）
    # B2 Place 契约扩展（0828）：meal 段餐厅详情。plan 层 RestaurantResolver
    # 已产出这三项（见 a_side/algorithoms/timeline.py add_meal），此前在
    # plan_to_trip_timeline 转换时被丢弃。非 meal 段保持默认空值，
    # 旧消费方（asdict 自动透出、逆向转换忽略新字段）完全向后兼容。
    restaurant_name: str = ""       # 餐厅名（meal 段 name 仍是「午餐/晚餐」餐段类型）
    cuisine: List[str] = field(default_factory=list)  # 菜系标签
    average_cost: float = 0.0       # 人均消费（元）
    # 批次 2（城际来去程）：transport 段透传结构化信息（mode / from_station /
    # to_station / cost_per_person / source / legs 市内衔接预留）——非 transport
    # 段保持默认空 dict，旧消费方（asdict 自动透出、逆向转换忽略）向后兼容。
    details: Dict[str, Any] = field(default_factory=dict)
    # E6（0828）：预约关联——自动/人工预约成功后回填，C 端时间轴可表达
    # "已预约/已确认"。默认空 = 未预约，旧数据完全向后兼容。
    booking_id: str = ""            # 关联 BookingRecord（如 "A1B2C3D4"）
    booking_status: str = ""        # pending_confirm / submitted / confirmed…（BookingStatus.value）


@dataclass
class DayPlan:
    """一天的行程。"""
    day: int
    date: date
    items: List[Place] = field(default_factory=list)


@dataclass
class TripTimeline:
    """行程时间轴（模块2 Route Planner 的输出契约）。

    人物B 的 Execution Agent 据此契约驱动持续监控。
    """
    id: str = ""                    # 计划 ID（如 "plan_001"）
    city: str = ""
    start_date: date = field(default_factory=lambda: date.today())
    end_date: date = field(default_factory=lambda: date.today())
    days: List[DayPlan] = field(default_factory=list)
    total_cost: float = 0.0        # 总费用（目的地内口径：门票 + 讲解 + 酒店 + 餐饮）
    # 费用明细（口径与 total_cost 一致，供 C 端拆分展示）：
    #   ticket 门票 / guide 讲解 / hotel 酒店（房费）/ meal 餐饮（人均 × 人数）
    cost_breakdown: Dict[str, float] = field(default_factory=dict)
    walking_distance: float = 0.0  # 总步行距离（km）


# ---------------------------------------------------------------------------
# Monitor / Decision 契约（B → A）
# ---------------------------------------------------------------------------

@dataclass
class MonitorEvent:
    """Monitor Scheduler 产出的一次观测事件，回传给 Decision Engine。"""
    event_id: str
    event_type: EventType
    place: str                     # 景点名称（B 内部用）
    observed_at: datetime
    rule_name: str
    spot_id: str = ""              # 景点 ID（供 A/C 引用）
    data: Any = None
    impact_score: float = 0.0       # 由 A 的 Decision Engine 评估，B 侧默认 0

    def to_dict(self) -> Dict[str, Any]:
        return to_dict(self)


@dataclass
class DecisionRequest:
    """B → A(Decision Engine) 的决策请求契约。

    当 Execution Agent 判定某次观测达到影响阈值时组装本请求，
    交给 A 的 Decision Engine 评估"是否需要重新规划"。
    """
    events: List[MonitorEvent]
    current_timeline: TripTimeline
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReplanRequest:
    """A → B / C 的重规划结果契约（A 产出，B 据此更新执行计划）。"""
    new_timeline: Optional[TripTimeline] = None
    reason: str = ""
    diff_summary: List[str] = field(default_factory=list)   # 修改点说明（Explainable）
    need_replan: bool = True        # 是否需要重规划（B 侧：返回 ReplanRequest 即视为 True）
    impact: float = 0.0            # 影响评分（0-1）
    affected_spots: List[str] = field(default_factory=list)  # 受影响的景点 ID 列表


# ---------------------------------------------------------------------------
# Action / Booking 契约（B → C）
# ---------------------------------------------------------------------------

@dataclass
class ActionItem:
    """Action Queue 的一项（供 C 展示/确认，对应 Action Queue + Permission）。"""
    action_id: str
    title: str
    description: str = ""
    status: ActionStatus = ActionStatus.PENDING
    permission: PermissionLevel = PermissionLevel.AUTO
    target: str = ""               # 例如 "booking:ABC123", "calendar:update"
    created_at: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))
    # 以下字段供 C 消费（对齐 A/C 数据格式）
    type: str = ""                 # 动作类型，如 "BOOK_TICKET"
    date: str = ""                 # 目标日期 YYYY-MM-DD
    quantity: int = 0              # 数量（如购票张数）
    # 审计（E5）：动作被 approve/reject 的时间与操作方（单用户 Demo 固定 "c_end_user"）
    decided_at: str = ""
    decided_by: str = ""


# E8（2026-08-28）：删除无引用的 BookingRequest 占位——真实需求由
# BookingManager.prepare + 两段式动作承载（见 docs/tool_encapsulation_design_20260828.md）