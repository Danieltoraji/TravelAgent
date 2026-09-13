# sync_notes：多用户改造（2026-09）

> 一页速览：B 侧服务从「单用户内存单例」升级为「多用户」——**账号体系（注册/登录）+ 每用户隔离运行时 + SQLite 持久化（重启恢复）+ 并发不再互相阻塞**。
> **A 侧 a_side 零改动**；**C 端需要小改（登录 + 每请求带 token，约 3 行）**。

---

## 一页速览

| 角色 | 需要做的 | 仅知悉 |
| --- | --- | --- |
| **C** | ① 对接 `POST /api/auth/register/`、`POST /api/auth/login/` 拿 token；② `api.ts` 的 `fetchJSON` 统一加 `Authorization: Bearer <token>` 头；③ 处理 401（跳登录页）；④ 切换账号时重置 `eventCursor` 并 `clearSession()` | token 存 localStorage；请求体结构**零变化**；轮询契约（events?since / replans 计数触发刷新）语义不变 |
| **A** | 无 | a_side 零改动（requirement 改为 B 侧 `a_interface.build_*` 显式传参）；`USE_LLM_TOOLS`/`USE_LIVE_DATA`/API key 仍是进程级共享开关 |

**全部 API 无 breaking**：所有既有端点路径/请求体/响应体不变，只多了 401 门禁和两个新端点。

---

## 一、账号与鉴权（对 C）

### 新端点

| 方法 | 路径 | 认证 | 说明 |
| --- | --- | --- | --- |
| POST | `/api/auth/register/` | 免 | `{username, password}` → `{token, username}`；重名 409；密码 ≥6 位 |
| POST | `/api/auth/login/` | 免 | `{username, password}` → `{token, username}`；错密码 401 |
| GET | `/api/auth/me/` | 需 | `{username, user_id, has_plan}` |
| POST | `/api/auth/logout/` | 需 | 删除当前 token |

- 传输方式：`Authorization: Bearer <token>` 请求头（**不用 cookie/session**，规避 CSRF 与 APK 明文 http 的 cookie 问题）。
- **单设备语义**：注册/登录都轮换旧 token——同账号同时只有一个有效 token，后登录挤掉先登录。
- token 只在签发响应里出现一次，服务端只存 sha256；密码 PBKDF2 存储。
- 白名单（免认证）：`/api/health/`、`/api/auth/register/`、`/api/auth/login/`；**其余全部端点无有效 token → 401** `{"error": "unauthorized（…）"}`。

### C 端最小改动（`src/services/api.ts`，约 3 行）

```ts
// fetchJSON 里 headers 组装处加一行：
const token = localStorage.getItem("voyageai.token");
if (token) headers["Authorization"] = `Bearer ${token}`;
```

配套：登录页拿到的 token 写 localStorage（可挂进现有 `session.ts` 体系）；`fetchJSON` 的非 2xx 分支对 `status===401` 跳登录；**切换账号时**重置 `eventCursor`（api.ts 模块级游标）并调 `clearSession()`，否则 since 游标/UI 现场会串到别的账号。

## 二、每用户隔离语义（对 C）

每个登录用户独立持有：行程 timeline、requirement、监控事件（`/api/events/?since=` 游标语义不变，按用户各自计数）、决策历史（`/api/replans/`）、预约与 ActionQueue、演示注入的 MockWorld override（`persist_world` 只影响自己）。

- 任何用户 `POST /api/plan/` **不再清空其他用户**的会话（旧行为是全局重置）。
- `decided_by` 从硬编码 `"c_end_user"` 变为真实用户名；`/api/profile/` 返回真实用户名。
- 服务端重启：用户下次带 token 请求时自动恢复完整会话（timeline/预约/事件游标），前端无感；`/api/auth/me/` 的 `has_plan` 可用于启动时判断是否需要重新规划。
- 会话内存上限 100 用户 / 2 小时不活跃淘汰（淘汰只丢内存，数据在 DB，下次请求自动重建）。

## 三、部署变化（对 A/运维知悉）

- gunicorn：`--workers 1 --threads 8`（gthread）。**workers 必须永远为 1**（每用户运行时注册表与 sqlite 都是单进程设计），threads 才是一个用户 20-70s 规划不阻塞其他用户的关键。
- 容器启动先 `manage.py migrate`（新表：`api_authtoken`、`api_trip` + Django auth 表）。
- sqlite 路径环境变量 `TRAVELAGENT_DB_PATH`（compose 已挂 named volume `travelagent-data:/app/django_server/data/`，容器重建数据不丢）。
- `BOOKING_PERSIST_PATH`（E5 文件持久化）降级为 legacy 兼容：HTTP 用户的预约状态一律走 Trip 表；该文件仅剩进程内单例（smoke 清单 2）在用。
- 部署冒烟新增清单 0（401/token）与清单 5（双用户隔离），并额外跑一个第二用户的完整 plan（smoke 总时长 +30~70s）。

## 四、A 侧边界确认

- **a_side 目录零改动**：多用户隔离全部在 B 侧完成。唯一的接缝是 B 侧 `django_server/runtime/a_interface.py` 的 `build_decision_hook/build_chat_hook` 增加 `requirement` 显式参数（每个运行时传各自的），A 侧函数签名无感知。
- BDecisionHook 重启安全：`_current_plan` 首次 replan 时从 `req.current_timeline` 逆向重建（A 侧既有机制），无需持久化。
- 进程级共享（多用户同权）：`USE_LLM_TOOLS`/`USE_LIVE_DATA`/LLM key/高德 key 与其 QPS 额度。QuotaManager 是每运行时实例，全局 QPS 纪律靠 amap_client 10021 退避兜底——并发规划时真源调用量按用户数放大，演示规模（数十用户）可控。

## 五、测试与验收

- 新增 `tests/test_auth_api.py`（门禁/账号生命周期）、`tests/test_multiuser.py`（双用户隔离 + manager 淘汰）、`tests/test_runtime_persist.py`（snapshot/restore/Trip 往返）；Django 引导在 `tests/conftest.py` 的 `pytest_configure`（临时 sqlite + migrate，全进程只 configure 一次）。
- 既有 6 个 Django 相关测试文件迁移到「每用例独立 AgentRuntime + request.runtime」模式（视图不再有模块级单例；`runtime.agent_runtime.runtime` 单例保留标记 deprecated）。
- 服务器冒烟 `smoke/smoke_acceptance.py`：所有 HTTP 调用带 smoke 用户 token；`/api/health/` 仍公开。

## 六、已知限制（明示不做）

- 明文 http 传输 token（与 APK 现状一致）；SECRET_KEY 硬编码、DEBUG=True 为既有债务。
- 不做多 worker 横向扩展（会话串号）、不做 Trip 历史（每用户仅保留当前行程，新 plan 覆写）、无注册审批/邮箱验证。
