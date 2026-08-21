# TravelAgent Django 单用户 Demo

把 B 侧（工具 / 执行 / 预约 / 导出）嵌入 Django，并预留 A 侧 Decision Engine 接入点。

## 运行

```bash
cd django_server
pip install -r requirements.txt
python manage.py runserver 8000
```

访问：

- 健康检查：`GET http://127.0.0.1:8000/api/health/`
- 工具列表：`GET http://127.0.0.1:8000/api/tools/`
- 设置时间轴：`POST http://127.0.0.1:8000/api/timeline/`

## API 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health/` | 健康检查 |
| GET | `/api/status/` | 运行时状态汇总 |
| GET | `/api/agent/` | ExecutionAgent 内部规则/地点信息 |
| GET | `/api/profile/` | 用户画像占位（A 侧接入后填充） |
| GET | `/api/tools/` | 工具列表（names + specs） |
| GET | `/api/tools/{name}/` | 单个工具元数据 |
| POST | `/api/tools/invoke/` | LLM 友好工具调用（只读白名单） |
| POST | `/api/tools/{name}/invoke/` | 工具调用 |
| GET/POST | `/api/timeline/` | 获取/设置行程时间轴 |
| GET | `/api/timeline/history/` | 行程版本历史 |
| POST | `/api/booking/prepare/` | 准备预约 |
| POST | `/api/booking/{id}/confirm/` | 确认预约 |
| POST | `/api/booking/{id}/cancel/` | 取消预约 |
| POST | `/api/booking/{id}/payment/` | 付款提醒 |
| GET | `/api/booking/` | 预约列表 |
| GET | `/api/booking/{id}/` | 预约详情 |
| GET | `/api/actions/` | Action Queue |
| POST | `/api/actions/{id}/approve/` | 批准动作 |
| POST | `/api/actions/{id}/reject/` | 拒绝动作 |
| GET | `/api/events/?since=` | 事件历史（增量） |
| GET | `/api/replans/` | 决策/重规划历史 |
| GET | `/api/replans/{id}/` | 单次决策详情 |
| GET | `/api/tool-calls/` | 工具调用历史 |
| POST | `/api/execution/poll/` | 手动轮询 |
| POST | `/api/execution/lookahead/` | 手动到达前检查 |
| GET | `/api/export/ics/?raw=1` | 导出 ICS 文本（raw 为直接下载） |
| GET | `/api/export/markdown/?raw=1` | 导出 Markdown 文本（raw 为直接下载） |
| GET | `/api/config/` | 当前配置 |
| POST | `/api/config/reload/` | 热更新配置 |

## A 侧接入点

A 的 Decision Engine 接入位置：

- `runtime/a_interface.py`
  - `ADecisionEngine`：A 必须实现的接口
  - `PlaceholderDecisionEngine`：当前 Demo 用的 B 侧 stub
  - `build_decision_hook()`：A 替换这里即可

- `runtime/agent_runtime.py`
  - `AgentRuntime._get_decision_hook()` 会调用 `build_decision_hook()`
  - `ExecutionAgent.tool_provider` 已注入，A 的 LLM 后续可直接调用工具

A 正式接入示例：

```python
# runtime/a_interface.py 中替换
def build_decision_hook():
    return MyLLMDecisionEngine(tool_provider=runtime.tool_provider)
```

## 数据说明

单用户 Demo 使用内存态数据，不落库：

- `runtime.agent_runtime.runtime.events`：事件列表
- `runtime.agent_runtime.runtime.booking_manager`：预约 / ActionQueue
- `runtime.agent_runtime.runtime.timeline`：当前时间轴

后续如需多用户 / 持久化，可将这些状态迁移到 Django ORM / Redis。
