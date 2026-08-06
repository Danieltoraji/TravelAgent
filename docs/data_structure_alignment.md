# 数据结构对齐文档

> **用途**：本文档用于 A/B/C 三方数据结构对齐协商。
> - **与 A/C 协商部分**：B 侧无法单独解决，需要 A/C 配合调整或确认方案
> - **B 内部调整部分**：B 侧可自行实施，不依赖 A/C
>
> **日期**：2026-08-06
> **B 侧代码基线**：`core/schemas.py` + `execution/execution_agent.py` + `booking/booking_manager.py` + `app/service.py`

---

## 目录

- [数据结构对齐文档](#数据结构对齐文档)
  - [目录](#目录)
  - [一、与 A/C 协商项](#一与-ac-协商项)
    - [协商项 1：决策流程步骤数不同](#协商项-1决策流程步骤数不同)
      - [A/C 期望的流程（4 步）](#ac-期望的流程4-步)
      - [B 侧实际流程（2 步）](#b-侧实际流程2-步)
      - [为什么 B 无法改为 4 步](#为什么-b-无法改为-4-步)
      - [协商方案](#协商方案)
    - [协商项 2：TripTimeline 必须携带完整 Place 对象](#协商项-2triptimeline-必须携带完整-place-对象)
      - [A/C 期望的 Plan 格式](#ac-期望的-plan-格式)
      - [B 侧的 TripTimeline 格式](#b-侧的-triptimeline-格式)
      - [为什么 B 无法改为纯 ID 引用](#为什么-b-无法改为纯-id-引用)
      - [B 侧需要的 Place 字段及用途](#b-侧需要的-place-字段及用途)
      - [协商方案](#协商方案-1)
    - [协商项 3：标识方式 — name vs spot\_id](#协商项-3标识方式--name-vs-spot_id)
      - [差异](#差异)
      - [B 侧为什么用 name](#b-侧为什么用-name)
      - [协商方案](#协商方案-2)
    - [协商项 4：Plan 键名 — items vs activities](#协商项-4plan-键名--items-vs-activities)
      - [差异](#差异-1)
      - [B 侧为什么用 items + arrival](#b-侧为什么用-items--arrival)
      - [协商方案](#协商方案-3)
    - [协商项 5：事件类型粒度 — 类别级 vs 具体级](#协商项-5事件类型粒度--类别级-vs-具体级)
      - [差异](#差异-2)
      - [B 侧 EventType 枚举](#b-侧-eventtype-枚举)
      - [B 侧 MonitorEvent.data 实际内容](#b-侧-monitoreventdata-实际内容)
      - [协商方案](#协商方案-4)
    - [协商项 6：ActionItem 字段差异](#协商项-6actionitem-字段差异)
      - [字段对照](#字段对照)
      - [B 侧 PermissionLevel 枚举](#b-侧-permissionlevel-枚举)
      - [协商方案](#协商方案-5)
    - [协商项 7：ActionStatus 枚举值差异](#协商项-7actionstatus-枚举值差异)
      - [差异](#差异-3)
      - [协商方案](#协商方案-6)
    - [协商项 8：decision\_hook 异步支持](#协商项-8decision_hook-异步支持)
      - [问题](#问题)
      - [LLM 延迟的实际影响分析](#llm-延迟的实际影响分析)
      - [协商方案](#协商方案-7)
  - [二、B 内部调整项](#二b-内部调整项)
    - [调整项 1：Place 加 id 字段](#调整项-1place-加-id-字段)
    - [调整项 2：Place 加 end\_time 字段](#调整项-2place-加-end_time-字段)
    - [调整项 3：MonitorEvent 加 spot\_id 字段](#调整项-3monitorevent-加-spot_id-字段)
    - [调整项 4：ActionItem 加 type / date / quantity 字段](#调整项-4actionitem-加-type--date--quantity-字段)
    - [调整项 5：ReplanRequest 加 need\_replan / impact / affected\_spots 字段](#调整项-5replanrequest-加-need_replan--impact--affected_spots-字段)
    - [调整项 6：TripTimeline 加 id / total\_cost / walking\_distance 字段](#调整项-6triptimeline-加-id--total_cost--walking_distance-字段)
  - [三、完整字段对照表](#三完整字段对照表)
    - [Place ↔ A/C 景点信息 / Plan activity](#place--ac-景点信息--plan-activity)
    - [MonitorEvent ↔ A/C 事件变化](#monitorevent--ac-事件变化)
    - [ReplanRequest ↔ A/C 决定](#replanrequest--ac-决定)
    - [ActionItem ↔ A/C 行为](#actionitem--ac-行为)
    - [TripTimeline ↔ A/C 计划](#triptimeline--ac-计划)
  - [附：协商优先级排序](#附协商优先级排序)

---

## 一、与 A/C 协商项

以下 8 项需要 A/C 团队确认或配合，B 侧无法单独解决。

---

### 协商项 1：决策流程步骤数不同

**严重程度**：🔴 架构级

#### A/C 期望的流程（4 步）

```
事件变化
  → 步骤1: Python 预判（硬约束不满足→直接重规划；影响太小→忽略）
  → 步骤2: LLM 判断是否需要重规划（返回 {need_replan, impact, affected_spots}）
  → 步骤3: LLM 从候选路线中选择组合，生成新计划
  → 步骤4: Python 校验（时间冲突、预算超支），不通过则回到步骤3
```

#### B 侧实际流程（2 步）

```
事件变化
  → 步骤1: B 侧预判 _significant()（降雨≥60% / 排队≥50min / 延误≥30min）
  → 步骤2: decision_hook(req) 一次调用，直接返回完整 ReplanRequest（含 new_timeline）
           → apply_replan() 立即应用，无校验环节
```

**B 侧代码位置**：`execution/execution_agent.py` 第 154-180 行

```python
def handle_event(self, event: MonitorEvent) -> Optional[DecisionRequest]:
    if not self._significant(event):       # 步骤1: B 侧预判
        return None
    req = DecisionRequest(events=[event], current_timeline=self.timeline, ...)
    replan = self.decision_hook(req)        # 步骤2: 一次调用拿完整方案
    if isinstance(replan, ReplanRequest):
        self.apply_replan(replan)           # 立即应用，无校验循环
    return req
```

#### 为什么 B 无法改为 4 步

| 需要的改动 | 为什么困难 |
|-----------|-----------|
| 拆成两个 hook（judge + replan） | 破坏现有 `decision_hook` 注入点契约，A 的 `DecisionEngine` stub 也要重写 |
| 增加"候选路线"概念 | 候选路线是 A 的 Route Planner 产出，B 侧完全没有这个数据结构 |
| 增加校验循环（时间冲突、预算超支） | 校验需要用户约束数据（budget、walking_time 等），这些在 `requirement_schema` 中由 A 管理，B 拿不到 |
| `handle_event` 从同步改为异步 | 波及 `MonitorScheduler`、`app/service.py` 所有端点（详见协商项 8） |

#### 协商方案

**方案**：A 的 `decision_hook` 直接返回完整 `ReplanRequest`（含 `new_timeline`），B 侧不做步骤 3-4。

- A 在 LLM 内部自行完成"判断 → 选路线 → 组合计划"全流程
- A 自行做时间冲突/预算校验（A 有用户约束数据）
- B 只负责：预判是否显著 → 调用 `decision_hook` → 应用返回的新时间轴

**LLM 延迟问题**：见 [协商项 8](#协商项-8decision_hook-异步支持)。


**按此方案进行**。

---

### 协商项 2：TripTimeline 必须携带完整 Place 对象

**严重程度**：🔴 架构级

#### A/C 期望的 Plan 格式

```json
{
  "id": "plan_001",
  "days": [{
    "date": "2026-08-10",
    "activities": [
      {"spot_id": "spot_001", "start": "09:00", "end": "12:00"}
    ]
  }],
  "total_cost": 780,
  "walking_distance": 4.2
}
```

特点：**纯 ID 引用**，activity 只有 `spot_id`，不携带景点详细信息。

#### B 侧的 TripTimeline 格式

```python
@dataclass
class Place:
    name: str               # 用名称做标识
    lat: float = 0.0        # 纬度
    lng: float = 0.0        # 经度
    category: str           # scenic / food / hotel / transport
    arrival: str            # 到达时间 HH:MM
    open_time: str          # 营业时间
    queue_min: int          # 当前排队时长
    ticket_required: bool   # 是否需要预约
    price: float            # 票价

@dataclass
class TripTimeline:
    city: str
    start_date: date
    end_date: date
    days: List[DayPlan]     # 用 items，不是 activities
```

#### 为什么 B 无法改为纯 ID 引用

B 的 `ExecutionAgent._build_rules()`（第 108-135 行）直接从 Place 对象读取字段构建监控规则：

```python
for day in self.timeline.days:
    for item in day.items:
        self._place_info[item.name] = item       # 用 name 做键
        if item.category == "scenic":            # 读 category 决定规则类型
            self.lookahead_rules.append(MonitorRule(
                place=item.name,                  # 用 name 做监控目标
                fire_at=self._fire_at(day.date, item.arrival, ...),  # 读 arrival 算触发时间
                call=lambda n=item.name: self._poll(EventType.SCENIC, place=n),
            ))
```

自动预约 `_maybe_auto_book()` 也依赖 Place 完整字段：

```python
place_info = self._place_info.get(rule.place)
if not place_info.ticket_required:     # 读 ticket_required 决定是否预约
    return
```

B 的所有 Tool 都接受**地名**而非 ID：

```python
registry.call("scenic", place="故宫")                    # 高德 POI 搜索
registry.call("traffic", origin="故宫", destination="天坛")  # 高德路线规划
registry.call("food", near="全聚德")                      # 高德餐厅搜索
```

高德 API 和和风天气 API 都不接受 `spot_id`，它们接受地名或坐标。

#### B 侧需要的 Place 字段及用途

| 字段 | 用途 | 如果缺失的后果 |
|------|------|---------------|
| `name` | 调用 Tool、构建监控规则键、自动预约 | 无法监控、无法预约 |
| `lat`, `lng` | 交通 Tool 计算路线 | 无法查交通 |
| `category` | 决定创建哪种监控规则（scenic/food） | 无法区分景点和餐厅 |
| `arrival` | 计算到达前触发的 `fire_at` 时间 | 无法触发到达前检查 |
| `ticket_required` | 决定是否自动预约 | 可能漏预约或误预约 |
| `price` | 预约时填充票价 | 预约信息不完整 |

#### 协商方案

**方案 A（推荐）**：A 发给 B 的计划中，每个 activity 既带 `spot_id`（供 A/C 引用），也带完整 Place 信息（供 B 执行）。即 A 在发送计划前，将 `spot_id` 展开为完整 Place 对象。

**方案 B**：A 发给 B 的计划只有 `spot_id`，但 B 侧增加一个"景点知识库查询"步骤，通过 `spot_id` 查询完整信息。但这需要 B 侧维护景点数据库，与当前架构不符（知识库是 A 的职责）。

**解决：A将会返回完整的 Place 对象**，B 侧不需要维护景点数据库。

---

### 协商项 3：标识方式 — name vs spot_id

**严重程度**：🟡 需协调

#### 差异

| 数据结构 | A/C 期望 | B 侧现状 |
|----------|----------|----------|
| 景点标识 | `spot_id`（如 "BJ_001"） | `name`（如 "故宫"） |
| 事件中的地点 | `spot_id` | `place`（name 字符串） |
| Action 中的目标 | `target: "spot_001"` | `target: "booking:ABC123"` |

#### B 侧为什么用 name

B 的所有 Tool（高德 API、和风天气 API）都接受地名，不接受 ID：

```python
# 高德 POI 搜索 — 传地名
registry.call("scenic", place="故宫")

# 高德路线规划 — 传地名
registry.call("traffic", origin="故宫", destination="天坛")
```

#### 协商方案

B 侧在 `Place`、`MonitorEvent`、`ActionItem` 中加 `id` / `spot_id` 字段（见 B 内部调整项 1/3/4），与 `name` 并存。

- A/C 用 `id` / `spot_id` 做引用
- B 内部用 `name` 调 Tool、构建监控规则
- 两者的映射关系由 A 在生成计划时提供（Place 对象同时包含 `id` 和 `name`）

**A将会返回完整的 Place 对象。**

---

### 协商项 4：Plan 键名 — items vs activities

**严重程度**：🟡 需协调

#### 差异

| 位置 | A/C 期望 | B 侧现状 |
|------|----------|----------|
| 天数列表中的活动项 | `days[].activities[]` | `days[].items[]` |
| 活动项的时间 | `start` + `end` | `arrival`（仅到达时间） |

#### B 侧为什么用 items + arrival

- `items` 是 dataclass 字段名，改名为 `activities` 会波及 `service.py` 的反序列化逻辑、所有测试的 JSON payload、demo 脚本
- `arrival` 是 B 侧计算到达前触发时间（`fire_at`）的依据；B 不需要 `end` 时间（监控在到达前触发，不关心离开时间）

#### 协商方案

**方案 A（推荐）**：A/C 适配 B 的格式，用 `items` + `arrival`。B 侧可加 `end_time` 字段（见 B 内部调整项 2），但不改 `items` 键名。

**方案 B**：B 侧在 `service.py` 的 `POST /timeline` 端点做键名兼容——接受 `activities` 也接受 `items`，内部统一转为 `items`。改动范围小，仅 `service.py` 的反序列化逻辑。

**按照方案B，即仅改变B的代码。**

---

### 协商项 5：事件类型粒度 — 类别级 vs 具体级

**严重程度**：🟡 需协调

#### 差异

| 维度 | A/C 期望 | B 侧现状 |
|------|----------|----------|
| 事件类型字段 | `type: "QUEUE_CHANGE"` | `event_type: "scenic"`（EventType 枚举） |
| 事件类型粒度 | 具体事件（QUEUE_CHANGE / RAIN_STORM / TRAFFIC_JAM） | 类别级（scenic / traffic / weather / food） |
| 时间戳字段 | `timestamp` | `observed_at` |
| 地点字段 | `spot_id` | `place`（name 字符串） |
| 数据字段 | `data: {old_value, new_value}` | `data: Any`（灵活 dict） |

#### B 侧 EventType 枚举

```python
class EventType(str, Enum):
    WEATHER = "weather"
    TRAFFIC = "traffic"
    SCENIC = "scenic"
    FOOD = "food"
    BOOKING = "booking"
    CALENDAR = "calendar"
```

#### B 侧 MonitorEvent.data 实际内容

B 侧 `data` 字段是灵活的 dict，不同事件类型携带不同数据：

| 事件类型 | data 内容 |
|---------|----------|
| `weather` | `{condition, temperature_c, rain_probability, uv_index, wind_kmh, ...}` |
| `scenic` | `{place, queue_min, ticket_required, open_hours, price, ...}` |
| `traffic` | `{origin, destination, mode, duration_min, congestion, delay_min, ...}` |
| `food` | `[{name, price_per_person, tel, address, ...}, ...]`（餐厅列表） |

#### 协商方案

- `event_type` 保持类别级（不改枚举），在 `data` 中加 `subtype` 字段表示具体事件（如 `subtype: "QUEUE_CHANGE"`）
- `observed_at` 可加 `timestamp` 别名（序列化时同时输出两个字段）
- `place` 可加 `spot_id` 字段（见 B 内部调整项 3）
- `data` 字段已兼容 `old_value` / `new_value`，A/C 可直接使用

**给B加字段，不改变A的字段。**

---

### 协商项 6：ActionItem 字段差异

**严重程度**：🟡 需协调

#### 字段对照

| A/C 期望 | B 侧 ActionItem | 差异 |
|----------|-----------------|------|
| `id` | `action_id` | 字段名不同 |
| `type`（"BOOK_TICKET"） | ❌ 无 | **缺失** |
| `target`（"spot_001"） | `target`（"booking:ABC123"） | 引用方式不同 |
| `date` | ❌ 无 | 缺失 |
| `quantity` | ❌ 无 | 缺失 |
| `requires_confirmation`（bool） | `permission`（PermissionLevel 枚举） | 类型不同 |
| `status`（"WAITING_CONFIRMATION"） | `status`（ActionStatus: "pending"） | 枚举值不同 |
| 无 | `title`, `description`, `created_at` | B 多出的字段 |

#### B 侧 PermissionLevel 枚举

```python
class PermissionLevel(str, Enum):
    AUTO = "auto"        # 直接执行（查询类）
    CONFIRM = "confirm"  # 加入 Action Queue，等待用户确认后执行
    MANUAL = "manual"    # 提醒用户自己执行（如付款）
```

#### 协商方案

- B 侧加 `type`、`date`、`quantity` 字段（见 B 内部调整项 4）
- `requires_confirmation` ↔ `permission` 的映射：`permission == CONFIRM` 等价于 `requires_confirmation == true`；`permission == MANUAL` 等价于 `requires_confirmation == true`（但需人工执行）；`permission == AUTO` 等价于 `requires_confirmation == false`
- `id` ↔ `action_id`：C 侧适配 `action_id`，或 B 侧序列化时加 `id` 别名
- `target` 引用方式：B 侧 `target` 格式为 `"booking:{booking_id}"`，C 侧可解析冒号后的 ID

**改B的字段，不改变A的字段。**

---

### 协商项 7：ActionStatus 枚举值差异

**严重程度**：🟡 需协调

#### 差异

| A/C 期望 | B 侧 ActionStatus | 语义 |
|----------|-------------------|------|
| `"WAITING_CONFIRMATION"` | `"pending"` | 待用户确认 |
| `"CONFIRMED"` | `"approved"` | 已确认，待执行 |
| `"EXECUTED"` / `"COMPLETED"` | `"executed"` | 已执行 |
| `"REJECTED"` / `"CANCELLED"` | `"rejected"` | 用户拒绝 |
| 无 | `"blocked"` | 禁止执行（如付款） |

#### 协商方案

- C 侧适配 B 的枚举值（`pending` / `approved` / `executed` / `rejected` / `blocked`）
- 或 B 侧在序列化时做映射（但会增加复杂度，不推荐）

**改B的字段，不改变A的字段。**

---

### 协商项 8：decision_hook 异步支持

**严重程度**：🟡 需协调（与协商项 1 相关）

#### 问题

A 的 `decision_hook` 如果内部调用 LLM（网络请求），可能耗时 5-15 秒。当前 `decision_hook` 是**同步阻塞调用**，会阻塞 asyncio 事件循环。

#### LLM 延迟的实际影响分析

```
run_forever()                          ← async 主循环
├── scheduler.start(handler)           ← 独立 asyncio.Task，每 5-30min 轮询
│   └── _tick() → on_event(event)     ← 只推送给 C，不触发决策
│
└── while True:                        ← 主循环，每 1 秒
    ├── check_lookahead(now)           ← 检查到达前规则
    │   └── handle_event(event)        ← 仅当规则到 fire_at 时触发
    │       ├── on_event(event)       ← C 先收到事件（在 decision_hook 之前）
    │       ├── _significant(event)   ← 不显著直接 return
    │       └── decision_hook(req)     ← 只在显著时调用 A（突发事件）
    └── await asyncio.sleep(1)
```

**关键结论**：

1. `decision_hook` 只在突发事件时触发（降雨≥60% / 排队≥50min / 延误≥30min），不是常规轮询
2. 异步轮询（`_tick`）是独立 `asyncio.Task`，但同步 `decision_hook` 会阻塞事件循环，导致 `_tick` 也卡住
3. C 的 `on_event` 在 `decision_hook` 之前执行，前端会立即收到事件通知

#### 协商方案

**方案 A（B 侧改，推荐）**：让 `decision_hook` 支持返回协程，B 侧 `await` 调用：

**按方案A进行。**

```python
# handle_event 改为 async
replan = self.decision_hook(req)
if asyncio.iscoroutine(replan):
    replan = await replan
```

改动范围：`handle_event` → `async def`，`check_lookahead` → `async def`，`poll_once` → `async def`，`app/service.py` 两个端点加 `async`。改动量中等，不影响接口契约。

**方案 B（A 侧自己解决）**：A 在 `decision_hook` 内部用 `asyncio.to_thread()` 包装同步 LLM 调用：

```python
# A 的 decision_hook 实现
def __call__(self, req):
    return asyncio.to_thread(self._call_llm_sync, req)
```

B 侧完全不用改，但 A 需要处理线程池。

**建议**：采用方案 A，B 侧改为支持异步 `decision_hook`，A 可自由选择同步或异步实现。

---

## 二、B 内部调整项

以下 6 项 B 侧可自行实施，加可选字段不破坏现有逻辑，不需要 A/C 配合。

---

### 调整项 1：Place 加 id 字段

**当前**：

```python
@dataclass
class Place:
    name: str
    lat: float = 0.0
    lng: float = 0.0
    category: str = "scenic"
    arrival: str = "09:00"
    open_time: str = "09:00-17:00"
    queue_min: int = 0
    ticket_required: bool = False
    price: float = 0.0
```

**调整后**：

```python
@dataclass
class Place:
    id: str = ""                    # 新增：景点 ID（如 "BJ_001"），供 A/C 引用
    name: str = ""                  # 景点名称，B 内部用于 Tool 调用
    lat: float = 0.0
    lng: float = 0.0
    category: str = "scenic"
    arrival: str = "09:00"
    end_time: str = ""              # 新增：离开时间（见调整项 2）
    open_time: str = "09:00-17:00"
    queue_min: int = 0
    ticket_required: bool = False
    price: float = 0.0
```

**影响范围**：
- `core/schemas.py`：Place 加字段
- `app/service.py`：`POST /timeline` 反序列化加 `id` 读取
- `execution/execution_agent.py`：`_build_rules()` 中 `_place_info` 可同时按 `id` 和 `name` 索引
- 现有测试：不受影响（新字段有默认值）

---

### 调整项 2：Place 加 end_time 字段

**当前**：Place 只有 `arrival`（到达时间），无离开时间。

**调整后**：加 `end_time: str = ""`，对应 A/C 期望的 `end` 字段。

**影响范围**：
- `core/schemas.py`：Place 加字段
- `app/service.py`：反序列化加 `end_time` 读取
- 现有测试：不受影响

---

### 调整项 3：MonitorEvent 加 spot_id 字段

**当前**：

```python
@dataclass
class MonitorEvent:
    event_id: str
    event_type: EventType
    place: str                     # 景点名称
    observed_at: datetime
    rule_name: str
    data: Any = None
    impact_score: float = 0.0
```

**调整后**：

```python
@dataclass
class MonitorEvent:
    event_id: str
    event_type: EventType
    place: str                     # 保留：景点名称（B 内部用）
    spot_id: str = ""              # 新增：景点 ID（供 A/C 引用）
    observed_at: datetime
    rule_name: str
    data: Any = None
    impact_score: float = 0.0
```

**影响范围**：
- `core/schemas.py`：MonitorEvent 加字段
- `monitor/monitor_scheduler.py`：`emit()` 方法从 `rule` 中读取 `spot_id`（需 MonitorRule 也加 `spot_id` 字段）
- `execution/execution_agent.py`：`_build_rules()` 构建 MonitorRule 时传入 `spot_id`
- 现有测试：不受影响

---

### 调整项 4：ActionItem 加 type / date / quantity 字段

**当前**：

```python
@dataclass
class ActionItem:
    action_id: str
    title: str
    description: str = ""
    status: ActionStatus = ActionStatus.PENDING
    permission: PermissionLevel = PermissionLevel.AUTO
    target: str = ""
    created_at: str = ...
```

**调整后**：

```python
@dataclass
class ActionItem:
    action_id: str
    title: str
    description: str = ""
    status: ActionStatus = ActionStatus.PENDING
    permission: PermissionLevel = PermissionLevel.AUTO
    target: str = ""
    created_at: str = ...
    # 新增字段（供 C 消费）
    type: str = ""                 # 动作类型，如 "BOOK_TICKET"
    date: str = ""                 # 目标日期 YYYY-MM-DD
    quantity: int = 0              # 数量（如购票张数）
```

**影响范围**：
- `core/schemas.py`：ActionItem 加字段
- `booking/booking_manager.py`：`prepare()` 和 `payment_action()` 构建 ActionItem 时填充新字段
- 现有测试：不受影响

---

### 调整项 5：ReplanRequest 加 need_replan / impact / affected_spots 字段

**当前**：

```python
@dataclass
class ReplanRequest:
    new_timeline: Optional[TripTimeline] = None
    reason: str = ""
    diff_summary: List[str] = field(default_factory=list)
```

**调整后**：

```python
@dataclass
class ReplanRequest:
    new_timeline: Optional[TripTimeline] = None
    reason: str = ""
    diff_summary: List[str] = field(default_factory=list)
    # 新增字段（供 C 展示决策信息）
    need_replan: bool = True        # 是否需要重规划（B 侧：返回 ReplanRequest 即视为 True）
    impact: float = 0.0            # 影响评分（0-1）
    affected_spots: List[str] = field(default_factory=list)  # 受影响的景点 ID 列表
```

**影响范围**：
- `core/schemas.py`：ReplanRequest 加字段
- `decision/decision_engine.py`：`_replan()` 方法填充新字段
- `execution/execution_agent.py`：`handle_event()` 可将 `affected_spots` 传给 C
- 现有测试：不受影响

---

### 调整项 6：TripTimeline 加 id / total_cost / walking_distance 字段

**当前**：

```python
@dataclass
class TripTimeline:
    city: str
    start_date: date
    end_date: date
    days: List[DayPlan] = field(default_factory=list)
```

**调整后**：

```python
@dataclass
class TripTimeline:
    id: str = ""                   # 新增：计划 ID（如 "plan_001"）
    city: str = ""
    start_date: date = ...
    end_date: date = ...
    days: List[DayPlan] = field(default_factory=list)
    total_cost: float = 0.0       # 新增：总费用
    walking_distance: float = 0.0  # 新增：总步行距离（km）
```

**影响范围**：
- `core/schemas.py`：TripTimeline 加字段
- `app/service.py`：`POST /timeline` 反序列化加新字段读取
- 现有测试：不受影响（新字段有默认值）

---

## 三、完整字段对照表

### Place ↔ A/C 景点信息 / Plan activity

| A/C 字段 | B 侧 Place 字段 | 状态 | 说明 |
|----------|----------------|------|------|
| `id` ("BJ_001") | `id` | 🟢 B 加字段 | 调整项 1 |
| `name` | `name` | ✅ 已对齐 | |
| `alias` | ❌ | 🟡 可选加 | 低优先级，B 不需要 |
| `location: {lat, lng}` | `lat`, `lng`（扁平） | ⚠️ 结构不同 | A/C 适配扁平结构，或 B 序列化时嵌套 |
| `price` | `price` | ✅ 已对齐 | |
| `duration` | ❌ | 🟡 可选加 | B 不需要（监控不依赖游览时长） |
| `opening_time` + `closing_time` | `open_time`（"09:00-17:00"） | ⚠️ 结构不同 | A/C 适配合并格式，或 B 拆分 |
| `content_tags` / `plan_tags` / `experience_tags` | ❌ | 🟡 可选加 | B 不需要（标签筛选是 A 的职责） |
| `reservation_required` | `ticket_required` | ⚠️ 字段名不同 | 语义相同，A/C 适配或 B 加别名 |
| `spot_id`（Plan activity 中） | `id` | 🟢 B 加字段 | 调整项 1 |
| `start`（Plan activity 中） | `arrival` | ⚠️ 字段名不同 | 协商项 4 |
| `end`（Plan activity 中） | `end_time` | 🟢 B 加字段 | 调整项 2 |

### MonitorEvent ↔ A/C 事件变化

| A/C 字段 | B 侧 MonitorEvent 字段 | 状态 | 说明 |
|----------|----------------------|------|------|
| `type` ("QUEUE_CHANGE") | `event_type` ("scenic") | 🟡 需协商 | 协商项 5：粒度不同 |
| `timestamp` | `observed_at` | ⚠️ 字段名不同 | 可加别名 |
| `spot_id` | `place`（name） | 🟢 B 加字段 | 调整项 3：加 `spot_id` |
| `data: {old_value, new_value}` | `data: Any` | ✅ 已兼容 | data 是灵活 dict |
| 无 | `event_id` | ✅ B 多出 | A/C 可用 |
| 无 | `rule_name` | ✅ B 多出 | A/C 可用 |
| 无 | `impact_score` | ✅ B 多出 | A/C 可用 |

### ReplanRequest ↔ A/C 决定

| A/C 字段 | B 侧 ReplanRequest 字段 | 状态 | 说明 |
|----------|----------------------|------|------|
| `event_id` | ❌ | 🟢 B 可加 | 低优先级 |
| `need_replan` | `need_replan` | 🟢 B 加字段 | 调整项 5 |
| `impact` | `impact` | 🟢 B 加字段 | 调整项 5 |
| `reason` | `reason` | ✅ 已对齐 | |
| `affected_spots` | `affected_spots` | 🟢 B 加字段 | 调整项 5 |
| 无 | `new_timeline` | 🔴 B 必须保留 | 协商项 1：B 需要完整时间轴 |
| 无 | `diff_summary` | ✅ B 多出 | A/C 可用 |

### ActionItem ↔ A/C 行为

| A/C 字段 | B 侧 ActionItem 字段 | 状态 | 说明 |
|----------|---------------------|------|------|
| `id` | `action_id` | ⚠️ 字段名不同 | C 适配或 B 加别名 |
| `type` ("BOOK_TICKET") | `type` | 🟢 B 加字段 | 调整项 4 |
| `target` ("spot_001") | `target`（"booking:ABC123"） | ⚠️ 格式不同 | C 解析冒号后 ID |
| `date` | `date` | 🟢 B 加字段 | 调整项 4 |
| `quantity` | `quantity` | 🟢 B 加字段 | 调整项 4 |
| `requires_confirmation`（bool） | `permission`（枚举） | ⚠️ 类型不同 | 协商项 6：映射关系 |
| `status`（"WAITING_CONFIRMATION"） | `status`（"pending"） | ⚠️ 枚举值不同 | 协商项 7 |
| 无 | `title` | ✅ B 多出 | C 可用于展示 |
| 无 | `description` | ✅ B 多出 | C 可用于展示 |
| 无 | `created_at` | ✅ B 多出 | C 可用于排序 |

### TripTimeline ↔ A/C 计划

| A/C 字段 | B 侧 TripTimeline 字段 | 状态 | 说明 |
|----------|----------------------|------|------|
| `id` ("plan_001") | `id` | 🟢 B 加字段 | 调整项 6 |
| 无 | `city` | ✅ B 多出 | A/C 可用 |
| 无 | `start_date`, `end_date` | ✅ B 多出 | A/C 可用 |
| `days[].date` | `days[].date` | ✅ 已对齐 | |
| `days[].activities[]` | `days[].items[]` | 🟡 需协商 | 协商项 4：键名不同 |
| `total_cost` | `total_cost` | 🟢 B 加字段 | 调整项 6 |
| `walking_distance` | `walking_distance` | 🟢 B 加字段 | 调整项 6 |

---

## 附：协商优先级排序

| 优先级 | 协商项 | 需要确认的内容 |
|--------|--------|---------------|
| 🔴 P0 | 协商项 1 | A 的 `decision_hook` 直接返回完整 `ReplanRequest`（含 `new_timeline`） |
| 🔴 P0 | 协商项 2 | A 发给 B 的计划携带完整 Place 对象（不只是 `spot_id`） |
| 🟡 P1 | 协商项 8 | A 的 LLM 调用用 `asyncio.to_thread` 包装，或 B 改为支持异步 `decision_hook` |
| 🟡 P1 | 协商项 3 | A 在 Place 中同时提供 `id` 和 `name` |
| 🟡 P2 | 协商项 4 | A/C 适配 `items` + `arrival`，或 B 做键名兼容 |
| 🟡 P2 | 协商项 5 | `event_type` 保持类别级，`data` 中加 `subtype` |
| 🟡 P2 | 协商项 6 | C 适配 `permission` 枚举，或 B 加 `requires_confirmation` 布尔字段 |
| 🟡 P3 | 协商项 7 | C 适配 B 的 `ActionStatus` 枚举值 |
