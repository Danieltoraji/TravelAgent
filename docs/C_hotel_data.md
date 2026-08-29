# C 端酒店数据暴露文档

> 本文档说明 `hotel_tool` 调用过程中产生的数据，以及 C 端（Android/Web）如何**只读展示**。
>
> **设计原则**：C 是展示平台，不直接操作工具。
> `hotel_tool` 由 A 的 LLM/Planner、B 的定时机制调用；
> B 在调用时自动缓存结果，C 通过 GET 接口查看缓存数据。

---

## 一、hotel_tool 会产生哪些数据

### 1. 酒店搜索（`action=search`）

对应 RollingGo `searchHotels`，返回：

| 字段 | 说明 |
|---|---|
| `hotels` | 酒店列表 |
| `count` | 酒店数量 |
| `raw` | RollingGo 原始返回（**仅 Live 版**有此键；Mock 版搜索无 `raw`） |

每个酒店包含：

| 字段 | 说明 |
|---|---|
| `id` | 酒店 ID |
| `name` | 酒店中文名 |
| `name_en` | 酒店英文名 |
| `brand` | 品牌 |
| `location.lat` | 纬度 |
| `location.lng` | 经度 |
| `star` | 星级 |
| `rating` | 评分（可能为 null） |
| `price_per_night` | 最低每晚价格 |
| `address` | 地址 |
| `tags` | 标签列表 |
| `booking_url` | 预订落地页 |
| `image_url` | 图片 |
| `open` | 是否开放（默认 true） |

### 2. 酒店房型明细（`action=detail`）

对应 RollingGo `getHotelDetail`，返回（Mock 与 Live 完全同构，C1 修复后口径）：

| 字段 | 说明 |
|---|---|
| `hotelId` | 酒店 ID |
| `name` | 酒店名 |
| `starRating` | 星级 |
| `checkIn` / `checkOut` | 入离日期 |
| `bookingUrl` | 预订落地页 |
| `rooms` | 房型/价格计划列表（snake_case） |
| `raw` | RollingGo 原始返回（Live 版；Mock 版为等效的原始形状数据） |

每个 `rooms` 元素包含：

| 字段 | 说明 |
|---|---|
| `room_name` | 房型名 |
| `rate_plan_id` | 价格计划 ID |
| `rate_plan_name` | 价格计划名 |
| `average_price` | 平均每晚价格 |
| `currency` | 币种 |
| `meal_amount` | 早餐份数 |
| `meal_type` | 餐食说明 |
| `on_request` | 是否需二次确认 |
| `cancel_policy` | 取消政策 |
| `cancelable` | 是否可免费取消 |
| `room_info` | 房型信息：`has_wifi` / `has_window` / `max_occupancy` / `size` / `floor` / `smoking` / `images` |

> 历史注：2026-08-28（C1）之前 Mock 版返回 `roomRatePlans`（camelCase），与 Live
> 不一致；现已统一为上表的 `rooms`（snake_case）口径。

### 3. 搜索标签（`action=tags`）

对应 RollingGo `getHotelSearchTags`，返回酒店搜索元数据/标签。

### 4. 工具调用日志（`/api/tool-calls/`）

每次 `hotel_tool` 调用都会记录：

| 字段 | 说明 |
|---|---|
| `tool` | 固定 `hotel` |
| `arguments` | 调用参数 |
| `status` | ok / error |
| `source` | mock / live |
| `elapsed_ms` | 耗时 |
| `timestamp` | 调用时间 |
| `error` | 错误信息 |
| `data` | **完整返回数据** |

---

## 二、C 端如何获取

C 端只使用 **GET 只读接口**，不会触发新的工具调用。

### 1. 酒店搜索历史

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
        "hotels": [...],
        "count": 5
      }
    }
  ],
  "count": 1,
  "latest": { ... }
}
```

`latest` 是最新一次搜索快照。

### 2. 酒店房型明细

```http
GET /api/hotels/{hotelId}/
```

返回该酒店**已被查询过**的房型/价格明细；未查询过则 404。

### 3. 酒店搜索标签

```http
GET /api/hotel-tags/
```

返回最近一次 `hotel_tool tags` 调用的结果。

### 4. 工具调用历史

```http
GET /api/tool-calls/
```

返回所有工具调用记录，包含 `hotel_tool` 的完整 `data`，可用于展示“Agent 查了什么酒店”。

---

## 三、数据如何产生

```text
A 的 LLM / Planner
B 的定时监控 / 执行
        │
        ▼
调用 hotel_tool
        │
        ▼
RollingGo MCP
        │
        ▼
结果返回 B
        │
        ▼
AgentRuntime 自动缓存：
  - hotel_search_results
  - hotel_details
  - hotel_tags
  - tool_call_log
        │
        ▼
C 通过 GET 接口展示
```

C 不直接调用 `hotel_tool`，只读取 B 已经产生的数据。

---

## 四、接口汇总

| 接口 | 方法 | 说明 |
|---|---|---|
| `/api/hotels/` | GET | 酒店搜索历史快照 |
| `/api/hotels/{hotelId}/` | GET | 单个酒店房型明细（已缓存） |
| `/api/hotel-tags/` | GET | 酒店搜索标签（已缓存） |
| `/api/tool-calls/` | GET | 工具调用历史（含完整 data） |

---

## 五、环境配置

C 无需关心后端配置；是否真源由 B 侧环境变量控制：

```env
ROLLINGGO_MCP_URL=https://mcp.rollinggo.cn/mcp
ROLLINGGO_API_KEY=你的key
```

---

## 六、说明

- 如果 B/A 还没有调用过 `hotel_tool`，`/api/hotels/` 返回空列表，`/api/hotels/{id}/` 和 `/api/hotel-tags/` 返回 404。
- 酒店数据缓存是内存态，单用户 Demo 重启后清空。
