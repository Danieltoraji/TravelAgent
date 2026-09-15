# Sync Notes：服务端日志观测 + 历史规划归档（2026-09-15）

> 分支 `server_log_dxd_260915`。本轮补齐 B 侧（django_server）的日志观测与
> 历史规划回查能力。**接口契约不变**（既有端点的请求/响应结构零变化），
> 新增内容：响应头 `X-Request-ID`、两个查询端点、一张归档表（迁移 0002，
> 容器启动 `migrate --noinput` 自动执行）。

---

## C 端（产品/前端）

### 1. 无必须改动

既有端点的请求体与响应 JSON 完全不变。可选接入两项新能力：

### 2. 报障请带 X-Request-ID（强烈建议）

每个响应都带 `X-Request-ID: <8位hex>` 响应头。服务端该请求链路上的全部日志
（请求行、异常栈、工具调用）都带同一 id——**用户报障时收集这个 id，
B 侧可直接定位到完整日志链**（`docker logs` 与 `data/logs/server.log` 均可 grep）。

401 响应也带该头。

### 3. 历史规划查询（可选，新端点）

此前旧规划被新 plan 整行覆写后无处可查；现在每次新 plan 覆写前自动归档
（每用户保留最近 20 份）。

```bash
# 列表（requirement 全量 + timeline 天数概要；limit 默认 20，上限 100）
curl "http://<host>/api/plans/history/?limit=20" \
  -H "Authorization: Bearer <token>"

# 单份全量快照（requirement / timeline / events / replans /
# timeline_history / booking_state）
curl "http://<host>/api/plans/history/<id>/" \
  -H "Authorization: Bearer <token>"
```

- 列表项字段：`id` / `archived_at`（ISO 时间）/ `reason`（当前恒为
  `new_plan`）/ `requirement` / `timeline_days`；
- 归档的是「被覆写前」的旧会话快照：第一次 plan 不产生归档；
- 越权访问他人归档一律 404（不泄漏存在性）。

---

## A 端（智能决策）

### 1. 镜像纪律不变

本轮 **未改 `a_side/` 任何文件**。B 侧通过既有钩子在入口层补齐了 A 相关报错的记录：

- A 规划失败：`BPlannerHook.last_error` → `_last_planner_error` → B 侧
  `views.plan` 现以 ERROR 级日志落盘（含完整错误链），同时旧会话快照在
  覆写前已归档（规划失败也不丢）；
- 工具调用（A 规划/技能层经 registry 发起）：B 侧 wrapper 现对**每次**调用
  打一行 INFO（tool/参数概要/status/耗时），**抛异常的调用**也有
  logger.exception 并原样上抛——此前抛异常的调用完全无记录。

### 2. a_side 仍静默的模块（建议 A 侧在主目录补日志后重新镜像）

以下文件当前零日志输出（或定义了 logger 但未使用），深层过程日志需 A 侧自补：

| 文件 | 现状 |
| --- | --- |
| `a_side/algorithoms/planner.py` | 零日志 |
| `a_side/call_llm/b_chat_hook.py` | 零日志 |
| `a_side/call_llm/skill_runner.py` | 零日志（错误只结构化回填给 LLM 回路） |
| `a_side/call_llm/b_planner_hook.py` | 定义了 logger 但全文无调用（错误走 `last_error` 属性） |

`data_transmission/adapters.py`、`call_llm/llm_clients/BaseClient.py`、
`planner_parts/*` 已有 warning 级日志，会自动进入 B 侧双通道（根 logger 收口）。

---

## 运维 / 部署变化

| 项 | 内容 |
| --- | --- |
| LOGGING | `settings.py` 新增：stdout + `RotatingFileHandler` 双通道，格式
  `时间 级别 logger名 [request_id] 消息`；根 logger 收口（api/runtime/a_side
  的命名 logger 与未处理异常统一落双通道） |
| 日志文件 | 容器内 `/app/django_server/data/logs/server.log`（与 db.sqlite3 同一
  named volume），10MB×5 轮转；宿主机经 volume 直查 |
| 环境变量 | `TRAVELAGENT_LOG_DIR` 覆盖目录；`TRAVELAGENT_LOG_DISABLE=1` 关文件通道（测试） |
| gunicorn | 开启 access log（含 `%(L)s` 请求耗时秒数）与 error log，均走 stdout |
| docker 轮转 | compose `logging: json-file max-size 10m / max-file 3` 兜底 |
| 迁移 | `api/migrations/0002_tripplanarchive.py`，启动自动执行 |

**日志降噪说明**：GET 请求行记 DEBUG（poll/lookahead 每 5s 一轮，不刷 INFO）；
POST 请求行（用户/状态码/耗时）记 INFO。

---

## B 侧补记录清单（本轮改动面）

| 位置 | 补充 |
| --- | --- |
| `api/middleware.py` | request_id 生成/回写、401 拒绝日志、请求行日志（method/path/user/status/耗时） |
| `api/views.py` | 全部静默 except 补日志（500 类 exception、业务失败 warning、GET 404 info）；plan 拒绝（空 body/缺预算）记 INFO；畸形 JSON 体记 WARNING；plan 归档逻辑 + 两个 history 端点 |
| `api/auth.py` | 注册成功/冲突、登录成功/失败记 INFO |
| `runtime/agent_runtime.py` | 工具调用每次一行 INFO；抛异常的调用记 exception 后上抛 |
| `api/models.py` | `TripPlanArchive`（六 blob 与 Trip 同构 + reason/archived_at） |
| `api/logging_utils.py`（新） | request_id ContextVar + RequestIdFilter |

测试：`tests/test_server_log.py`（7 例）、`tests/test_plan_archive.py`（6 例）；
全量基线 622 passed + 3 例 ab_sync 环境挂（预期，见 README）。
