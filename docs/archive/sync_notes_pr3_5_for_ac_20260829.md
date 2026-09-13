# A/C 同步说明：PR #3–#5 变更与待办（2026-08-29）

> 覆盖三个已合入 main 的 PR：
> - **PR #3** 修复 22 项代码实现裂缝（工具契约 / 执行侧 / 文档 / 校验 / 动作执行器）
> - **PR #4** 热修 `/api/plan/` 500（`_settings` 未定义）
> - **PR #5** 工具封装设计 P0-P2（三轴分类框架 / 执行器注册表 / train_trip・weather_brief 技能）
>
> 详细技术依据见 `docs/code_defects_and_fixes_20260828.md`（裂缝清单）与
> `docs/tool_encapsulation_design_20260828.md`（设计文档）。本文只讲
> **"你们的部分有没有被动、你们需要做什么"**。

---

## 0. 一页速览

| 角色 | 需要做的事 | 仅需知悉（无需改代码） |
|------|-----------|----------------------|
| **A** | ① 回同步 2 个 a_side 文件（§1.1）；② 本地测试先 `pip install -r requirements.txt`；③（建议）保证需求里 start_date/travel_schedule 有值 | CityTravelEdge 契约零变化；planner/select_spots 零改动；DecisionRequest.context 缩小 |
| **C** | 无强制项。可选：接入新端点 mark-confirmed、展示预约状态/审计字段 | /api/tools、/api/timeline、/api/actions 均为**纯增量**字段；hotel detail 形状变化仅限本地 Mock 模式；approve 语义未变 |

---

## 1. A 侧

### 1.1 ⚠️ 被修改的 a_side 文件（需要回同步到 A 仓库）

| 文件 | 改动内容 | 来源 |
|------|----------|------|
| `a_side/data_transmission/live_data.py` | ① 补 `logger` 定义（原文件无 logging，别名埋点分支命中会 NameError——实修的 bug）；② 各 normalize 加主字段声明 + 别名命中 debug 埋点；③ **新增 `make_live_train_trip_provider(tool_provider, date)`** | PR #3 + #5 |
| `a_side/call_llm/b_planner_hook.py` | `_attach_trip_segments` 的城际 provider 切换：map 估算表 hack → `make_live_train_trip_provider`（date 取 `travel_schedule.departure_date`，失败回退本地估算表） | PR #5 |

> 回同步时**整个文件覆盖即可**（B 侧已保证全量测试通过）；若 A 仓库对这两个
> 文件有并行改动，请先在群里对齐再合。

### 1.2 A 侧行为变化（无需改代码，但影响规划结果）

| 变化 | 说明 |
|------|------|
| **城际交通真源化** | 时间轴 transport 段 `details.source` 从 `estimate` 变 `live`，车站/时刻/票价为真实 12306 班次（如 北京→上海：G25 / 258 分钟 / 二等座 795 元）。`travel_schedule.departure_date` 缺失时自动回退估算表 |
| **回退更稳** | 旧版工具失败会抛 LiveDataError 导致城际段整体消失；新版返回 None 回退估算，规划不再因 12306 抖动丢城际段 |
| **假数据模式餐厅带坐标** | food Mock 补了 location——假数据/回退模式下餐厅可正常参与通勤计算（此前全部被丢弃） |
| **DecisionRequest.context 缩小** | `tool_specs` 已移除（A 侧从未消费）；`BDecisionHook` 只读 `impact_threshold` 不受影响 |

### 1.3 A 侧需要做的

1. **回同步 §1.1 的两个文件**到 A 仓库（或在下次"同步 a_side"时确认接受）；
2. **本地跑测试先 `pip install -r requirements.txt`**——缺 rapidfuzz/openai 时
   `test_a_interface` / `test_replan_actions` 会被显式 skip（此前表现为假失败，
   8.28 排查过一轮）；
3. （建议）需求解析尽量保证 `start_date` 与 `travel_schedule.departure_date`
   有值——缺失时城际段拿不到真源班次，会回退估算。

### 1.4 明确无需做的

- `CityTravelEdge` 契约**零变化**（train_trip 输出直接对齐，无换算）；
- `select_spots` / `plan_multi_day` / `planner` 全链零改动；
- `booking` 不可被直调的边界不变；`readonly` 布尔语义保留（新增的
  `safety` 字段是它的正式化，向后兼容）。

---

## 2. C 侧

### 2.1 API 变化一览（**全部增量字段/新增端点，无 breaking**）

| 端点 | 变化 | C 需要做的 |
|------|------|-----------|
| `GET /api/timeline/` | `Place` 新增 `booking_id` / `booking_status`（默认空串；自动预约成功后回填，如 `"pending_confirm"`） | 可选：展示"已预约/已确认"状态 |
| `GET /api/actions/` | 每项新增 `decided_at` / `decided_by`（approve/reject 时回填）；配置 `BOOKING_PERSIST_PATH` 后**动作重启不丢** | 可选：展示审计信息 |
| `GET /api/tools/` | 列表新增 `train_trip` / `weather_brief` 两个技能；每个工具新增 `domain` / `kind`(atomic/skill/internal) / `safety`(query/action) / `internal_actions` 字段 | 可选：据 `internal_actions`/`safety` 过滤展示面 |
| `POST /api/booking/{id}/mark-confirmed/` | **新端点**：服务方确认回调（SUBMITTED → CONFIRMED） | 可选接入 |
| `POST /api/booking/prepare/` | `booking_type=hotel/transport` 时返回的 `price`/`address` 开始有值（此前恒空） | 无需改动，展示更完整 |
| `GET /api/replans/` | `context` 不再含 `tool_specs`（体积变小） | 无需改动 |

### 2.2 仅本地 Mock 模式的形状变化（线上 Live 不变）

`hotel` 工具 `action=detail` 在 **Mock 模式**下返回结构从 `roomRatePlans`
（camelCase）统一为 `rooms`（snake_case），与线上 Live 形状一致——
`/api/hotels/{id}` 缓存在 Mock 部署下随之变化。**线上（配置 RollingGo key）
走 Live，形状从未变过**；`docs/C_hotel_data.md` §一.2 已同步为新口径。

### 2.3 明确不变的

- **approve 语义未变**：对 `booking:` 类动作仍是"批准仅标记"，confirm 仍走
  `POST /api/booking/{id}/confirm/`（E2 语义变更待与 C 确认后另行上线）；
- REST 双入口未收紧（无过滤的 `/api/tools/<name>/invoke/` 保持原样）；
- 所有既有字段无删除、无改名。

### 2.4 一个新行为要知道

重规划换宿产出的「预订{新酒店}」动作（HOTEL_BOOK）此前是死信（批准后永远
PENDING）；现在**批准后会真实创建酒店预约单**：动作变 EXECUTED、description
回填新 `booking_id`、`GET /api/booking/` 出现对应记录。预约单的 confirm 仍需
C 端用户操作。

### 2.5 C 需要做的

无强制项。可选项按优先级：
1. （建议）时间轴接入 `booking_id/booking_status` 展示预约状态；
2. （可选）Action Queue 展示 `decided_at/decided_by`；
3. （可选）接入 mark-confirmed 端点；
4. （仅本地 Mock 联调时）按 `docs/C_hotel_data.md` 新口径适配 hotel detail。

---

## 3. 已修复的部署事故（知悉）

- 8.29 部署冒烟曾因 `/api/plan/` 500（B 侧 `_settings` 未定义）失败——
  **PR #4 已修**并新增回归用例 `TestInitFromRequirement`；
- 同期确认：本地/CI 跑测试需先 `pip install -r requirements.txt`，缺
  rapidfuzz/openai 时相关用例现在会**显式 skip**（不再假失败）。

---

## 4. 下一轮预告（提前留心理prepare）

| 项 | 涉及方 | 预告 |
|----|--------|------|
| P3 预定接入 | C 为主 | Action Queue 将出现 `ticket:` 等新 kind；hotel_book 的 commit 是否接 RollingGo 真实下单，取决于 `list_tools()` 探测结果，届时单独同步 |
| P4 function calling | A 为主 | 会改 `a_side/call_llm/BaseClient.py`（tool_calls 回路）与 `decision_engine.py`——**属 A 侧代码，开工前会先出设计并与 A 确认**；LLM 白名单首批拟为 weather_brief / train_trip / web_search |
| lodging/dining 上提 | A | 因输入契约深嵌 requirement/plan 结构暂缓，待接口设计定稿再排 |


---

## 5. 追加（0829 二轮，P3 预定接入 + P4 function calling）

### 5.1 RollingGo 探测结论（重要）

`list_tools()` 实测：RollingGo MCP **仅暴露 3 个只读工具**（searchHotels /
getHotelDetail / getHotelSearchTags），**无任何下单/预订工具**。因此
hotel_book 的 commit 取设计文档回退终态：**意图组装 + booking_url 落地页 +
MANUAL 付款**；若未来 RollingGo 上线 order 工具再评估接入（需产品与 C 确认）。

### 5.2 A 侧回同步清单（⚠️ 在 §1.1 基础上新增 3 个文件）

| 文件 | 改动 | 说明 |
|------|------|------|
| `a_side/call_llm/llm_clients/BaseClient.py` | generate 增 `tool_executor`/`max_tool_rounds` 与 tool_calls 回路、不支持 tools 时的一次性降级 | **P4 核心，需 A review** |
| `a_side/call_llm/decision_engine.py` | decide_replan 增 `tool_provider` 参数 + `USE_LLM_TOOLS` 门控（默认 false） | 关闭时行为与既往完全一致 |
| `a_side/call_llm/b_decision_hook.py` | _decide 透传 tool_provider | 一行 |

### 5.3 C 侧新增（全部增量）

- `/api/tools/` 新增 `hotel_book` / `ticket_book` 两个动作技能
  （readonly=false → 只读白名单 invoke 端点不可调，仅展示）；
  每工具新增 `domain/kind/safety` 等标注字段
- 未来 Action Queue 可能出现 `ticket:` 前缀动作（批准即创建车票预约单）

### 5.4 新增环境开关

| 变量 | 默认 | 说明 |
|------|------|------|
| `USE_LLM_TOOLS` | false | 开启后决策 LLM 可查询白名单工具佐证（weather_brief/train_trip/web_search 等）；需 A review 后开启 |
| `BOOKING_PERSIST_PATH` | 空（关） | 生产 deploy.yml 已注入 logs/actions.json |
