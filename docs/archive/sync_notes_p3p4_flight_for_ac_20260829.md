# A/C 同步说明：技能框架与预定接入（P3/P4 + 航班真源融合）（2026-08-29）

> 对应分支 `skill-design-p3p4_dxiaodai_20260829`（**已合入 main，PR #6**），
> 同时包含 A 的航班真源工作与本仓库 PR #3–#5 的融合结果；并追加
> **PR #7（C 端报告交通故障 T1–T4 修复）**（见 §6）。
> 覆盖两份规划文档的全部实施项：
> - `docs/code_defects_and_fixes_20260828.md`（裂缝修复：**22/23 完成**，E2 待 C 确认；另有 T1–T4 交通修复，见 §6）
> - `docs/tool_encapsulation_design_20260828.md`（封装设计：**P0–P4 全部完成**，lodging/dining 缓行）
>
> 验证基线：`python -m pytest tests/ -v` → **448 passed**。

---

## 0. 一页速览

| 角色 | 需要做的事 | 仅需知悉（无需改代码） |
|------|-----------|----------------------|
| **A** | ① **回同步 3 个 a_side 文件**（§2.1，P4 改动了 LLM 客户端与决策引擎）；② **决策**是否开启 `USE_LLM_TOOLS`（默认关）；③ 确认 LLM 工具白名单首批清单 | 航班/火车/技能的融合链已按 A 的架构落地；CityTravelEdge/规划算法契约不变；lodging/dining 上提缓行（A 侧 Resolver/Selector 不动） |
| **C** | 无强制项。可选：展示 flight_search/预订意图、接入 mark-confirmed、适配 `JUHE_FLIGHT_KEY` 部署配置 | /api/tools 增 5 个工具 + 标注字段；时间轴城际段 `source=live`；全部为增量，无 breaking |
| **C（T1–T4，§6）** | 无需改代码。按 §6.4 验证方法复验交通监控 | 交通监控修复：非北京城市可用、限流自动重试、动线语义更真实、错误带分类 |

---

## 1. 我们做了什么（B 侧本轮）

### 1.1 融合 A 的航班真源（冲突解决）

合并 origin/main（A 的 5 个提交：航班工具组 `tools/flight/`、融合城际链
`make_live_intercity_provider`、BFS+剪枝联运算法、部署 key 配置）与本分支
（P3/P4）。唯一冲突 `tools/__init__.py` 按并集解决：**A 的航班注册 + 我们的
动作技能注册全部保留**，合并后注册表 **21 个工具**，全量测试 437 通过。

### 1.2 P3：预定接入（两段式动作技能）

- **RollingGo 探测结论（0829 实测）**：MCP 服务端仅 3 个只读工具
  （searchHotels/getHotelDetail/getHotelSearchTags），**无下单通道**——
  hotel_book 的 commit 取设计文档回退终态。
- **`ActionSkill(Skill)` 基类**（`tools/action_skill.py`）：两段式契约——
  `prepare` 幂等组装预订意图（纯计算，任何调用方可发起）；
  `commit` 是真实副作用，**仅批准链路（approve → 执行器注册表）可触发**，
  直调一律被守卫拦截并透出领域原因。`safety=action / readonly=False`，
  天然不进 LLM 与只读白名单。
- **`hotel_book`**：按城市/酒店名查询并组装预订意图（房价/地址/booking_url）。
- **`ticket_book`**：经 train_trip 组装车票意图（班次/二等座价）；边界明示：
  12306 无公开自动化购票 API，出票走官方候补/人工。
- **BookingManager**：执行器注册表新增 `"ticket"` 执行器（→ transport 预约单，
  车次/票价经自动填充落库）。

### 1.3 P4：function calling 最小闭环（默认关闭）

- `ToolProvider.to_openai_tools()`：LLM 白名单 → OpenAI function 格式；
- `BaseClient.generate` 增 `tool_executor`/`max_tool_rounds`：模型请求调用
  工具时经白名单执行并回填续问，**上限 3 轮**；模型/网关不支持 tools 时
  **一次性降级**为纯文本 JSON 模式（降级原因写入返回值）；
- `decision_engine` 经 **`USE_LLM_TOOLS` 环境变量门控（默认 false）**接入：
  开启时决策 LLM 可查询白名单工具佐证影响评分。

### 1.4 文档

设计文档标注 P0–P4 实施状态与探测结论；工具文档补动作技能两段式契约表；
同步说明追加本轮章节。

---

## 2. A 应该改什么

### 2.1 ⚠️ 回同步 3 个 a_side 文件（P4 改动了 A 的 LLM 层，需 A review）

| 文件 | 改动 | 说明 |
|------|------|------|
| `a_side/call_llm/llm_clients/BaseClient.py` | `generate` 增 `tool_executor`/`max_tool_rounds` 参数与 tool_calls 执行回填回路；不支持 tools 时一次性降级 | **P4 核心，请 A 重点 review**；关闭门控时不走新路径 |
| `a_side/call_llm/decision_engine.py` | `decide_replan` 增 `tool_provider` 参数；`_use_llm_tools()` 读 `USE_LLM_TOOLS` 门控 | 默认 false，行为与既往完全一致 |
| `a_side/call_llm/b_decision_hook.py` | `_decide` 透传 `tool_provider` | 一行 |

### 2.2 A 需要决策/确认的

1. **`USE_LLM_TOOLS` 是否开启**（默认关）：开启后决策 LLM 可查询白名单工具
   佐证影响评分；token 略增，需 DeepSeek/GLM 的 tools 能力（已实现降级兜底）。
2. **LLM 白名单首批清单确认**：当前为 `list_for_llm()` 规则自动产出
   （query 且无内部管道动作），含 weather_brief / train_trip / web_search /
   food / weather 系 / train 系；A 如需调整白名单口径请反馈。
3. **lodging/dining 上提缓行**确认：`RestaurantResolver` / `HotelSelector`
   暂不动（其输入契约深嵌 requirement/plan 结构），待接口设计定稿再排期。

### 2.3 A 无需改的

- 航班工具组（`tools/flight/`）已原样融合，A 无需改自己的航班代码；
- 融合链（train_trip → train_ticket → flight → 估算）的回退语义与 A 的设计
  一致，B 仅在其上叠加了动作技能；
- `CityTravelEdge`（含新增 `candidates` 默认字段）、BFS 算法、planner 零改动。

---

## 3. C 应该改什么

### 3.1 API 变化一览（**全部增量，无 breaking**）

| 端点/数据 | 变化 | C 可选动作 |
|------|------|-----------|
| `GET /api/tools/` | 新增 5 个工具：`flight_search` / `train_trip` / `weather_brief` / `hotel_book` / `ticket_book`；每工具新增 `domain` / `kind`(atomic/skill) / `safety`(query/action) / `internal_actions` 字段 | 建议按 `safety=action` 标注"需人工确认"，按 `internal_actions` 隐藏管道工具 |
| `GET /api/timeline/` | 城际 transport 段 `details.source=live`（此前 estimate）、班次/车站/票价为真实数据；`details.candidates` 可含多条候选班次/航班 | 可选：展示真实班次与联运候选 |
| Action Queue | 未来可能出现 `ticket:` 前缀动作（批准即创建车票预约单） | 可选：按 type 展示 |
| `POST /api/booking/{id}/mark-confirmed/` | 上轮已加，此处提醒可接入 | 可选 |

### 3.2 部署配置（重要）

| 配置 | 说明 |
|------|------|
| `JUHE_FLIGHT_KEY` | **服务器需在 GitHub Secrets 配置**，否则航班工具自动回退 Mock（deploy.yml 已写入注入逻辑，缺 key 不影响部署） |
| `BOOKING_PERSIST_PATH` | 上轮已注入 logs/actions.json（动作重启不丢） |
| `USE_LLM_TOOLS` | 默认关闭；A review 后如开启，在 Secrets/环境补 `USE_LLM_TOOLS=1` |

### 3.3 明确不变的

- **approve 语义未变**（对 `booking:` 类动作仍是批准仅标记；E2 待 C 确认后另行上线）；
- REST 双入口现状未动；既有字段无删除/改名；
- 预订类动作的边界不变：**Agent 只组装意图，不自动下单、不代付**。

---

## 4. 下一步（B 侧待办）

1. ~~本分支 PR 合入 main~~ ✅ 已完成（PR #6）；后续交通修复见 PR #7；
2. E2（approve 即触发 confirm）待 C 确认交互语义后单独实施；
3. lodging/dining 上提的接口设计（a_side 候选池注入方案）；
4. 服务器配置 `JUHE_FLIGHT_KEY` 后做一次 flight 真源端到端复验。


---

## 6. 追加（0829 三轮，PR #7）：C 端报告的交通故障 T1–T4 修复

C 端报告《交通与酒店问题排查-AB修复建议》逐条核实与修复结果（技术细节见
`docs/code_defects_and_fixes_20260828.md` 附录）。排查基线为当时 main（含
PR #3/#4，未含 #5/#6）；PR #5/#6 未触碰交通代码，**全部故障在修复前均存在**。

### 6.1 逐条闭环

| 编号 | 排查结论 | 修复内容 |
|------|----------|----------|
| **T1（P0）** 非北京城市交通必现异常 | **属实且比报告更严重**：共 **3 处**硬编码 `city="北京"`——traffic 工具两次 geocode + get_route 默认值（报告漏了第三处） | `TrafficToolLive` 增 `city` 参数全链透传；Mock 版签名/schema 同步；监控规则传 `timeline.city`。**真源验证**：广州塔→陈家祠 transit 查通（35min/11.1km），修复前必失败 |
| **T2（P1）** 生成瞬间偶发失败 | **属实**：QPS 退避重试此前只存在于批量 distance 端点；geocode/get_route 被限流（infocode 10021 等）即立即失败，且 BaseTool 视为业务错误不重试 | `AmapClient._get_with_transient_retry`：QPS 类 infocode（10019/10020/10021）退避 0.4s×n 重试最多 3 次；geocode 与 get_route 全部 4 个端点接入。非瞬时错误（如 key 无效）不重试 |
| **T3（P3）** 监控语义 | 属实：监控的是"市中心→首景点" | traffic-poll 改为 **timeline 前两个到达点**（排除 hotel）之间的通勤，真实游客动线前段；不足两个回退旧行为 |
| **T4（P3）** 错误上报缺细节 | 部分属实（infocode 已在错误串，但无分类与参数） | `_poll` 失败返回增 `params`（触发查询参数）与 `error_category`：`RATE_LIMITED`（限流）/ `GEOCODE_NOT_FOUND`（未找到地址）/ `OTHER`，随 MonitorEvent.data 透传 |

### 6.2 给 C 的书面回应（H1/H2，无代码改动）

- **H1（满房演示订假池酒店）**：经核实**非回退 bug，是满房演示的刻意设计**——
  smoke 清单 3 与 BookingTool 的模拟失败依赖假池酒店名 + "（满房）"后缀触发
  "确认失败 → BOOKING 事件 → A 重规划 → 新酒店动作"的验收闭环，与真源注入
  无关。如需真源化演示（用真源酒店名注入强制失败），请提需求排期。
- **H2（换宿链路缺完成证据）**：链路本身有测试与冒烟覆盖
  （`test_replan_actions` + 清单 3）；"缺证据"是 replans 为内存态、被新计划
  重置——actions 已持久化（E5），建议按报告 §修复后的验证方法跑清单 3 并
  留存产物（replans 记录、新动作、timeline 变更）。

### 6.3 C 端验证方法（采纳报告建议）

- **T1**：用广州或上海做目的地发起规划，确认监控不再出现"交通数据获取异常"，
  且 `/api/tool-calls/` 里 traffic 调用携带正确城市（`city` 字段）
- **T2**：连续发起 3-5 次规划，统计 traffic 调用失败率（修复前约 1/34）；
  失败时 `/api/tool-calls/` 的 `error_category=RATE_LIMITED` 可确认为限流类
- **H1/H2**：跑满房演示场景，确认 replans 有记录且 timeline 换宿

### 6.4 部署提示

- 修复不涉及新配置；部署后无需迁移。 traffic 监控事件的新增字段
  （`params`/`error_category`）为增量透出。
