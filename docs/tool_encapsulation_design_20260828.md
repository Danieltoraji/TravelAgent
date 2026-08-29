# 工具封装设计优化方案（2026-08-28）

> **实施状态（0829）**：P0 分类框架、P1 执行器注册表、P2 技能固化
> （train_trip / weather_brief）已实施并合入；lodging/dining 上提因 a_side
> 耦合缓行；P3 预定接入、P4 function calling 待办。train_trip 接线按修正
> 方案执行：A 侧 provider 直调技能并传出行日期（原"map 内部适配"方案因
> 12306 日期必填而弃用），估算表保留为回退。
>
> **本文档定位**：工具体系的**抽象与泛化设计**——三轴分类框架、技能层、两段式
> 动作模式、LLM function calling 打通、未来预定功能落位。
> 点状缺陷修复见姊妹篇**《代码实现裂缝与修复措施》**
> （`docs/code_defects_and_fixes_20260828.md`，下称"裂缝文档"）。
>
> **分界与接口**：本文档以裂缝文档全部修复完成为**基线**，各项设计对修复项的
> 依赖见 §9 路线图标注；裂缝文档 §6 移交清单中的问题在本文档正式解决。

---

## 目录

- [0. 分界与接口](#0-分界与接口)
- [1. 设计目标与原则](#1-设计目标与原则)
- [2. 三轴正交分类框架](#2-三轴正交分类框架)
- [3. Skill 层设计](#3-skill-层设计)
- [4. 动作模式：两段式 + 执行器注册表](#4-动作模式两段式--执行器注册表)
- [5. 三条调用路径](#5-三条调用路径)
- [6. LLM function calling 打通](#6-llm-function-calling-打通)
- [7. 未来预定功能落位](#7-未来预定功能落位)
- [8. A/B/C 影响矩阵](#8-abc-影响矩阵)
- [9. 实施路线图](#9-实施路线图)
- [10. 反模式边界](#10-反模式边界)

---

## 0. 分界与接口

| 裂缝文档的修复项 | 本文档依赖它的设计 | 接口约定 |
|---|---|---|
| C5 schema 校验落地（前置 C6） | §6 LLM 白名单（schema 即 LLM 看到的参数契约） | 校验器就是 LLM 入参的第一道防线 |
| C1–C4 Mock/Live 同构 | §3 技能 output_schema（站在同构基线上才可信） | 以 Live 形状为 output_schema 蓝本 |
| E1 `hotel:` 处理函数（独立函数版） | §4 执行器注册表（把该函数注册为 `hotel` kind 执行器） | 函数签名保持 `(name, **ctx) -> BookingRecord` |
| E2 approve→执行接通 | §4 两段式（approve 即授权 commit） | 同一端点，语义泛化 |
| E3 booking_type 枚举统一 | §4/§7 action-skill 的领域取值扩展 | 枚举即注册表的 kind 来源之一 |
| E7 hotel/transport 填充分支（直调原子工具版） | §3 lodging/train_trip 技能（分支改为调技能） | 私有方法签名即技能调用点 |
| A3 normalize 主字段埋点 | §2 output_schema 收敛依据 | 埋点统计决定别名删除节奏 |

**总原则**：先裂缝文档、后本文档，全程无返工——裂缝修复全部写成可被注册/
复用的独立单元。

---

## 1. 设计目标与原则

**目标**：一套工具体系同时服务三类消费者——A 侧 LLM（tool_call）、B 侧
自动化流程、C 端 REST——且未来旅馆预定、车票预定等动作类功能接入时
**只加内容、不改框架**。

**原则**：

1. **意图与管道分离**：LLM 与 C 看到的应是"一步到位的意图"，矩阵构建、候选池
   供给等管道不进公开面；
2. **领域知识与消费解耦**：单位换算、选班次、排序规则等组合知识归属供给方
   （技能层），消费方（normalize/booking/LLM prompt）不再各自实现；
3. **副作用必须过闸**：查询自由，动作过权限闸（AUTO/CONFIRM/MANUAL），
   支付恒 MANUAL（`README.md:391` 既有安全边界不变）；
4. **零依赖不破**：Skill 是 BaseTool 子类而非新运行时；校验是轻量实现而非
   jsonschema 库；
5. **增量可回退**：每个设计项独立开关/独立注册，Mock 路径始终可用。

---

## 2. 三轴正交分类框架

**回答"按领域 / 层次 / 搜寻or执行？"——三个轴都要，各管一件事**，合成一份
工具元数据，而非三种互相竞争的文件夹结构：

| 轴 | 回答的问题 | 载体 |
|---|---|---|
| **领域 domain** | 代码住哪：子包、共享 client、`use_real_*` 开关 | 目录组织（`tools/train/` 模式），元数据标注 |
| **层次 kind** | 它是原料（atomic）、成品（skill）还是管道（internal） | ToolSpec 元数据 |
| **安全性 safety** | 它读还是写、谁能调用、要不要批准 | ToolSpec 元数据 + 执行层 PermissionLevel 焊接 |

领域轴无法回答"LLM 该看见谁"（同领域内查询可调、预定不可调）与"新领域的
成长顺序"（原子→技能→动作）；读写轴必须升为一等公民，因为它的本质是权限：
`safety=query` ≈ AUTO（任何消费者直调），`safety=action` ≈ CONFIRM（必须过
批准链路），支付恒 MANUAL——**与执行层既有 PermissionLevel 语义一一对应**。

### 2.1 能力矩阵

```
                    query（搜寻）                action（执行）
 atomic   │ weather / train_ticket /       │ （原则上不存在：
          │ web_search…                    │  单端点写操作应包成 skill）
 skill    │ train_trip / weather_brief /   │ hotel_book / ticket_book
          │ lodging / dining               │ （两段式 prepare/commit）
 internal │ map.batch_route                │ （无）
          │ scenic.search / hotel.tags     │
```

### 2.2 ToolSpec 扩展（向后兼容）

```python
@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict = {}
    output_schema: dict = {}     # 新增：出参契约（裂缝文档 A3 治本）
    domain: str = "general"      # 新增：weather/train/hotel/traffic/food/scenic/web/booking
    kind: str = "atomic"         # 新增：atomic / skill / internal
    safety: str = "query"        # 新增：query / action
    readonly: bool = True        # 保留：推导属性（safety=="query"），兼容旧消费方
    source: str = "mock"
```

### 2.3 白名单分层（取代单一 readonly 集合）

| 白名单 | 内容 | 消费方 |
|---|---|---|
| `list_for_llm()` | query-**skill** 主体 + 少量 query-atomic（见 §6 白名单建议） | function calling |
| `list_for_client()` | 全部 query-*（internal 除外） | `/api/tools/` 文档与 invoke 白名单 |
| `list_all()` | 全量（含 internal，供代码管道） | execution / live_data / 测试 |

现有 `readonly` allowlist 路径保留为 `list_for_client()` 的兼容别名。

---

## 3. Skill 层设计

**Skill 不是新运行时**：`class Skill(BaseTool)`——构造注入共享 client（TrainClient/
AmapClient/RollingGoClient），`_run` 内组合多次原子调用/客户端调用，输出
`output_schema` 化的意图级结构；照常 Mock/Live 双版本、走同一 registry 与
ToolResult 契约。领域知识（换算/选班次/排序）从消费方上收到这里。

### 3.1 首批四个技能（按证据强度排序，证据=同一组合已被 ≥2 类消费者各自实现）

**① `train_trip`（城际出行，第一刀）**

- 组合：`train_ticket`（选班次）+ `train_price`（取二等座价）；输入
  `from_city/to_city/date/preference(earliest|cheapest|direct)`；
- 输出：`{transport_minutes(int), cost_per_person(float), code, from_station,
  to_station, depart_time, arrive_time, source}`——**正好是 CityTravelEdge 的形状**；
- 化解的三件事：map 估算表 hack 退役（估算表保留为技能内回退）、A 侧
  `make_live_city_travel_provider` 零改动、LLM 不必学"查票→查价→心算"三步。

**② `weather_brief`（出行天气简报）**

- 组合：weather + weather_forecast + air_quality + weather_warning；
- 输出出行视角简报（今明趋势/预警/空气质量 + 一句综合建议字段）；消费方：
  execution_agent 事件佐证、LLM 白名单、C 端简报卡片。

**③ `lodging`（住宿）**

- 上提 a_side `transport/hotels.py` HotelSelector 的组合逻辑（搜索 + 预算/
  通勤校验 + 假池兜底）；booking_manager hotel 填充分支（裂缝 E7 的点状版）
  改为调它。

**④ `dining`（餐饮）**

- 上提 `RestaurantResolver`（food 搜索 + 偏好打分 + 锚点选店）；booking food
  填充分支与 A 侧餐厅锚定共用。

### 3.2 internal 化

`map.batch_route`、`scenic(action=search)`、`hotel(action=tags)` 打 internal：
不进 `list_for_llm()`、不进 `/api/tools/` 文档、保留 registry 供代码管道。
scenic 公开面收敛为纯 `status` 意图。

---

## 4. 动作模式：两段式 + 执行器注册表

### 4.1 两段式（所有 action 能力的公共模式）

```
prepare（幂等、无真实副作用）──批准链路──▶ commit（真实副作用）──▶ 支付恒 MANUAL
```

- `prepare`：任何调用方可发起。**LLM 调 prepare = "建议创建预定意图"**，系统
  自动落地为 ActionItem（CONFIRM 级）——LLM 永远触不到真实副作用；
- `commit`：唯一合法触发方是批准链路（ActionQueue approve → 执行器）；
  失败走既有 FAILED/BLOCKED → BOOKING 事件 → 换宿/换班次闭环；
- 支付：恒 `MANUAL` 提醒（"Agent 不代付"边界不变）。

**这不是新发明**：BookingManager 的 prepare→confirm 就是现成实现；本设计把它
从 booking 一个领域的特例泛化为所有 action 能力的公共模式。实现形态建议：
`class ActionSkill(Skill)` 基类约定 `prepare_input/commit` 两段 schema 与
幂等键（同参数重复 prepare 返回同一 intent）。

### 4.2 执行器注册表（消灭死信的泛化机制）

```python
EXECUTORS = {
    "booking":   booking_manager.confirm,          # 现有
    "hotel":     execute_hotel_booking,            # 裂缝 E1 的独立函数，此处注册
    "ticket":    execute_ticket_intent,            # 未来 §7
    "timeline":  noop（已由 apply_replan 承担）,
    "calendar":  ...,
}
# approve(action) → EXECUTORS[action.target.kind](action.target.id)
```

- 裂缝 E1 的 point fix（独立函数）在此注册为 `hotel` 执行器，无返工；
- approve 语义 = **批准即授权执行**（裂缝 E2 的泛化；交互变更需 C 确认，见
  裂缝文档 E2）；
- `mark_confirmed`（裂缝 E4）由 commit 成功回调按领域调用（Live 适配层或
  Demo 手动端点）。

---

## 5. 三条调用路径

LLM 要"粗"（意图级一步到位），自动化要"细"（步骤级精确控制）——解法是
**同一能力、分层暴露**，而不是二选一：

| 消费方 | query-atomic | query-skill | action-skill |
|---|---|---|---|
| **LLM tool_call** | 少量备选（语义单一者） | **直调（白名单主体）** | 仅触达 `prepare`（= 建议创建意图 → ActionItem 待批） |
| **B 侧自动代码** | 直调（管道） | 直调（替代 normalize 链） | 经 BookingManager 权限流（AUTO/CONFIRM/MANUAL） |
| **C 端 REST** | `/api/tools/invoke`（白名单） | 同左 + 只读缓存端点 | `/api/booking/*`、`/api/actions/*` 人工链 |

配套契约四要件（双满足的必要条件）：`output_schema`（机器可读）；幂等
prepare；Mock/Live 同构（裂缝文档 C1–C4 已铺）；错误分类标准化
（可重试 / 业务失败 / 需人工，error 消息对 LLM 可读）。

---

## 6. LLM function calling 打通

现状（裂缝文档 A1/A2）：A 侧 LLM 从未发起过 tool_call——`tools` 参数是死代码、
`tool_specs` 进 context 无消费。打通设计：

1. **转换器**：`ToolProvider.list_for_llm()` → OpenAI tools 格式
   `{"type":"function","function":{"name","description","parameters":input_schema}}`。
2. **回路**：`BaseClient.generate` 支持 `finish_reason=="tool_calls"`——解析
   tool_calls → 经 ToolProvider 白名单执行（**双重限制**：`list_for_llm()` ∩
   readonly）→ 结果按 `role=tool` 回填 → 续问；**上限 3 轮**；
   模型不支持 tools（API 报错）或超限 → 降级为现有纯文本 JSON 模式
   （降级原因写入返回值，`last_data_source` 式可观测）。
3. **白名单建议**（query-skill 为主）：
   - 首批：`weather_brief`、`train_trip`、`web_search`；
   - 视验证扩：`scenic(status)`、`lodging`、`dining`；
   - **不暴露**：`map`（内部矩阵）、`hotel` 多动作接口、`booking`/一切
     action-skill 的 commit、`web_fetch`（任意 URL 抓取）。
4. **接入点**：执行期事件评估（decision_engine 打分时可查实时工具佐证）与
   未来 C 端对话入口。预期管理：当前 replan"换酒店"由代码完成，LLM tool_call
   的增量价值在**对话式场景**与需要实时佐证的判断——按最小闭环接入，不为接而接。

---

## 7. 未来预定功能落位

### 7.1 能力边界（2026-08-28 核实）

| 服务 | 现状 | 结论 |
|---|---|---|
| RollingGo（酒店） | 通用 MCP 通道（`rollinggo_client.py`，Bearer + Streamable HTTP + 常驻会话）；仓内只用过 3 个只读工具 searchHotels/getHotelDetail/getHotelSearchTags；`list_tools()` 已实现未使用 | **先探测**：一次 `list_tools()` 确认服务端有无 order 类工具；`booking_url` 只是落地页字符串 |
| 12306（车票） | 参考实现 7 工具/5 URL 全部只读；真实下单需官方登录+实名+支付 | **无公开购票 API**；可行形态 = 购票意图 + 官方候补/人工 |

### 7.2 落位

```
hotel 领域                                  train 领域
  hotel(search/detail/tags)  query-atomic     train_ticket/price/route/transfer  query-atomic
  lodging                    query-skill      train_trip                          query-skill
  hotel_book                 action-skill     ticket_book                         action-skill
    ├ prepare: 选房+锁价意图 → ActionItem       ├ prepare: 选班次+购票意图（候补/提醒）
    └ commit: RollingGo order 工具（若探测到）   └ commit: 短期=官方候补链接+MANUAL 提醒
      或 booking_url 跳转+人工                     （中期如需自动化须官方授权渠道，另行立项）
```

- 两个领域、四种能力，**一套权限语义、一条批准链路、一个执行器注册表**；
- 接入 checklist：注册 registry → 查询技能 → 动作两段式 → 执行器注册 →
  缓存钩子（如需给 C）→ smoke 用例 → 主文档章节（吸收裂缝文档发现的教训）。

---

## 8. A/B/C 影响矩阵

| 改动 | A 侧影响 | B 侧影响 | C 端影响 |
|---|---|---|---|
| ToolSpec 扩展 domain/kind/safety | 无契约变化 | 各 Tool 类补 3 个类属性 | `/api/tools/` 多 3 个标注字段（兼容） |
| internal 标注 | batch_route 等退出 LLM 可见集合（本就无人用） | 无 | `/api/tools/` 可选过滤（由 C 定展示策略） |
| query-skill 固化 | city travel 切 train_trip **零改动**（source=live）；normalize 瘦身 | HotelSelector/RestaurantResolver 归位工具层；E7 分支改调技能 | 技能输出即展示结构；可选新只读端点 |
| 两段式 + 执行器注册表 | LLM 触达 action 收敛为仅 prepare（比现状更安全） | 死信/审批脱钩修复的泛化；新领域接入=注册执行器+写 skill | approve 语义变化需确认（裂缝 E2）；动作有执行结果回填 |
| hotel_book / ticket_book | 满房换宿闭环不变，零改动 | 泛化后新增领域=注册+写 skill | BookingRecord 新增领域取值；时间轴可表达预约状态（E6 字段） |
| function calling | decision prompt 变化、token 略增；DeepSeek/GLM tools 能力 + 降级路径 | 被暴露工具延迟可控（LLM 轮次内同步） | 未来对话入口复用白名单；现有端点零变化 |

**总评**：A 侧几乎零破坏；B 侧工作量在标注与技能上提；C 端全部为新增/可选，
唯一行为变更是 approve 语义（需确认）。

---

## 9. 实施路线图

依赖链：**裂缝文档四批次全部完成 = 基线** → 下列阶段可部分并行。

| 阶段 | 内容 | 依赖裂缝文档 | 粗估 |
|---|---|---|---|
| P0 分类框架 | ToolSpec 扩展三轴 + output_schema；白名单分层；internal 标注 | C5/C6（校验在线）、C1–C4（同构基线） | 0.5–1 天 |
| P1 动作基建 | ActionSkill 基类（两段式）；执行器注册表（注册 E1 函数）；approve=授权执行 **（需 C 确认）**；E4 回调 | E1/E2/E3/E4/E5 | 1–2 天 |
| P2 技能固化 | train_trip（第一刀，A 侧零改动验收=`last_data_source=live`）→ weather_brief → lodging/dining 上提 | E7（点状版先行）、A3 埋点数据 | 2–3 天 |
| P3 预定接入 | RollingGo `list_tools()` 探测 → hotel_book / ticket_book 按两段式接入；`Place.booking_*` 回填展示 | P1 注册表 + P2 技能 | 0.5–2 天（视探测） |
| P4 tool_call | list_for_llm 转换 + generate 回路/降级 + 接入 decision_engine | P0 白名单、C5 | 1–2 天 |

里程碑验收：P2 完成 = 城际段真实班次进入时间轴（`source=live`）；
P3 完成 = 首个真实/半真实预定意图可走通 prepare→approve→commit 全链。

---

## 10. 反模式边界

1. **不给所有工具包技能壳**：天气四件套本身就是意图粒度，包一层是无信息转发；
2. **技能不承载业务决策**：技能供给事实（含建议排序），"要不要换酒店/退票"
   是 decision_hook 与人工确认的事；
3. **不为接而接 tool_call**：当前管线 LLM 只需结构化打分，function calling
   先最小闭环验证价值；
4. **internal 不进公开面**：管道工具一旦进 LLM/C 文档就再难撤下；
5. **动作不走裸工具**：任何真实副作用必须过两段式 + 批准链路，禁止新增
   "一键下单型"只读伪装工具。
