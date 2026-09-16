# B 侧改动说明：确认链路三修复（2026-09-16）

> 写给 B 侧队友：A 侧（mengguangxuan 工作区）在 **B 侧代码目录** 直接做了三处
> 修复，全部源于用户实测反馈——「智能体输出调整计划后，同样的消息弹出很多次；
> 点确认一直'执行中'无法完成」。诊断与方案详见工作区
> `plan/架构整理/确认链路诊断与修复方案-20260916.md`，本文档只讲**改了什么、
> 对 C 端/部署有什么影响**。改动只在本仓库（不涉及 a_side 镜像 6 目录）。

## 一、背景：确认链路当时发生了什么

```
C 端点确认 → POST /api/booking/{id}/confirm/（持 runtime.lock 全程）
  → booking_manager.confirm → booking submit 失败（满房）
    → _on_booking_failed 回调 → asyncio.run(handle_event(event))
      → BDecisionHook 完整重规划（选点+池审核+估时+排程，30~110s）
    → 重规划跑完才返回 400
```

- confirm 请求被重规划拖住 30~110s，C 端按钮「一直在执行中」；
- 每 5s 的 poll 全量重发周期事件，`_significant` 无状态 → 同一显著事件反复
  触发 decision→replan；
- 每圈 replan 产出一张一模一样的「预订XX酒店」卡片，逐圈累积不消失；
- 满房酒店可以被反复 prepare→confirm→失败，构成死循环。

## 二、三个修复（按用户症状对应）

### 修复 1：确认异步化 —— 治「确认一直执行中」

**文件**：`django_server/runtime/agent_runtime.py`、`django_server/api/views.py`

- `_on_booking_failed` 不再在 confirm 请求线程里同步跑重规划，改为提交到
  **运行时自持的单线程后台池**（`_replan_executor`，同用户多个满房事件串行）；
- 后台任务持 `runtime.lock` 执行（与视图层写互斥；`execution_poll` 拿不到锁
  返回 busy，天然避让），并挂 `PlanTraceRecorder`；
- 新增会话陈旧保护：新 plan 重建 agent 后，旧会话挂起的后台重规划直接作废；
- `booking_confirm` 失败响应**只增两个字段**：`replan_async: true`、
  `replan_in_progress`（事件概要 dict 或 null）——旧解析完全不受影响。

**C 端看到的变化**：confirm 秒级返回 400（含原有 error/booking/actions）；
重规划在后台进行期间，`GET /api/plan-trace/` 返回 `status:"running"` 的实时
步骤流（复用现成端点，C 端无需新接口），完成后落 `last_plan_trace`；
`GET /api/status/` 新增只读字段 `replan_in_progress`。

### 修复 2：事件冷却去重 —— 治「重复弹卡 / 反复 replan」

**文件**：`execution/execution_agent.py`

- `handle_event` 在 `_significant` 放行后、进决策前，按**事件指纹**
  （`event_type | place | data 稳定序列化`）查冷却表：同一指纹
  **10 分钟内**（`event_cooldown_s=600`，dataclass 字段可调）已放行过 →
  跳过决策只记日志；
- 放行即记指纹（decision 返回 None 也算已处理——重复 LLM 打分同样浪费）；
- 未达阈值的事件不记指纹（弱事件升级后仍正常触发）；
- 冷却表超 200 条自动清理已出窗旧指纹；
- 时钟走 `now_fn`（可注入），测试可模拟冷却到期。

**行为影响**：persist 注入态每轮 poll 重发的同一满房/暴雨/排队事件不再反复
replan；换宿后的新酒店 place 不同、指纹不同，不受影响。

### 修复 3：满房循环终结 —— 治死循环源头

**文件**：`booking/booking_manager.py`、`django_server/runtime/agent_runtime.py`

- `confirm` 提交失败且 `booking_type == "hotel"` 时，把酒店名归一化键
  （剥「满房」标记，口径同 `_resolve_hotel_id`）记入 `_failed_hotels`；
- `prepare(booking_type="hotel")` 命中满房表直接 `RuntimeError` 拒绝——
  approve 端点把动作置 BLOCKED，不再产生新预约单/新卡片；
- `AgentRuntime._record_decision` 回填重规划动作时过滤已知满房酒店的
  HOTEL_BOOK 卡片（`is_hotel_failed`），换宿后的新酒店正常入队；
- `snapshot()`/`restore_snapshot()` 只增键 `failed_hotels`（M2 恢复不丢）。

## 三、改动文件清单

| 文件 | 内容 |
|---|---|
| `django_server/runtime/agent_runtime.py` | 后台重规划池 + `replan_in_progress` + 动作过滤 + status 只增字段 |
| `django_server/api/views.py` | `booking_confirm` 失败响应只增 `replan_async`/`replan_in_progress` |
| `execution/execution_agent.py` | 事件指纹冷却（`event_cooldown_s`/`_event_fingerprint`/`_prune_fingerprints`） |
| `booking/booking_manager.py` | `_failed_hotels` + `is_hotel_failed` + prepare 拒绝 + snapshot 只增键 |
| `tests/test_confirm_link_fixes_20260916.py` | 新增 13 个回归用例（全过） |
| `tests/test_runtime_persist.py` | 快照断言适配只增键 `failed_hotels`（空会话恒空列表） |
| `tests/test_replan_actions.py` | BOOKING 事件缓冲断言改为等后台任务收口（异步化后事件不再同步缓冲） |

**契约影响：只增不改**（响应新增 3 个字段 + snapshot 新增 1 个键），C 端与
持久化旧数据零破坏。

## 四、测试基线（2026-09-16 改动后）

- A 侧（工作区）：816 passed（未受影响，A 侧代码零改动）；
- B 侧（本仓库）：**668 passed**（656 基线 + 13 新增回归，2 例 rollinggo flaky
  移出统计）+ 2 处既有测试适配（见上表）；
- 已知 flaky：`tests/test_rollinggo_client.py::TestRetry::test_auth_error_not_retried`
  单跑也稳定失败（`RollingGoClient._start_loop` 的 `future.result(timeout=1)`
  超时，环境相关），与本次改动无关，**已移交 B 侧排查**（同 rollinggo 序跑
  超时问题家族）。

## 五、部署与验证建议

1. 部署后用满房注入验证闭环：`/api/debug/inject/`（hotel_full）或
   prepare 一个「XX（满房）」→ confirm——预期 confirm 立即 400，
   `/api/plan-trace/` 出现重规划步骤流，actions 里同一酒店卡片只出现一次；
2. 同一满房事件重复注入：10 分钟内不应出现第二次 replan（服务端日志可见
   "within 600s cooldown, skip decision"）；
3. C 端展示层（BLOCKED/旧卡片折叠）待前端处理（方案第 4 项，队友侧）。
