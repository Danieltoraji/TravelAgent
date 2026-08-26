# hotel_tool 文档

> 本文档系统整理 `hotel_tool` 的功能、实现原理、A 侧调用约定、C 侧展示接口。
> 面向 A/C 协作，越详细越好。

---

## 一、功能概述

`hotel_tool` 是 B 侧提供的一个**酒店信息查询工具**，封装了 RollingGo 酒店 MCP。

功能：

| 功能 | 说明 |
|---|---|
| 搜索酒店 | 按城市/地点、日期、人数、星级、价格、标签等条件搜索 |
| 查询房型明细 | 查询单个酒店实时房型、价格、餐食、取消政策 |
| 获取搜索标签 | 获取酒店搜索元数据/标签 |

它不负责：

- 酒店预订/下单
- 酒店支付
- 多用户数据管理

---

## 二、实现原理

### 2.1 整体结构

```text
A 的 LLM / Planner
B 的定时机制 / ExecutionAgent
        │
        ▼
ToolProvider / ToolRegistry
        │
        ▼
hotel_tool (HotelTool / HotelToolLive)
        │
        ▼
RollingGoClient (MCP 客户端)
        │
        ▼
RollingGo MCP Server
  https://mcp.rollinggo.cn/mcp
```

### 2.2 文件位置

| 文件 | 作用 |
|---|---|
| `tools/hotel_tool.py` | 工具定义：Mock / Live 两种实现 |
| `tools/rollinggo_client.py` | RollingGo MCP 客户端 |
| `tools/__init__.py` | 工具注册 |
| `config/settings.py` | RollingGo 配置 |
| `django_server/runtime/agent_runtime.py` | 调用日志 + 酒店数据缓存（C 展示用） |

### 2.3 Mock / Live 切换

- `HotelTool`：Mock，返回固定酒店列表，无需 Key
- `HotelToolLive`：真实调用 RollingGo

切换条件：

```python
settings.use_real_hotel_api
# = not demo_mode and bool(rollinggo_api_key)
```

### 2.4 MCP 客户端实现要点

`RollingGoClient`：

- 使用 MCP Streamable HTTP
- Bearer Token 认证
- 后台常驻事件循环 + 单 worker 复用 MCP `ClientSession`
- 支持超时、重试、错误分类
- 结果解析容错（`structured_content` / `text` JSON / raw）

错误类型：

| 类型 | 含义 | 是否重试 |
|---|---|---|
| `RollingGoAuthError` | 认证失败 401/403 | 否 |
| `RollingGoProtocolError` | 协议/解析错误 | 否 |
| `RollingGoTimeoutError` | 超时 | 是 |
| `RollingGoConnectionError` | 网络/连接错误 | 是 |

### 2.5 结果标准化

`hotel_tool` 会把 RollingGo 原始返回整理成统一结构，供 A/C 使用。

---

## 三、面向 A

### 3.1 A 如何调用

A 的 LLM / Planner 通过 `ToolProvider` 调用：

```python
# 方式一：call_json，返回 dict
result = tool_provider.call_json("hotel", {
    "action": "search",
    "city": "北京",
    "checkInDate": "2026-09-01",
    "stayNights": 1,
    "adultCount": 2,
    "size": 5,
})

# 方式二：call，返回 ToolResult
result = tool_provider.call("hotel", action="search", city="北京")
data = result.data
```

### 3.2 action 约定

| action | 对应 MCP | 用途 |
|---|---|---|
| `search` | `searchHotels` | 搜索酒店 |
| `detail` | `getHotelDetail` | 查询房型明细 |
| `tags` | `getHotelSearchTags` | 获取搜索标签 |

### 3.3 search 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `action` | string | 是 | `search` |
| `place` | string | 二选一 | 城市/机场/景点/酒店等 |
| `city` | string | 二选一 | 城市名，等价于 `place` 为城市 |
| `placeType` | string | 否 | 默认 `城市` |
| `originQuery` | string | 否 | 综合住宿意图描述 |
| `checkInDate` | string | 否 | YYYY-MM-DD |
| `stayNights` | integer | 否 | 晚数 |
| `adultCount` | integer | 否 | 成人数 |
| `starRatings` | array | 否 | 星级范围，如 `[4.5, 5.0]` |
| `maxPricePerNight` | number | 否 | 每晚价格上限 |
| `distanceInMeter` | integer | 否 | 距 POI 距离（米） |
| `preferredBrands` | array | 否 | 偏好品牌 |
| `requiredTags` | array | 否 | 必须命中的标签 |
| `size` | integer | 否 | 返回数量，默认 5，最大 20 |

示例：

```python
tool_provider.call_json("hotel", {
    "action": "search",
    "place": "北京",
    "placeType": "城市",
    "checkInDate": "2026-09-01",
    "stayNights": 1,
    "adultCount": 2,
    "starRatings": [4.0, 5.0],
    "maxPricePerNight": 1000,
    "size": 5,
})
```

### 3.4 search 返回格式

```json
{
  "hotels": [
    {
      "id": 43586,
      "name": "北京某酒店",
      "name_en": "...",
      "brand": "...",
      "location": {"lat": 40.06, "lng": 116.58},
      "star": 4.0,
      "rating": null,
      "price_per_night": 423.0,
      "address": "...",
      "tags": ["机场酒店"],
      "booking_url": "...",
      "image_url": "...",
      "open": true
    }
  ],
  "count": 5,
  "raw": { ... }
}
```

### 3.5 detail 请求参数

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `action` | string | 是 | `detail` |
| `hotelId` | integer | 二选一 | 酒店 ID |
| `name` | string | 二选一 | 酒店名称 |
| `checkInDate` | string | 否 | 入住日期 |
| `checkOutDate` | string | 否 | 离店日期 |
| `adultCount` | integer | 否 | 成人数 |
| `roomCount` | integer | 否 | 房间数 |
| `childCount` | integer | 否 | 儿童数 |
| `childAgeDetails` | array | 否 | 儿童年龄 |
| `cancelPolicy` | string | 否 | `CANCELABLE` / `NON_CANCELABLE` |
| `mealType` | string | 否 | `WITH_BREAKFAST` / `SINGLE_BREAKFAST` / `DOUBLE_BREAKFAST` / `NO_MEAL` |

示例：

```python
tool_provider.call_json("hotel", {
    "action": "detail",
    "hotelId": 43586,
    "checkInDate": "2026-09-01",
    "checkOutDate": "2026-09-02",
    "adultCount": 2,
    "roomCount": 1,
    "cancelPolicy": "CANCELABLE",
    "mealType": "NO_MEAL",
})
```

### 3.6 detail 返回格式

```json
{
  "hotelId": 43586,
  "name": "...",
  "starRating": 4.0,
  "checkIn": "2026-09-01",
  "checkOut": "2026-09-02",
  "bookingUrl": "...",
  "rooms": [
    {
      "room_name": "高级间-双床",
      "rate_plan_id": "...",
      "rate_plan_name": "...",
      "average_price": 443,
      "currency": "CNY",
      "meal_amount": 0,
      "meal_type": "不含早餐",
      "on_request": false,
      "cancel_policy": "...",
      "cancelable": true,
      "room_info": {
        "has_wifi": true,
        "has_window": true,
        "max_occupancy": 2,
        "size": "31-35",
        "floor": "1-4",
        "smoking": "不可吸烟",
        "images": "..."
      }
    }
  ],
  "raw": { ... }
}
```

### 3.7 tags 调用与返回

```python
tool_provider.call_json("hotel", {"action": "tags"})
```

返回 RollingGo `getHotelSearchTags` 的结果；`HotelToolLive` 内部有 1 小时 TTL 缓存。

### 3.8 与 A 现有 `make_live_hotel_provider` 的兼容

A 现有适配器：

```python
tool_provider.call("hotel", city=city)
```

`hotel_tool` 已兼容：

- `city` 自动作为 `place`
- 默认 `placeType="城市"`
- 返回的 `hotels` 列表可直接被 A 的 `_normalize_live_hotel` 消费

因此 A 现有 `make_live_hotel_provider` **无需修改**即可使用 B 的 `hotel_tool`。

---

## 四、面向 C

### 4.1 设计原则

C 是展示层，**不直接调用 hotel_tool**。

`hotel_tool` 由 A 的 LLM/Planner、B 的定时机制调用；B 在调用时自动缓存结果，C 通过 GET 只读接口查看。

### 4.2 C 可用接口汇总

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/hotels/` | GET | 酒店搜索历史快照 |
| `/api/hotels/{hotelId}/` | GET | 单个酒店房型明细（已缓存） |
| `/api/hotel-tags/` | GET | 酒店搜索标签（已缓存） |
| `/api/tool-calls/` | GET | 工具调用历史（含完整 data） |

### 4.3 GET /api/hotels/

请求：

```http
GET /api/hotels/
```

返回：

```json
{
  "hotel_search_results": [
    {
      "timestamp": "2026-08-27T12:00:00",
      "arguments": {
        "action": "search",
        "city": "北京",
        "checkInDate": "2026-09-01"
      },
      "data": {
        "hotels": [
          {
            "id": 43586,
            "name": "北京某酒店",
            "location": {"lat": 40.06, "lng": 116.58},
            "price_per_night": 423.0
          }
        ],
        "count": 5
      }
    }
  ],
  "count": 1,
  "latest": { ... }
}
```

说明：

- `hotel_search_results`：历史搜索快照列表
- `latest`：最近一次搜索快照
- 如果还没有调用过 `hotel_tool`，返回空列表

### 4.4 GET /api/hotels/{hotelId}/

请求：

```http
GET /api/hotels/43586/
```

返回（标准化 detail）：

```json
{
  "hotelId": 43586,
  "name": "...",
  "starRating": 4.0,
  "checkIn": "2026-09-01",
  "checkOut": "2026-09-02",
  "bookingUrl": "...",
  "rooms": [
    {
      "room_name": "高级间-双床",
      "average_price": 443,
      "currency": "CNY",
      "cancelable": true,
      "room_info": {
        "has_wifi": true,
        "size": "31-35"
      }
    }
  ],
  "raw": { ... }
}
```

说明：

- 只返回**已被查询过**的酒店
- 未查询过返回 404

### 4.5 GET /api/hotel-tags/

请求：

```http
GET /api/hotel-tags/
```

返回：

```json
{
  "tags": ["市中心", "近地铁", "含早餐"]
}
```

说明：

- 返回最近一次 `hotel_tool tags` 调用的结果
- 未调用过返回 404

### 4.6 GET /api/tool-calls/

请求：

```http
GET /api/tool-calls/
```

返回：

```json
{
  "tool_calls": [
    {
      "tool": "hotel",
      "arguments": {
        "action": "search",
        "city": "北京"
      },
      "status": "ok",
      "source": "live",
      "elapsed_ms": 123,
      "timestamp": "2026-08-27T12:00:00",
      "error": null,
      "has_data": true,
      "data": {
        "hotels": [...]
      }
    }
  ],
  "count": 1
}
```

说明：

- 展示所有工具调用记录
- `data` 是完整返回数据
- C 可以用它展示“Agent 查了哪些酒店”

---

## 五、A 端下一步任务

按 `docs/A_hotel_tool_adapter.md`，A 需要完成：

1. `BPlannerHook._live_hotel_pool()` 改为真源优先 + 假池回退
2. `BPlannerHook._attach_hotels()` 透传 `hotel_provider`
3. `BDecisionHook` 接收 `tool_provider`
4. `BDecisionHook` 重规划时透传 `hotel_provider`
5. `replanner.replan()` / `_replan_hotels()` 支持 `hotel_provider`
6. `a_interface.py` 把 `tool_provider` 传给 `BDecisionHook`

可选：

- A 的 LLM 在决策阶段通过 `tool_provider.call_json("hotel", ...)` 查询酒店

---

## 六、环境配置

```env
ROLLINGGO_MCP_URL=https://mcp.rollinggo.cn/mcp
ROLLINGGO_API_KEY=你的key
ROLLINGGO_MCP_TIMEOUT=30
ROLLINGGO_MCP_MAX_RETRIES=2
ROLLINGGO_MCP_RETRY_BACKOFF_BASE=1.0
```

---

## 七、相关文档

- `docs/A_hotel_tool_adapter.md`：A 侧适配指南
- `docs/C_hotel_data.md`：C 端酒店数据展示说明
- `docs/交付文档.md`：整体交付说明
