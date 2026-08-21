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
    """
    name: str
    description: str
    input_schema: Dict[str, Any] = field(default_factory=dict)
    readonly: bool = True
    source: str = "mock"            # mock / live

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
    price: float = 0.0


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
    total_cost: float = 0.0        # 总费用
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


@dataclass
class BookingRequest:
    """预约请求（只准备，不付款）。"""
    place: str
    target_date: str                # YYYY-MM-DD
    party_size: int = 1
    note: str = ""
