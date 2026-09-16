# 规划轨迹（Plan Trace）v1 同步文档 —— A/C 交付口径

> 日期：2026-09-16 · 分支：`server_log_dxd_260915`（后续提交）· 状态：B 侧已实现，A 侧工作项待排期
> 关联文档：`docs/plan_trace_a_side_guide_20260916.md`（A 侧唯一代码改动的实施规格）

## 0. 一句话

规划期间的 30~110s 不再是黑盒：`POST /api/plan/`（和 `/api/chat/`）响应顶层新增
**`trace`** 字段——结构化的智能体轨迹（模型调用/工具调用/里程碑），前端可
**完成后回放**；另有**无锁轮询端点** `GET /api/plan-trace/`，等待期就能看到
实时步骤流。**契约只增不改**：TripTimeline、events（EventType）现有字段一律未动。

**本版不做**（v2 展望，见 §8）：SSE 真流式、trace 落库归档。

---

## 1. 数据流向（端到端）

```
[采集] 规划进行中（边跑边记，全部旁路 try/except，失败不影响规划）
  ├─ 真源/增强工具调用 ─→ logged_call（registry.call 包装，agent_runtime.py）
  │     ├─ tool_call_log（原有调试通道，语义不变；本次补上异常调用留痕）
  │     └─ rt.current_trace_recorder（新增轨迹缓冲，视图持锁期间挂载）
  ├─ 备注解析 LLM ─→ parse_requirement_input 返回值本就是完整 generate dict
  │     （B 侧此前只取 content，现同时保留轨迹）——零 A 侧改动
  ├─ 候选池 LLM ─→ A 侧透传 BPlannerHook.last_llm_trace（A 侧唯一工作项，
  │     未合入时该步自动降级，其余功能不受影响）
  └─ 编排里程碑 ─→ views.plan 各阶段起止打点
        ↓
[装配] 规划结束（锁内一次）
  trace = {version, request_id, total_elapsed_ms, plan_meta, steps[]}
  ※ request_id 与 X-Request-ID 响应头一致——C 端报障可拿它去 server.log 对账
        ↓ 存 rt.last_plan_trace（内存最近一份，含失败的部分轨迹）
[交付] 三条契约（只增不改）
  ① POST /api/plan/    响应顶层新增 "trace"（成功与 400/500 失败都带）
  ② GET  /api/plan-trace/   无锁轮询：running → done → idle
  ③ POST /api/chat/    响应顶层新增 "trace"（同 schema）
        ↓
[展现] C 端三状态（见 §4）
```

---

## 2. 哪些数据被采集（完整清单）

| # | 数据项 | 采集点 | trace 里的形态 |
|---|---|---|---|
| 1 | 真源工具调用（train_trip / train_ticket / flight_search / weather / map / hotel / food / traffic / scenic / web_search…） | logged_call 成功分支 | `tool` 名、脱敏 `args`、`result_digest`（≤200 字符关键事实）、`status`、`source`(mock/real_api)、`elapsed_ms` |
| 2 | 交通增强 map.route（并发 3 线程） | logged_call（经 runtime 挂载点，线程安全） | 同上，`phase=enrich` |
| 3 | 工具调用**失败**（抛异常） | logged_call 异常分支（本次补的盲区） | `status=error` + `error` 摘要；tool_call_log 同步补一条失败记录 |
| 4 | 备注解析 LLM（free_text 非空时） | B 侧保留 generate 返回 dict | `phase=parse` 的 llm 步（耗时/解析摘要）+ 工具轮/审查步（如有） |
| 5 | 候选池定制 LLM（门控 USE_LIVE_DATA+USE_LLM_TOOLS） | **A 侧透传** `last_llm_trace` | `phase=llm` 主步（model/耗时）+ 逐轮工具步（`phase=data`）+ 审查步（`phase=review`） |
| 6 | 编排里程碑 | views.plan 各阶段 | 归档 / 规划完成（附数据源三态）/ 交通增强；`phase=plan|enrich` |
| 7 | chat 对话 LLM + 其工具轮次 | B 侧 generate 返回 dict | chat 响应 `trace`，schema 相同 |

### 步骤 schema（version=1）

```jsonc
{
  "seq": 3,                    // 序号，从 1 连续递增
  "t": 842,                    // 相对起点的毫秒偏移（回放动画的时间轴）
  "phase": "data",             // parse🔍 | data🔎 | llm🤖 | review⚖️ | plan⚙️ | enrich🚄 | final🔨
  "kind": "tool",              // llm | tool | milestone
  "title": "调用 train_trip",
  "tool": "train_trip",        // kind=tool 时有
  "model": "deepseek-chat",    // kind=llm 时可能有
  "args": "{\"from_city\":\"北京\",...}",  // 脱敏+截断≤200 字符
  "result_digest": "共 8 个班次，价格 73.5~156 元",       // ≤200 字符关键事实
  "elapsed_ms": 840,
  "status": "ok",              // ok | error | no_data
  "source": "real_api",        // mock | real_api | llm_round（可选）
  "error": "…"                 // 仅失败步
}
```

顶层 `trace.plan_meta`（落锤信息）：`data_source`（fake/live/live_fallback，A 镜像
未合入前为 null）、`search_plan_tried`、`planner_error`、
`timeline`（city/days/spots/total_cost 统计）。

### result_digest 生成规则

按工具名分派摘要器（train→「共 N 个班次，价格 X~Y 元」、flight→「共 N 个航班」、
weather→「晴 12~23℃」、hotel→「共 N 家酒店，价格区间」、map→「X km / Y min」…），
未命中走通用兜底（记录条数/字段清单）。**原则：结构化摘要，不回传原始 ToolResult。**

### 明确不采集（负清单）

- 工具返回的完整 data（只进 digest）；
- 敏感参数值：`tel/phone/mobile/password/passwd/token/secret/id_card/email` → `***`
  （与 server log 同一黑名单，`runtime/plan_trace.py` 的 `SENSITIVE_ARG_KEYS`）；
- LLM 原始 prompt 全文、候选池完整 JSON；
- 非规划/对话请求（poll、lookahead、events 等不产生 trace）；
- 执行期 replan（BDecisionHook）暂不在 trace 范围（v2）。

---

## 3. 契约明细（只增不改）

| 端点 | 变化 |
|---|---|
| `POST /api/plan/` | 成功响应 `{"status","message","timeline","planner_error"}` **+ `"trace"`**；失败响应 `{"error"}` **+ `"trace"`**（部分轨迹，定位失败环节） |
| `POST /api/chat/` | 成功 `{"reply","elapsed_ms"}` **+ `"trace"`**；工具轮次超限的友好回复也带；「LLM 未配置」502 不带（未产生调用） |
| `GET /api/plan-trace/` | **新增**，需 Bearer。`{"status":"running","steps":[…],"phase","elapsed_ms"}` / `{"status":"done","trace":{…}}` / `{"status":"idle","trace":null}` |

- **无锁保证**：plan-trace 端点不碰 `runtime.lock`，规划长写期间即时返回
  （规划占 1 个 gthread 线程，轮询走其余线程）。
- **时序说明**：`POST /api/plan/` 的备注解析阶段在锁外执行（前 2~5s），此窗口
  轮询可能返回 `running` 且 `steps` 为空——属正常，前端显示起始文案即可。
- **A 镜像未合入时的降级**：`plan_meta.data_source` 为 null、无候选池 LLM 细节步，
  其余轨迹完整。合入后自动点亮，C 端无需改代码。

---

## 4. 如何展现（C 端 UI 规范）

三个状态：**等待期 → 完成回放（主路径）；轮询异常静默降级（兜底）**

1. **等待期**（POST 已发出）：骨架屏 + spinner + 文案
   「LLM 正在编排（真源查询 + 迭代优化，约 30~100s）」；同时每 **1~2s** 轮询
   `GET /api/plan-trace/`，`status=running` 时把 `steps` 渲染为实时步骤流
   （新步骤追加、自动滚动）——替换现有时间驱动的假进度条。
2. **完成期**（响应到达）：用响应里的完整 `trace` 按 `t(ms)` 等比回放，总动画
   **压缩到 10~15s**（等比缩放 `t`）；步骤逐条点亮；点击行展开
   `args / result_digest / elapsed_ms / source` 详情。回放节奏可控，对答辩录屏友好。
3. **降级**：轮询报错 / 一直空步骤 → 静默跳过，等响应后直接回放
   （回放不依赖轮询，任何情况下可用）。

**步骤行渲染**：图标按 phase（见 §2 枚举后注释）；状态着色 ok 绿 / error 红 /
no_data 黄；前端已有 `AgentLog.tsx`（时间线容器）与 `ToolCallCard`（手风琴详情）
组件，结构与步骤 schema 几乎同构，可直接复用。`trace` 建议随会话存 localStorage
（同 `voyageai.session.v1` 桶扩展，24h TTL），历史会话可重放。

---

## 5. C 端接入指南（最小改动）

```ts
// types.ts：步骤与 trace 类型（按 §2 schema）
interface TraceStep { seq: number; t: number; phase: string; kind: string;
  title: string; tool?: string; model?: string; args?: string;
  result_digest?: string; elapsed_ms?: number | null;
  status: "ok" | "error" | "no_data"; source?: string; error?: string }
interface PlanTrace { version: number; request_id: string; total_elapsed_ms: number;
  plan_meta: Record<string, unknown>; steps: TraceStep[] }

// api.ts：fetchJSON 是唯一请求咽喉点（已有 Bearer 头），submitPlan 泛型扩展约 1 行
submitPlan(): Promise<{ status: string; timeline: BTripTimeline; trace?: PlanTrace }>

// 等待期轮询（runAgent 内，isRunning 期间 1.5s 一次）：
const poll = setInterval(async () => {
  const s = await api.get<PlanTraceProgress>("/plan-trace/");
  if (s.status === "running") setLiveSteps(s.steps);
}, 1500);   // finally 里 clearInterval；失败即静默（降级为回放）
```

新规划发起时清空上一次的 liveSteps / trace；`trace.request_id` 建议随报障一并提交。

---

## 6. A 侧协作约定（摘要）

三个 LLM 调用点中**两个无需 A 改**（parse / chat 的轨迹 B 侧自留）；A 侧唯一代码
改动是**候选池 LLM 轨迹透传**（scenic_search_planner → data_source → BPlannerHook
三个文件、约 10 行）。透传格式：

```json
{"model": "…", "elapsed_ms": 1234, "tool_trace": [原样], "reviews": [原样]}
```

硬性要求：**纯附加属性 + try/except 兜底**，不改任何现有返回值与控制流。
实施细节、逐文件改点、验收标准见 **`docs/plan_trace_a_side_guide_20260916.md`**。

---

## 7. 测试与基线

- 专项：`tests/test_plan_trace.py` 13 例全绿（recorder 单元 + plan/plan-trace/chat 集成）；
- 全量回归：`pytest tests/` **637 passed + 3 failed**，3 个失败均为 `test_ab_sync`
  已知环境性失败（本机无 A 仓库外层布局），与本功能无关；
- 契约兼容：`test_free_text_remark.py` 已随 `_parse_free_text_requirement`
  返回值形态（payload → (payload, parse_meta)）同步更新，行为契约不变。

## 8. v2 展望（本版明示不做）

- **SSE / 异步任务化**：等待期真流式（当前用无锁轮询拿到八成实时感，成本低一个量级）；
- **trace 落库**：随 `TripPlanArchive` 归档，历史规划详情页可回放轨迹（schema 已留
  `version` 字段，落库为纯增量改动）；
- **执行期 replan 轨迹**（BDecisionHook 决策链）纳入同一时间线。
