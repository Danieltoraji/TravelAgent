# A 侧接入 hotel_tool 适配文档

> 本文档说明 B 侧已交付的 `hotel_tool` 如何接入 A 侧规划/重规划链路。
> **B 侧未改动任何 A 代码**；以下为 A 后续适配时需要做的改动。
>
> 当前状态：B 侧 `hotel_tool` 已完成并验证，A 侧现有 `make_live_hotel_provider` 已可直接消费 B 的 `hotel_tool`。

---

## 一、B 侧已交付内容

### 1. 工具

```text
hotel_tool
```

支持 3 个 action：

| action | 对应 RollingGo MCP | 说明 |
|---|---|---|
| `search` | `searchHotels` | 搜索酒店 |
| `detail` | `getHotelDetail` | 查询单个酒店房型价格 |
| `tags` | `getHotelSearchTags` | 获取搜索标签 |

### 2. 调用方式

```python
from tools import default_registry

# 搜索（兼容 A 现有调用：city=...）
default_registry.call("hotel", action="search", city="北京")

# 或显式 place
default_registry.call(
    "hotel",
    action="search",
    place="北京",
    placeType="城市",
    checkInDate="2026-09-01",
    stayNights=1,
    adultCount=2,
    size=5,
)

# 房型明细
default_registry.call(
    "hotel",
    action="detail",
    hotelId=43586,
    checkInDate="2026-09-01",
    checkOutDate="2026-09-02",
    adultCount=2,
    roomCount=1,
)
```

### 3. search 标准化输出

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
  "count": 5
}
```

---

## 二、A 侧现状

A 侧已经存在一个适配器：

```python
# a_side/data_transmission/live_data.py
def make_live_hotel_provider(tool_provider):
    def hotel_provider(city):
        result = tool_provider.call("hotel", city=city)
        ...
    return hotel_provider
```

该适配器当前调用：

```python
tool_provider.call("hotel", city=city)
```

B 侧 `hotel_tool` 已做兼容：

- 支持 `city` 参数作为 `place` 的别名
- 默认 `placeType="城市"`
- 输出字段已对齐 `_normalize_live_hotel` 预期

**因此 A 现有 `make_live_hotel_provider` 无需修改即可使用 B 的 `hotel_tool`。**

---

## 三、A 侧后续需要做的改动

### 1. 让 `BPlannerHook` 使用真源酒店池

文件：

```text
a_side/call_llm/b_planner_hook.py
```

当前 `_live_hotel_pool()` 固定读假池：

```python
self._live_hotel_pool_cache = list(load_hotels(self.city))
```

改为真源优先：

```python
def _live_hotel_pool(self):
    if getattr(self, "_live_hotel_pool_cache", None) is None:
        self._live_hotel_pool_cache = self._load_live_hotels_with_fallback()
    return self._live_hotel_pool_cache

def _load_live_hotels_with_fallback(self):
    if self._use_live and self._tool_provider is not None:
        try:
            from data_transmission.live_data import make_live_hotel_provider
            hotels = list(make_live_hotel_provider(self._tool_provider)(self.city))
            if hotels:
                return hotels
        except Exception:
            pass
    from data_transmission.hotel import load_hotels
    return list(load_hotels(self.city))
```

### 2. 让 `_attach_hotels()` 透传 `hotel_provider`

当前调用：

```python
select_hotels_for_plan(
    self.requirement,
    plan,
    travel_time_provider=self._travel_time_provider,
)
```

改为：

```python
select_hotels_for_plan(
    self.requirement,
    plan,
    hotel_provider=self._live_hotel_provider_or_none(),
    travel_time_provider=self._travel_time_provider,
)
```

新增辅助方法：

```python
def _live_hotel_provider_or_none(self):
    if self._use_live and self._tool_provider is not None:
        from data_transmission.live_data import make_live_hotel_provider
        return make_live_hotel_provider(self._tool_provider)
    return None
```

### 3. 让 `BDecisionHook` 接收 `tool_provider`

文件：

```text
a_side/call_llm/b_decision_hook.py
```

构造函数增加：

```python
def __init__(
    self,
    requirement,
    *,
    tool_provider=None,
    ...
):
    self.tool_provider = tool_provider
```

### 4. 让 `BDecisionHook` 重规划时透传 `hotel_provider`

在 `__call__` 中：

```python
hotel_provider = None
if self.tool_provider is not None:
    try:
        from data_transmission.live_data import make_live_hotel_provider
        hotel_provider = make_live_hotel_provider(self.tool_provider)
    except Exception:
        hotel_provider = None

result = self._replan(
    self.requirement,
    current_plan,
    spots,
    a_events,
    hotel_provider=hotel_provider,
)
```

### 5. 让 `replanner.replan()` 支持 `hotel_provider`

文件：

```text
a_side/algorithoms/replanner.py
```

- `replan()` 增加参数：

```python
def replan(
    ...,
    hotel_provider=None,
):
```

- `_replan_hotels()` 增加参数：

```python
def _replan_hotels(
    ...,
    hotel_provider=None,
):
```

- `_replan_hotels()` 内传给 `select_hotels_for_plan`：

```python
new_acc = select_hotels_for_plan(
    ...,
    hotel_provider=hotel_provider,
)
```

- `replan()` 内透传：

```python
hotel_changes, new_accommodation = _replan_hotels(
    ...,
    hotel_provider=hotel_provider,
)
```

### 6. 让 `a_interface.py` 把 `tool_provider` 传给 `BDecisionHook`

文件：

```text
django_server/runtime/a_interface.py
```

当前 `build_decision_hook()` 已接收 `tool_provider`，但构造 `BDecisionHook` 时未传入。改为：

```python
return BDecisionHook(
    requirement=requirement,
    start_date=...,
    tool_provider=tool_provider,
    ...
)
```

---

## 四、可选：A 的 LLM 在决策阶段直接调用 hotel_tool

如果 A 的 LLM 需要在“影响评分 / 决策”阶段主动查酒店，可以扩展：

### `a_side/call_llm/decision_engine.py`

```python
def decide_replan(
    requirement,
    events,
    *,
    tool_provider=None,
    ...
):
```

在构造 prompt 前调用：

```python
hotel_info = tool_provider.call_json(
    "hotel",
    {"action": "search", "city": "北京", "size": 5},
)
```

把结果拼入：

```python
【酒店信息】
...
```

### `BDecisionHook._decide()` 透传

```python
return decide_replan(
    ...,
    tool_provider=self.tool_provider,
)
```

---

## 五、B 侧已完成验证

### 1. 单元测试

```text
tests/test_hotel_tool.py
```

覆盖：

- Mock search / detail / tags
- Live 参数构造
- `city` 别名兼容
- RollingGo 返回标准化
- `ToolProvider` 白名单包含 `hotel`

### 2. 真实 Key 验证

已用真实 RollingGo Key 验证：

- MCP 连接成功
- `searchHotels` 返回真实酒店
- `getHotelDetail` 返回真实房型
- A 现有 `make_live_hotel_provider` 直接消费 B `hotel_tool` 成功，返回 `List[Hotel]`

---

## 六、涉及文件汇总

| 文件 | 归属 | 是否由 B 修改 |
|---|---|---|
| `tools/hotel_tool.py` | B | ✅ 已交付 |
| `tools/rollinggo_client.py` | B | ✅ 已交付 |
| `tools/__init__.py` | B | ✅ 已交付 |
| `config/settings.py` | B | ✅ 已交付 |
| `django_server/requirements.txt` | B | ✅ 已交付 |
| `.env.example` | B | ✅ 已交付 |
| `a_side/data_transmission/live_data.py` | A | ❌ 未改，A 后续可自行确认 |
| `a_side/call_llm/b_planner_hook.py` | A | ❌ 未改，A 后续需按本文档修改 |
| `a_side/call_llm/b_decision_hook.py` | A | ❌ 未改，A 后续需按本文档修改 |
| `a_side/algorithoms/replanner.py` | A | ❌ 未改，A 后续需按本文档修改 |
| `django_server/runtime/a_interface.py` | A/B 边界 | ❌ 未改，A 后续需按本文档修改 |

---

## 七、验收标准

A 完成适配后，应满足：

1. `BPlannerHook._live_hotel_pool()` 返回 RollingGo 真源酒店
2. `BPlannerHook.last_data_source == "live"` 时，时间轴酒店段来自真源
3. 酒店满房 → `replan()` 使用 RollingGo 候选酒店换宿，而不是假池
4. 真源失败时自动回退假池，不崩溃
5. A 的 LLM 可通过 `tool_provider.call_json("hotel", ...)` 查询酒店
