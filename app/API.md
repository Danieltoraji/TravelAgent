# TravelAgent API 接入文档（B → C）

> 本文档面向 **C（产品负责人）**，说明 B 侧 FastAPI 服务层的全部端点、请求/响应格式和典型工作流。
>
> 服务地址：`http://localhost:8000`（开发环境）
> Swagger UI：`http://localhost:8000/docs`
> CORS：已配置允许所有源（开发环境）

---

## 启动服务

```bash
pip install -r requirements.txt
uvicorn app.service:app --reload --port 8000
```

---

## 数据结构

### TripTimeline（行程时间轴）

```json
{
  "city": "北京",
  "start_date": "2026-08-01",
  "end_date": "2026-08-02",
  "days": [
    {
      "day": 1,
      "date": "2026-08-01",
      "items": [
        {
          "name": "故宫",
          "lat": 0.0,
          "lng": 0.0,
          "category": "scenic",
          "arrival": "09:00",
          "open_time": "09:00-17:00",
          "queue_min": 20,
          "ticket_required": true,
          "price": 60.0
        }
      ]
    }
  ]
}
```

### BookingRecord（预约记录）

```json
{
  "booking_id": "A1B2C3D4",
  "place": "故宫",
  "target_date": "2026-08-01",
  "party_size": 2,
  "status": "pending_confirm",
  "payment_required": true,
  "note": "信息已填写完毕，等待用户确认后提交；付款需用户手动完成。",
  "booking_type": "scenic",
  "price": 60.0,
  "tel": "4009501925",
  "ticket_required": true,
  "address": "景山前街4号",
  "open_hours": "08:30-17:00",
  "confirm_code": ""
}
```

**status 枚举值**：`draft` → `pending_confirm` → `submitted` → `confirmed` / `failed` / `cancelled`

### ActionItem（Action Queue 项）

```json
{
  "action_id": "act-A1B2C3D4",
  "title": "预约 故宫（2026-08-01，2人）",
  "description": "信息已填写完毕...",
  "status": "pending",
  "permission": "confirm",
  "target": "booking:A1B2C3D4",
  "created_at": "2026-08-06T00:10:26"
}
```

**status 枚举值**：`pending` → `approved` / `rejected` / `executed` / `blocked`
**permission 枚举值**：`auto`（自动执行）/ `confirm`（需用户确认）/ `manual`（提醒用户自己执行）

### MonitorEvent（监控事件）

```json
{
  "event_id": "weather-0001",
  "event_type": "weather",
  "place": "北京",
  "observed_at": "2026-08-06T00:10:24",
  "rule_name": "weather-poll",
  "data": { "condition": "晴", "temperature_c": 28.0, "rain_probability": 10 },
  "impact_score": 0.0
}
```

---

## 端点一览

### 基础

#### `GET /health`

健康检查。

**响应**：
```json
{ "status": "ok", "project": "TravelAgent" }
```

#### `GET /tools`

列出所有已注册工具。

**响应**：
```json
{ "tools": ["booking", "food", "map", "scenic", "traffic", "weather", ...] }
```

#### `POST /tools/{name}/invoke`

调用指定工具。

**请求体**：工具参数（key-value）

**示例**：
```bash
POST /tools/weather/invoke
{ "city": "北京" }
```

**响应**：`ToolResult` 的完整 JSON

---

### 行程时间轴

#### `GET /timeline`

获取当前行程时间轴。

**响应**：`TripTimeline` JSON

**错误**：未设置时间轴时返回 `400`

#### `POST /timeline`

设置/替换行程时间轴，同时初始化 ExecutionAgent。

**请求体**：`TripTimeline` JSON

**示例**：
```bash
POST /timeline
{
  "city": "北京",
  "start_date": "2026-08-01",
  "end_date": "2026-08-02",
  "days": [
    {
      "day": 1,
      "date": "2026-08-01",
      "items": [
        {"name": "故宫", "category": "scenic", "arrival": "09:00",
         "ticket_required": true, "price": 60.0}
      ]
    }
  ]
}
```

**响应**：
```json
{ "status": "ok", "message": "Timeline set", "timeline": { ... } }
```

---

### 预约管理

#### `POST /booking/prepare`

准备预约（自动调用 scenic Tool 填充景点信息）。

**请求体**：
```json
{
  "place": "故宫",
  "target_date": "2026-08-01",
  "party_size": 2,
  "booking_type": "scenic"
}
```

**响应**：`BookingRecord` JSON

#### `POST /booking/{booking_id}/confirm`

用户确认 → 提交预约（调用 submit，生成 confirm_code）。

**响应**：更新后的 `BookingRecord` JSON（status=submitted, confirm_code 非空）

**错误**：`404` 预约不存在 / `400` 状态不可确认 / `500` 提交失败

#### `POST /booking/{booking_id}/cancel`

取消预约。

**响应**：更新后的 `BookingRecord` JSON（status=cancelled）

#### `POST /booking/{booking_id}/payment`

生成付款提醒（人工执行，permission=MANUAL）。

**响应**：`ActionItem` JSON

#### `GET /booking`

列出所有预约记录。

**响应**：
```json
{ "bookings": [ ... ], "count": 3 }
```

#### `GET /booking/{booking_id}`

查询单条预约记录。

**错误**：`404` 预约不存在

---

### Action Queue

#### `GET /actions`

列出所有 ActionItem（预约确认 + 付款提醒）。

**响应**：
```json
{ "actions": [ ... ], "count": 2 }
```

#### `POST /actions/{action_id}/approve`

C 用户确认 ActionItem → 标记为 `approved`。

**响应**：更新后的 `ActionItem` JSON

#### `POST /actions/{action_id}/reject`

C 用户拒绝 ActionItem → 标记为 `rejected`。

**响应**：更新后的 `ActionItem` JSON

---

### 监控事件

#### `GET /events?since={index}`

获取事件历史。可选 `since` 参数做增量查询。

**参数**：
- `since`（可选，默认 0）：返回 index 之后的事件

**响应**：
```json
{
  "events": [ ... ],
  "count": 2,
  "total": 5
}
```

#### `POST /execution/poll`

手动触发一次轮询（Demo/调试用）。

**响应**：
```json
{ "status": "ok", "events": [ ... ], "count": 2 }
```

#### `POST /execution/lookahead`

手动触发到达前检查（Demo/调试用）。

> **自动预约**：触发后，对 `ticket_required=True` 的景点和所有餐厅自动调用 `BookingManager.prepare()`，
> 产出 `PENDING` 状态的 ActionItem 供 C 端确认。同一地点不重复预约。

**可选 body**：
```json
{ "now": "2026-08-01T08:45:00" }
```
不传 `now` 则用当前时间。

**响应**：
```json
{ "status": "ok", "events": [ ... ], "count": 1 }
```

---

### 导出

#### `GET /export/ics`

导出 .ics 日历文件（可导入 Google Calendar / Outlook）。

**响应**：`Content-Type: text/calendar`，.ics 文本

#### `GET /export/markdown`

导出 Markdown 行程单。

**响应**：`Content-Type: text/markdown`，Markdown 文本

### 配置

#### `POST /config/reload`

热更新配置：重新从环境变量 + `config/local_settings.py` 读取 API Key 等。

无需重启服务即可切换 Mock ↔ Real 模式。

**响应**：

```json
{
  "status": "ok",
  "demo_mode": false,
  "use_real_api": true,
  "use_real_map_api": true
}
```

---

## 典型工作流

### 1. 初始化行程

```bash
POST /timeline
{ "city": "北京", "start_date": "2026-08-01", "end_date": "2026-08-02", "days": [...] }
```

### 2. 监控（手动触发或轮询）

```bash
# 手动触发一次轮询
POST /execution/poll

# 查看事件
GET /events
```

### 3. 预约闭环

#### 方式 A：自动预约（推荐）

到达前检查自动为需要预约的景点和餐厅准备预约：

```bash
# 触发到达前检查（自动产出 ActionItem）
POST /execution/lookahead
{ "now": "2026-08-01T08:45:00" }

# 查看自动产出的 Action Queue
GET /actions

# 用户确认预约
POST /booking/{booking_id}/confirm

# 生成付款提醒
POST /booking/{booking_id}/payment

# 查看所有预约
GET /booking
```

#### 方式 B：手动预约

```bash
# 手动准备预约（自动填充景点信息）
POST /booking/prepare
{ "place": "故宫", "target_date": "2026-08-01", "party_size": 2 }

# 查看 Action Queue
GET /actions

# 用户确认预约
POST /booking/{booking_id}/confirm
```

### 4. 导出

```bash
# 导出日历
GET /export/ics

# 导出行程单
GET /export/markdown
```

---

## 注意事项

1. **付款必须人工**：`POST /booking/{id}/payment` 只生成提醒（permission=MANUAL），Agent 不代付。
2. **ExecutionAgent 延迟初始化**：必须先 `POST /timeline` 设置行程，才能调用 `/execution/*` 端点。
3. **事件缓冲在内存**：服务重启后事件历史清空。
4. **CORS 已配置**：开发环境允许所有源跨域调用。
5. **Swagger UI**：访问 `http://localhost:8000/docs` 可查看交互式 API 文档。
