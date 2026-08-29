# TravelAgent 工具层接口文档

> 本文档介绍 TravelAgent 工具层的整体架构、各工具的调用接口、API 端点映射、以及返回字段对照关系。

---

## 目录

- [1. 架构总览](#1-架构总览)
- [2. 统一调用契约](#2-统一调用契约)
- [3. QWeatherClient 共享客户端](#3-qweatherclient-共享客户端)
- [4. 天气类工具（4 个 × Mock/Live 双版本）](#4-天气类工具4-个--mocklive-双版本)
  - [4.1 weather — 实况天气](#41-weather--实况天气)
  - [4.2 weather_warning — 天气预警](#42-weather_warning--天气预警)
  - [4.3 air_quality — 空气质量](#43-air_quality--空气质量)
  - [4.4 weather_forecast — 逐小时预报](#44-weather_forecast--逐小时预报)
- [5. 地图工具（Mock/Live 双版本）](#5-地图工具mocklive-双版本)
  - [5.1 AmapClient 共享客户端](#51-amapclient-共享客户端)
  - [5.2 map — 地图服务](#52-map--地图服务)
- [6. 其他领域工具](#6-其他领域工具)
  - [6.1 scenic — 景点状态（Mock/Live 双版本）](#61-scenic--景点状态)
  - [6.2 traffic — 交通状态（Mock/Live 双版本）](#62-traffic--交通状态)
  - [6.3 food — 餐饮推荐（Mock/Live 双版本）](#63-food--餐饮推荐)
  - [6.4 booking — 预约服务](#64-booking--预约服务)
  - [6.5 火车票查询组（Mock/Live 双版本）](#65-火车票查询组mocklive-双版本)
  - [6.6 hotel — 酒店查询（Mock/Live 双版本）](#66-hotel--酒店查询mocklive-双版本)
  - [6.7 web_fetch / web_search — 网页抓取与搜索（Mock/Live 双版本）](#67-web_fetch--web_search--网页抓取与搜索mocklive-双版本)
  - [6.8 技能（Skill）—— 面向意图的组合工具（0829 起）](#68-技能skill--面向意图的组合工具0829-起)
- [7. Mock/Live 切换机制](#7-mocklive-切换机制)
- [8. 和风天气 API 端点汇总](#8-和风天气-api-端点汇总)
- [9. 高德地图 API 端点汇总](#9-高德地图-api-端点汇总)
- [10. 12306 API 端点汇总](#10-12306-api-端点汇总)

---

## 1. 架构总览

```
┌─────────────────────────────────────────────────────────┐
│                    ToolRegistry（注册表）                 │
│         register() / get() / call() / names()           │
├─────────────────────────────────────────────────────────┤
│  map    weather   weather_warning   air_quality         │
│  weather_forecast   scenic   traffic   food   booking   │
│  hotel   train_ticket/train_transfer/train_route/       │
│  train_price   web_fetch   web_search                   │
├──────────────┬──────────────────────────────────────────┤
│   Mock 版     │              Live 版                      │
│ (MockWorld)   │ (QWeatherClient / AmapClient / RollingGo  │
│               │  / TrainClient / 真实 API)                 │
└──────────────┴──────────────────────────────────────────┘
```

### 核心设计原则

| 原则 | 说明 |
|------|------|
| **统一契约** | 所有工具继承 `BaseTool`，通过 `execute(**kwargs) → ToolResult` 统一调用 |
| **Mock/Live 双版本** | 天气(4)、地图(1)、交通(1)、景点(1)、餐饮(1)、酒店(1)、火车票(4)、网页(2) 工具有 Mock 和 Live 两个实现类，签名一致，调用方零改动（booking 仅有 Mock） |
| **零依赖（核心）** | 工具层 Live 版仅用 Python 标准库 `urllib.request` + `gzip` + `json`，不依赖 `requests`；例外：web_fetch/web_search 的 Live 版依赖 BeautifulSoup（requirements.txt 已声明），hotel Live 走 `mcp` SDK |
| **共享客户端** | 4 个 Live 天气工具共用 `QWeatherClient`；5 个 Live 地图工具用 `AmapClient`；4 个 Live 火车工具用 `TrainClient`；hotel Live 用 `RollingGoClient`，各自缓存 |

### 文件结构

```
tools/
├── __init__.py            # build_registry() 工厂 + default_registry
├── base_tool.py           # BaseTool 抽象基类 + ToolRegistry 注册表
├── tool_provider.py       # ToolProvider 门面：白名单（readonly）/ list_tools_json
├── qweather_client.py    # QWeatherClient 共享 API 客户端（天气）
├── amap_client.py         # AmapClient 共享 API 客户端（地图，v5 POI 搜索）
├── rollinggo_client.py    # RollingGoClient 共享 MCP 客户端（酒店，Streamable HTTP）
├── weather_tool.py        # 4 个天气工具 × 2 版本 = 8 个类
├── map_tool.py            # 地图工具 × 2 版本 = 2 个类
├── scenic_tool.py         # 景点工具 × 2 版本 = 2 个类
├── traffic_tool.py        # 交通工具 × 2 版本 = 2 个类
├── food_tool.py           # 餐饮工具 × 2 版本 = 2 个类
├── hotel_tool.py          # 酒店工具 × 2 版本 = 2 个类（详见 docs/hotel_tool.md）
├── booking_tool.py        # 预约工具
├── mock_data.py           # MockWorld + 模拟数据
├── web_client.py          # WebClient：网页抓取 + Bing 搜索（web 两工具共用）
├── web_fetch_tool.py      # 网页抓取工具 × 2 版本 = 2 个类
├── web_search_tool.py     # 网页搜索工具 × 2 版本 = 2 个类
└── train/                 # 火车票查询组（12306 直连，无需 API Key）
    ├── __init__.py        # 工具组导出
    ├── client.py          # TrainClient 共享客户端 + 响应解析 + 日期校验
    ├── stations.py        # 内置车站表（站名/拼音/电报码互转）
    ├── tools.py           # 4 个工具 × 2 版本 = 8 个类
    └── data/station_name.js  # 12306 官方车站数据（3300+ 站，随包内置）
```

---

## 2. 统一调用契约

### BaseTool 抽象基类

```python
class BaseTool(abc.ABC):
    name: str           # 工具唯一名（注册键）
    description: str    # 工具说明
    source: str         # "mock" 或 "live"
    input_schema: dict  # JSON Schema 风格入参说明

    def execute(self, **kwargs) -> ToolResult:
        """统一入口：计时 + 异常捕获 + 包装为 ToolResult"""

    @abc.abstractmethod
    def _run(self, **kwargs) -> Any:
        """子类实现真实逻辑"""
```

### ToolResult 统一返回结构

```python
@dataclass
class ToolResult:
    tool: str                    # 工具名
    status: ToolStatus           # OK / ERROR / NO_DATA
    data: Any = None             # 工具返回的数据 dict/list
    error: Optional[str] = None  # 异常信息（status=ERROR 时）
    source: str = "mock"         # "mock" 或 "live"
    elapsed_ms: float = 0.0      # 执行耗时（毫秒）
    timestamp: str = ...         # ISO 时间戳
```

### 调用方式

```python
from tools import default_registry

# 方式 1：通过注册表调用（推荐）
result = default_registry.call("weather", city="北京")
print(result.status)   # ToolStatus.OK
print(result.data)     # {"city": "北京", "temperature_c": 31.0, ...}
print(result.source)   # "mock" 或 "live"

# 方式 2：直接实例化调用
from tools.weather_tool import WeatherToolLive
tool = WeatherToolLive(client, world)  # world 用于 Demo 突发事件 override
result = tool.execute(city="北京")
```

### ToolStatus 枚举

| 值 | 含义 |
|----|------|
| `OK` | 调用成功，`data` 中有有效数据 |
| `ERROR` | 调用失败，`error` 中有错误信息 |
| `NO_DATA` | **预留位，当前代码不会产出**——查询成功但无数据时返回 `OK` + 空列表/空结构（启用需先适配 A 侧 `_tool_payload` 的降级语义，见 `docs/code_defects_and_fixes_20260828.md` C7） |

---

## 3. QWeatherClient 共享客户端

> 文件：`tools/qweather_client.py`

所有 Live 版天气工具共用同一个 `QWeatherClient` 实例，负责：

1. **API KEY 认证**：每次请求自动添加 `X-QW-Api-Key` 请求头
2. **Location ID 缓存**：城市名 → Location ID 映射，避免重复调 GeoAPI
3. **统一 HTTP GET**：封装 `urllib.request`，支持 gzip 响应解压
4. **零依赖**：仅用标准库 `urllib.request` + `gzip` + `json`

### 构造方法

```python
client = QWeatherClient(
    api_key="your_api_key",          # 和风天气 API KEY
    api_host="abc1234xyz.def.qweatherapi.com",  # 不带 https://
    timeout=10.0,                     # 请求超时（秒）
)
```

### 核心方法

| 方法 | 说明 |
|------|------|
| `get_location_id(city: str) → str` | 调 GeoAPI 城市搜索获取 Location ID，带缓存 |
| `get_location_coord(city: str) → tuple` | 获取城市经纬度 `(lat, lon)`，带缓存（v1 API 需要） |
| `get(path_or_url: str) → dict` | 发送 GET 请求，返回解析后的 JSON dict |
| `api_host` (property) | 获取 API Host |
| `api_key` (property) | 获取 API Key |

### 认证方式

```
GET https://{api_host}/v7/weather/now?location=101010100
Headers:
  X-QW-Api-Key: {your_api_key}
  Accept-Encoding: gzip
```

### Location ID 与坐标缓存机制

```
首次调用 get_location_id("北京")
  → 调 GeoAPI /geo/v2/city/lookup?location=北京
  → 获取 Location ID "101010100" 和坐标 (39.92, 116.41)
  → 同时存入 _location_cache 和 _coord_cache

后续调用 get_location_id("北京")
  → 直接从 _location_cache 返回 "101010100"

调用 get_location_coord("北京")
  → 直接从 _coord_cache 返回 (39.92, 116.41)
  → 若缓存未命中，先调 get_location_id 填充缓存
```

> **注意**：天气预警和空气质量已迁移至 v1 API，需要经纬度路径参数，
> 因此 `get_location_coord()` 在首次调用时会触发 GeoAPI 查询并缓存坐标。

---

## 4. 天气类工具（4 个 × Mock/Live 双版本）

### 4.1 weather — 实况天气

| 属性 | 值 |
|------|-----|
| **工具名** | `weather` |
| **说明** | 查询城市实况天气：天气状况、气温、体感温度、降雨概率、紫外线、风力、湿度、能见度 |
| **Mock 类** | `WeatherTool` |
| **Live 类** | `WeatherToolLive` |
| **文件** | `tools/weather_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `city` | string | ✅ | 城市名 |
| `date` | string | ❌ | 日期 YYYY-MM-DD，缺省为当天 |

#### 返回字段

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `city` | str | 城市名 | 入参 | 入参 |
| `date` | str | 日期 | MockWorld | 当天 |
| `condition` | str | 天气状况（晴/多云/暴雨…） | MockWorld | `now.text` 或 `now.icon` → `_QWEATHER_ICON_TEXT` 映射 |
| `temperature_c` | float | 气温（°C） | MockWorld | `now.temp` |
| `feels_like` | float | 体感温度（°C） | = temperature_c | `now.feelsLike` |
| `rain_probability` | int | 降雨概率（%） | MockWorld | `now.precip > 0` → 80，否则 10 |
| `uv_index` | int | 紫外线指数 | MockWorld | `/v7/indices?type=5` → `daily[0].category` |
| `wind_kmh` | int | 风速（km/h） | MockWorld | `now.windScale` → `_WIND_SCALE_KMH` 蒲福风级换算 |
| `humidity` | int | 湿度（%） | 固定 50 | `now.humidity` |
| `visibility_km` | float | 能见度（km） | 固定 10 | `now.vis` |

#### Live 版构造函数

```python
WeatherToolLive(client, world=None)
  # client: QWeatherClient 实例（共享 API KEY + Host + Location ID 缓存）
  # world: 可选 MockWorld，用于 Demo 突发事件 override（如 set_weather(rain_probability=85)）
```

#### Live 版调用链路

```
WeatherToolLive._run(city="北京")
  │
  ├─ client.get_location_id("北京")          # GeoAPI（带缓存）
  │    GET /geo/v2/city/lookup?location=北京
  │    → Location ID "101010100"
  │
  ├─ self._fetch_now("101010100")            # 实况天气
  │    GET /v7/weather/now?location=101010100
  │    → now.temp / now.text / now.icon / now.windScale / now.humidity / now.precip / now.feelsLike / now.vis
  │
  ├─ self._fetch_uv_index("101010100")       # UV 指数（可选，失败默认 0）
  │    GET /v7/indices?location=101010100&type=5
  │    → daily[0].category
  │
  └─ MockWorld override 叠加（如有）
       world.set_weather(condition="暴雨", rain_probability=85)
       → result.update(world.weather_overrides)
       → API 数据被 override 字段覆盖
```

> **Demo 突发事件 override**：`WeatherToolLive` 接收可选的 `MockWorld` 参数，
> `set_weather()` 设置的 override 字段会叠加到 API 返回数据上，用于注入模拟暴雨等突发事件。

#### 蒲福风级 → km/h 换算表

| 风力等级 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 | 10 | 11 | 12 |
|----------|---|---|---|---|---|---|---|---|---|---|----|----|-----|
| 风速 km/h | 0 | 3 | 10 | 17 | 25 | 33 | 42 | 51 | 61 | 71 | 85 | 100 | 120 |

---

### 4.2 weather_warning — 天气预警

| 属性 | 值 |
|------|-----|
| **工具名** | `weather_warning` |
| **说明** | 查询城市当前天气预警：暴雨、台风、雷电、大风等极端天气预警信息 |
| **Mock 类** | `WeatherWarningTool` |
| **Live 类** | `WeatherWarningToolLive` |
| **文件** | `tools/weather_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `city` | string | ✅ | 城市名 |

#### 返回字段

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `city` | str | 城市名 | 入参 | 入参 |
| `warnings` | list[dict] | 预警列表 | 固定 `[]` | `warning` 数组 |
| `has_warning` | bool | 是否有预警 | 固定 `False` | `len(warnings) > 0` |

#### warnings 列表中每个 dict 的字段（Live 版）

| 字段 | 类型 | 说明 | API 来源（v1） |
|------|------|------|----------|
| `title` | str | 预警标题 | `alerts[].headline` |
| `type` | str | 预警类型（暴雨/台风…） | `alerts[].eventType.name` |
| `level` | str | 预警等级（blue/yellow/orange/red） | `alerts[].color.code` |
| `text` | str | 预警详细内容 | `alerts[].description` |

#### Live 版调用链路

```
WeatherWarningToolLive._run(city="北京")
  │
  ├─ client.get_location_coord("北京")        # 获取经纬度（带缓存）
  │    → (39.92, 116.41)
  │
  └─ client.get("/weatheralert/v1/current/39.92/116.41")
       → resp["alerts"] 数组
       → 映射: headline→title, eventType.name→type, color.code→level, description→text
```

> **v1 迁移说明**：旧端点 `/v7/warning/now?location={id}` 已废弃（返回 403），
> 新端点 `/weatheralert/v1/current/{lat}/{lon}` 使用经纬度路径参数，响应结构完全不同。

---

### 4.3 air_quality — 空气质量

| 属性 | 值 |
|------|-----|
| **工具名** | `air_quality` |
| **说明** | 查询城市空气质量：AQI 指数、PM2.5、PM10、主要污染物 |
| **Mock 类** | `AirQualityTool` |
| **Live 类** | `AirQualityToolLive` |
| **文件** | `tools/weather_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `city` | string | ✅ | 城市名 |

#### 返回字段

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `city` | str | 城市名 | 入参 | 入参 |
| `aqi` | int | 空气质量指数 | 固定 35 | `indexes[code=us-epa].aqi` |
| `category` | str | 空气质量类别（优/良/轻度污染…） | 固定 "优" | `indexes[code=us-epa].category` |
| `pm25` | float | PM2.5 浓度（μg/m³） | 固定 15.0 | `pollutants[code=pm2p5].concentration.value` |
| `pm10` | float | PM10 浓度（μg/m³） | 固定 30.0 | `pollutants[code=pm10].concentration.value` |
| `no2` | float | 二氧化氮（μg/m³） | 固定 20.0 | `pollutants[code=no2].concentration.value` |
| `so2` | float | 二氧化硫（μg/m³） | 固定 5.0 | `pollutants[code=so2].concentration.value` |
| `co` | float | 一氧化碳（mg/m³） | 固定 0.5 | `pollutants[code=co].concentration.value` |
| `o3` | float | 臭氧（μg/m³） | 固定 60.0 | `pollutants[code=o3].concentration.value` |

#### Live 版调用链路

```
AirQualityToolLive._run(city="北京")
  │
  ├─ client.get_location_coord("北京")        # 获取经纬度（带缓存）
  │    → (39.92, 116.41)
  │
  └─ client.get("/airquality/v1/current/39.92/116.41")
       → resp["indexes"] 数组 → 取 code=us-epa 的 aqi/category
       → resp["pollutants"] 数组 → 按 code 匹配 pm2p5/pm10/no2/so2/co/o3
```

> **v1 迁移说明**：旧端点 `/v7/air/now?location={id}` 已废弃（返回 403），
> 新端点 `/airquality/v1/current/{lat}/{lon}` 使用经纬度路径参数，
> 响应结构从单个 `now` 对象改为 `indexes` + `pollutants` 两个数组。

---

### 4.4 weather_forecast — 逐小时预报

| 属性 | 值 |
|------|-----|
| **工具名** | `weather_forecast` |
| **说明** | 查询城市未来 24 小时逐小时天气预报：气温、天气状况、降雨概率变化趋势 |
| **Mock 类** | `WeatherForecastTool` |
| **Live 类** | `WeatherForecastToolLive` |
| **文件** | `tools/weather_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `city` | string | ✅ | 城市名 |
| `hours` | int | ❌ | 返回小时数（默认 24） |

#### 返回字段

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `city` | str | 城市名 | 入参 | 入参 |
| `hours` | list[dict] | 逐小时预报列表 | MockWorld 生成 | `hourly` 数组 |
| `summary` | str | 降雨摘要 | "未来N小时{condition}" | 有降雨小时数 > 0 → "有N小时可能降雨"，否则 "无降雨" |

#### hours 列表中每个 dict 的字段

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `time` | str | 时间（HH:MM） | 当前时间 + i 小时 | `hourly[].fxTime` 取末 5 字符 |
| `temp` | float | 气温（°C） | MockWorld | `hourly[].temp` |
| `condition` | str | 天气状况 | MockWorld | `hourly[].iconCode` → `_QWEATHER_ICON_TEXT` 映射 |
| `rain_probability` | int | 降雨概率（%） | MockWorld | `hourly[].precip > 0` → 80，否则 10 |

#### Live 版调用链路

```
WeatherForecastToolLive._run(city="北京", hours=24)
  │
  ├─ client.get_location_id("北京")
  │
  └─ client.get("/v7/weather/24h?location=101010100")
       → resp["hourly"] 数组
       → 遍历前 hours 个，映射字段
       → 统计 rain_probability > 50 的小时数，生成 summary
```

---

## 5. 地图工具（Mock/Live 双版本）

### 5.1 AmapClient 共享客户端

> 文件：`tools/amap_client.py`

Live 版地图工具使用 `AmapClient` 实例，负责：

1. **API Key 认证**：每次请求自动在 URL query string 中附加 `key` 参数
2. **地理编码缓存**：地址 → 坐标映射，避免重复调地理编码 API
3. **统一 HTTP GET**：封装 `urllib.request`，支持 gzip 响应解压
4. **零依赖**：仅用标准库 `urllib.request` + `gzip` + `json`

#### 构造方法

```python
client = AmapClient(
    api_key="your_amap_key",    # 高德地图 API Key
    timeout=10.0,                # 请求超时（秒）
)
```

#### 核心方法

| 方法 | 说明 |
|------|------|
| `geocode(address, city="") → (lat, lng)` | 调 `/v3/geocode/geo`，地址→坐标，带缓存 |
| `search_poi(query, city="", limit=10) → list[dict]` | 调 `/v5/place/text`（v5 API），关键词搜索 POI |
| `search_poi_around(location, radius=1000, ...) → list[dict]` | 调 `/v5/place/around`（v5 API），周边搜索 POI |
| `get_route(origin, destination, mode="transit") → dict` | 调路线规划 API，返回 `{distance, duration}` |
| `api_key` (property) | 获取 API Key |

#### 认证方式

```
GET https://restapi.amap.com/v5/place/text?keywords=故宫&key={your_api_key}&show_fields=business,opentime_today,opentime_week,rating,cost,tag,alias
```

#### v5 POI 深度信息结构

v5 API 通过 `show_fields` 参数返回深度信息，嵌套在 `business` 对象内（非扁平顶层字段）：

```json
{
  "name": "故宫博物院",
  "location": "116.397029,39.917839",
  "address": "景山前街4号",
  "type": "风景名胜;风景名胜;世界遗产",
  "business": {
    "rating": "4.9",
    "cost": "",
    "tag": "",
    "tel": "4009501925",
    "opentime_today": "",
    "opentime_week": "07/27 08:30-17:00开放 最晚进入16:00...",
    "alias": "紫禁城"
  }
}
```

`_normalize_poi()` 从 `business` 对象提取深度字段，同时保留 v3 `biz_ext` fallback 兼容。

#### 地理编码缓存机制

```
首次调用 geocode("故宫")
  → 调 /v3/geocode/geo?address=故宫
  → 获取坐标 (39.916, 116.397)
  → 存入 _geocode_cache["故宫|"] = (39.916, 116.397)

后续调用 geocode("故宫")
  → 直接从缓存返回，不再调 API
```

#### 路线模式 → API 端点映射

| mode | 端点 | 返回结构 |
|------|------|----------|
| `transit` | `/v3/direction/transit/integrated` | `route.transits[0].distance/duration` |
| `driving` | `/v3/direction/driving` | `route.paths[0].distance/duration` |
| `riding` | `/v4/direction/bicycling` | `paths[0].distance/duration` |
| `walk` | `/v3/direction/walking` | `route.paths[0].distance/duration` |

---

### 5.2 map — 地图服务

| 属性 | 值 |
|------|-----|
| **工具名** | `map` |
| **说明** | 地图服务：搜索景点位置、计算两点间路线距离与预计耗时 |
| **Mock 类** | `MapTool` |
| **Live 类** | `MapToolLive` |
| **文件** | `tools/map_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | enum | ✅ | `search_poi`（搜索地点）/ `route`（计算路线）/ `batch_route`（批量矩阵，内部管道） |
| `query` | string | ❌ | 搜索关键词（action=search_poi 时） |
| `origin` / `destination` | string | ❌ | 起点/终点（action=route 时） |
| `origins` / `destinations` | array<string> | ❌ | 起点/终点数组（action=batch_route 时，A 侧交通矩阵用） |
| `mode` | enum | ❌ | `transit`/`driving`/`riding`/`walk`（默认 transit）；`train`/`air` 为城际估算（仅 route，查 `fake_spots/city_travel.json`，缺失回退 driving） |
| `city` | string | ❌ | 地理编码限定城市（默认北京） |

#### 返回数据

**action=search_poi** → `list[dict]`：

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `name` | str | 地点名称 | PLACES 字典 | `pois[].name` |
| `lat` | float | 纬度 | PLACES 字典 | `pois[].location` 分割 |
| `lng` | float | 经度 | PLACES 字典 | `pois[].location` 分割 |
| `open` | str | 营业时间 | PLACES 字典 | 固定 `""`（高德无此字段） |
| `price` | float | 票价 | PLACES 字典 | 固定 `0.0`（高德无此字段） |
| `address` | str | 地址 | — | `pois[].address`（仅 Live） |

**action=route** → `dict`：

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `from` | str | 起点 | 入参 | 入参 |
| `to` | str | 终点 | 入参 | 入参 |
| `mode` | str | 模式（含 train/air 城际） | 入参 | 入参 |
| `distance_km` | float | 距离（km） | 固定 3.5 | `route.distance / 1000` |
| `duration_min` / `transport_minutes` | int | 预计耗时（分钟，两键同值，`transport_minutes` 为 A 侧规范字段） | 固定 25 | `route.duration / 60` |
| `transit` | str | 交通方式描述 | 固定描述 | mode → 中文（公交/驾车/骑行/步行） |
| `fare` | float | 费用（元） | 固定 4.0 | 固定 `0.0`（高德无此字段） |
| `source` | str | 数据来源 | `mock` | `amap`（城际估算时 `estimate`） |
| 城际附加 | — | `from_station`/`to_station`/`cost_per_person`/`transit_text`/`legs` | 估算表 | — |

**action=batch_route** → `list[dict]`（每对 origin×destination 一行）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `origin` / `destination` | str | 该行起终点 |
| `distance_km` | float | 距离（km） |
| `transport_minutes` | int | 预计耗时（分钟） |
| `mode` | str | 英文模式名（batch 仅支持 driving/walk，transit 默认解析为 driving 近似；**C4 修复后 Mock/Live 同构**） |
| `transit_text` | str | 交通方式中文描述（C4 新增，Mock 原先误填在 mode 里） |
| `fare` | float | 费用（元） |

#### Live 版调用链路

```
MapToolLive._run(action="route", origin="故宫", destination="天坛", mode="transit")
  │
  ├─ client.geocode("故宫")               # 地理编码（带缓存）
  │    GET /v3/geocode/geo?address=故宫
  │    → (39.916, 116.397)
  │
  ├─ client.geocode("天坛")               # 地理编码（带缓存）
  │    GET /v3/geocode/geo?address=天坛
  │    → (39.882, 116.407)
  │
  └─ client.get_route(origin, dest, "transit")   # 路线规划
       GET /v3/direction/transit/integrated?origin=116.397,39.916&destination=116.407,39.882&city=北京
       → {distance: 3500, duration: 1500}
       → distance_km=3.5, duration_min=25
```

---

## 6. 其他领域工具

### 6.1 scenic — 景点状态

| 属性 | 值 |
|------|-----|
| **工具名** | `scenic` |
| **说明** | 景点实时状态（是否开放/排队/预约/票价），或按城市搜索景点候选池（A 侧规划真源用） |
| **Mock 类** | `ScenicTool` |
| **Live 类** | `ScenicToolLive` |
| **文件** | `tools/scenic_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `place` | string | ✅ | action=status 时为景点名；action=search 时为城市名 |
| `action` | enum | ❌ | `status`（默认，单点状态）/ `search`（城市候选池，内部管道，A 侧 LiveSpotsSource 消费） |
| `limit` | int | ❌ | search 返回数量上限（默认 10） |
| `city` | string | ❌ | 地理编码限定城市（Live status 用；C6 补入 schema） |

#### 返回字段

- `action=status` → `dict`：见下表
- `action=search` → `list[dict]` 候选池：`id`/`name`/`alias`/`location{"lat","lng"}`/
  `opening_time`/`closing_time`/`suggest_duration`/`price`/`tags`（Live 另含
  `address`/`open_hours_week`）

#### 返回字段（status）

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `place` | str | 景点名称 | 入参 | 入参 |
| `open` | bool | 是否开放 | 固定 `True` | 固定 `True`（高德无法判断） |
| `queue_min` | int | 预计排队时间（分钟） | MockWorld | MockWorld（无公开 API） |
| `ticket_required` | bool | 是否需要预约 | MockWorld | MockWorld（无公开 API） |
| `open_hours` | str | 营业时间 | MockWorld | v5 API `business.opentime_today`，空时 fallback MockWorld |
| `price` | float | 票价 | MockWorld | MockWorld（高德无此字段） |
| `rating` | float | 景点评分 | 固定 `0` | v5 API `business.rating` |
| `address` | str | 地址 | 固定 `""` | v5 API `address` |
| `tel` | str | 联系电话 | 固定 `""` | v5 API `business.tel` |
| `open_hours_week` | str | 周营业时间 | 固定 `""` | v5 API `business.opentime_week` |

#### Live 版调用链路

```
ScenicToolLive._run(place="故宫")
  │
  ├─ client.search_poi("故宫", city="北京", limit=1)    # v5 POI 搜索
  │    GET /v5/place/text?keywords=故宫&region=北京&city_limit=true&show_fields=business,opentime_today,opentime_week,rating,cost,tag,alias
  │    → pois[0].business.opentime_today / opentime_week / rating / tel / address
  │
  ├─ MockWorld.get_place("故宫")                      # 排队/票价 fallback
  │    → queue_min / ticket_required / price
  │
  └─ open_hours = poi.opentime_today or MockWorld.open
       → 优先用 v5 API 营业时间，空时 fallback MockWorld
```

> **v5 升级说明**：v3 API 无营业时间字段，`open_hours` 完全依赖 MockWorld。
> v5 API 通过 `show_fields=business,opentime_today,...` 返回 `business.opentime_today`，
> 景点类 POI 通常只有 `opentime_week`（含详细日期段描述），餐饮类才有 `opentime_today`。

---

### 6.2 traffic — 交通状态

| 属性 | 值 |
|------|-----|
| **工具名** | `traffic` |
| **说明** | 交通状态：公交/地铁/打车预计耗时与拥堵程度 |
| **Mock 类** | `TrafficTool` |
| **Live 类** | `TrafficToolLive` |
| **文件** | `tools/traffic_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `origin` | string | ✅ | 起点 |
| `destination` | string | ✅ | 终点 |
| `mode` | enum | ❌ | `transit` / `taxi` / `walk`（默认 transit） |

#### 返回字段

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `origin` | str | 起点 | 入参 | 入参 |
| `destination` | str | 终点 | 入参 | 入参 |
| `mode` | str | 交通方式 | 入参 | 入参 |
| `duration_min` | int | 预计耗时（分钟） | 固定 30 | `route.duration / 60`（秒→分钟） |
| `congestion` | str | 拥堵程度 | 固定 "畅通" | 驾车模式按速度推断，其他固定 "畅通" |
| `delay_min` | int | 延迟时间（分钟） | 固定 0 | 驾车模式：实际耗时 - 40km/h 基准耗时 |
| `note` | str | 备注 | 固定 "地铁1号线运行正常" | `距离 {x}km`，拥堵时追加延误信息 |

#### Live 版拥堵推断逻辑

仅 `taxi`（驾车）模式根据平均速度推断拥堵：

| 平均速度 | 拥堵程度 | 说明 |
|----------|----------|------|
| < 15 km/h | 拥堵 | 严重拥堵 |
| < 30 km/h | 缓行 | 轻度拥堵 |
| ≥ 30 km/h | 畅通 | 路况良好 |

> `transit`（公交）和 `walk`（步行）模式不推断拥堵，固定返回 "畅通"。

#### Live 版构造函数

```python
TrafficToolLive(client, world=None)
  # client: AmapClient 实例（共享 API Key + 地理编码缓存）
  # world: 可选 MockWorld，用于 Demo 突发事件 override（如 set_traffic_delay()）
```

#### Live 版调用链路

```
TrafficToolLive._run(origin="故宫", destination="天坛", mode="taxi")
  │
  ├─ client.geocode("故宫", city="北京")          # 地理编码（带缓存）
  │    → (39.916, 116.397)
  │
  ├─ client.geocode("天坛", city="北京")          # 地理编码（带缓存）
  │    → (39.882, 116.407)
  │
  ├─ client.get_route(origin_coord, dest_coord, mode="driving")
  │    GET /v3/direction/driving?origin=116.397,39.916&destination=116.407,39.882
  │    → {distance: 5500, duration: 1260}
  │    → duration_min=21, distance_km=5.5
  │    → speed=5.5/(21/60)=15.7km/h → 缓行
  │    → free_flow_min=round(5.5/40*60)=8, delay_min=21-8=13
  │
  └─ MockWorld override 叠加（如有）
       world.set_traffic_delay("北京", "故宫", delay_min=45, congestion="拥堵")
       → 覆盖 API 计算的 delay_min 和 congestion
```

> **Demo 突发事件 override**：`TrafficToolLive` 接收可选的 `MockWorld` 参数，
> `set_traffic_delay()` 设置的 override 会覆盖 API 返回的延误数据，用于注入模拟交通拥堵事件。

#### mode 映射表

| TrafficTool mode | AmapClient mode | 高德端点 |
|------------------|----------------|----------|
| `transit` | `transit` | `/v3/direction/transit/integrated` |
| `taxi` | `driving` | `/v3/direction/driving` |
| `walk` | `walk` | `/v3/direction/walking` |

---

### 6.3 food — 餐饮推荐

| 属性 | 值 |
|------|-----|
| **工具名** | `food` |
| **说明** | 餐厅推荐：评分、人均价格、营业状态、距离 |
| **Mock 类** | `FoodTool` |
| **Live 类** | `FoodToolLive` |
| **文件** | `tools/food_tool.py` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | ❌ | 关键词，如菜系 |
| `near` | string | ❌ | 附近地点（有 near → 周边 1km 搜索；无 → 城市内搜索） |
| `city` | string | ❌ | 搜索城市（无 near 时生效，默认北京） |
| `limit` | int | ❌ | 返回数量上限 |

#### 返回数据 → `list[dict]`

| 字段 | 类型 | 说明 | Mock 来源 | Live API 来源 |
|------|------|------|-----------|---------------|
| `name` | str | 餐厅名称 | 固定列表 | `pois[].name` |
| `location` | str | 坐标 `"lng,lat"`（**A 侧必需**：无坐标餐厅不参与通勤；C2 修复后 Mock 也有） | 固定坐标 | `pois[].location` 或顶层 lat/lng |
| `rating` | float | 评分 | 固定值 | `business.rating` |
| `price_per_person` | float | 人均价格 | 固定值 | `business.cost` |
| `open` | bool | 是否营业 | 固定 `True` | 固定 `True`（无公开 API） |
| `distance_km` | float | 距离 | 固定值 | `pois[].distance / 1000`（周边搜索） |
| `cuisine` | str | 菜系 | 固定值 | POI `type` 字段第二段（如"餐饮服务;中餐厅"→"中餐厅"） |
| `queue_min` | int | 排队时间 | 固定值 | 固定 `0`（无公开 API） |
| `open_hours` | str | 今日营业时间 | 固定值 | v5 API `business.opentime_today` |
| `specialty` | str | 特色菜 | 固定值 | v5 API `business.tag`（如 "烤鸭,京菜"） |
| `address` | str | 餐厅地址 | 固定值 | v5 API `address` |
| `tel` | str | 联系电话 | 固定值 | v5 API `business.tel` |

#### Live 版调用链路

```
FoodToolLive._run(query="餐厅", near="故宫")
  │
  ├─ client.geocode("故宫")                           # 地理编码（带缓存）
  │    → (39.916, 116.397)
  │
  └─ client.search_poi_around(coord, types="050000", radius=1000, keywords="餐厅")
       GET /v5/place/around?location=116.397,39.916&radius=1000&types=050000&keywords=餐厅&show_fields=business,opentime_today,opentime_week,rating,cost,tag,alias
       → pois[].business.rating / cost / tag / opentime_today
       → pois[].distance（米）
       → pois[].type（推断菜系）
```

> **无 near 参数时**：直接调 `search_poi(query or "餐厅", city="北京")`，无距离字段，`distance_km=0.0`。

---

### 6.4 booking — 预约服务

| 属性 | 值 |
|------|-----|
| **工具名** | `booking` |
| **说明** | 预约服务：为景点/酒店准备预约（填写信息），不涉及付款 |
| **类** | `BookingTool` |
| **文件** | `tools/booking_tool.py` |
| **source** | `mock` |

#### 输入参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `action` | enum | ✅ | `prepare`（准备预约）、`submit`（模拟提交）、`status`（查询状态） |
| `place` | string | ❌ | 景点/酒店名（action=prepare 时） |
| `booking_id` | string | ❌ | 预约 ID（action=submit/status 时必填） |
| `target_date` | string | ❌ | 目标日期 YYYY-MM-DD |
| `party_size` | int | ❌ | 人数（默认 1） |
| `booking_type` | enum | ❌ | 预约类型：`scenic`/`hotel`/`transport`（默认 `scenic`） |
| `price` | number | ❌ | 票价/房费（由 BookingManager 自动填充） |
| `tel` | string | ❌ | 联系电话（由 BookingManager 自动填充） |
| `ticket_required` | bool | ❌ | 是否需要预约（默认 True） |
| `address` | string | ❌ | 地址（由 BookingManager 自动填充） |
| `open_hours` | string | ❌ | 营业时间（由 BookingManager 自动填充） |

#### 返回字段

| 字段 | 类型 | 说明 |
|------|------|------|
| `booking_id` | str | 预约 ID（8 位大写 hex） |
| `place` | str | 景点/酒店名 |
| `target_date` | str | 目标日期 |
| `party_size` | int | 人数 |
| `booking_type` | str | 预约类型（`scenic`/`hotel`/`transport`） |
| `price` | float | 票价/房费 |
| `tel` | str | 联系电话 |
| `ticket_required` | bool | 是否需要预约 |
| `address` | str | 地址 |
| `open_hours` | str | 营业时间 |
| `status` | str | 预约状态（`draft` → `submitted`） |
| `payment_required` | bool | 是否需要付款（始终 True，付款需人工） |
| `confirm_code` | str | 确认码（submit 后生成，格式 `CONF-{booking_id}`） |
| `note` | str | 备注 |

#### 调用链路

```
BookingManager.prepare()
  ├─ registry.call("scenic", place=...)     ← 自动填充景点信息
  │   返回 price/tel/ticket_required/address/open_hours
  └─ registry.call("booking", action="prepare", ...)  ← 生成 draft

BookingManager.confirm()
  └─ registry.call("booking", action="submit", ...)    ← 模拟提交，生成 confirm_code
```

### 6.5 火车票查询组（Mock/Live 双版本）

12306 官方接口直连的四个只读查询工具，集中在 `tools/train/` 子包中（参考
`Reference Code/mcp-server-12306` 的实现方式移植到标准库 urllib）。

| 属性 | 值 |
|------|-----|
| **工具名** | `train_ticket` / `train_transfer` / `train_route` / `train_price` |
| **说明** | 余票时刻坐席 / 中转换乘方案 / 经停站 / 票价 |
| **Mock 类** | `TrainTicketTool` / `TrainTransferTool` / `TrainRouteTool` / `TrainPriceTool` |
| **Live 类** | 对应 `*Live` 版本，共享同一个 `TrainClient` 实例 |
| **文件** | `tools/train/tools.py`（工具）、`tools/train/client.py`（客户端）、`tools/train/stations.py`（车站表） |
| **开关** | `settings.use_real_train_api`（无需 API Key，非 Demo 即 Live） |

#### TrainClient 共享客户端

- **认证方式**：无 Key。首次请求先 `GET /otn/leftTicket/init` 获取会话
  Cookie（JSESSIONID/route），后续请求原样回传；实例内复用，重试时自动重新 init
- **车站参数**：入参支持中文站名（"北京南"、"北京南站"）、全拼（beijingnan）、
  三字码（VNP），由 `stations.resolve_station()` 转换为 12306 电报码
- **日期**：`YYYY-MM-DD`，限定今天 ~ 今天+14 天（12306 预售期），违规抛业务错误
- **反爬处理**：非 200 或最终 URL 命中错误页特征（error.html / /ntce/）→ 业务错误；
  网络错误 → `ConnectionError` 交由 BaseTool 指数退避重试

#### train_ticket — 余票查询

输入参数：

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `from_station` | string | ✅ | 出发车站（中文名/全拼/电报码） |
| `to_station` | string | ✅ | 到达车站 |
| `date` | string | ✅ | 出发日期 YYYY-MM-DD（14 天内） |
| `purpose_codes` | string | ❌ | `ADULT` 成人（默认）/ `0X` 学生 |
| `limit` | int | ❌ | 返回车次数量上限，默认 20 |

返回数据 → `list[dict]`：

| 字段 | 类型 | 说明 |
|------|------|------|
| `code` | str | 车次号，如 G39 |
| `train_no` | str | 12306 官方编号（如 `24000000G10L`），train_route 查询需要 |
| `status` | str | `预订` / `停运` 等标记 |
| `from_station` / `to_station` | str | 出发/到达站中文名 |
| `from_station_code` / `to_station_code` | str | 出发/到达站电报码 |
| `depart_time` / `arrive_time` | str | 出发/到达时刻 HH:mm |
| `duration` | str | 历时 HH:mm |
| `seats` | dict | 各坐席余票（键见下表），值可为数字 / `有` / `无` / `候补`；无此坐席的车次不含该键 |

`seats` 键名对照：`business` 商务座、`first_class` 一等座、`second_class` 二等座、
`premium_soft_sleeper` 高级软卧、`soft_sleeper` 软卧、`soft_seat` 软座、`dongwo` 动卧、
`hard_sleeper` 硬卧、`hard_seat` 硬座、`no_seat` 无座。

#### train_transfer — 中转换乘

输入：`from_station` / `to_station` / `date` 必填；`middle_station`（指定中转站）、
`purpose_codes`（`00` 普通 / `0X` 学生）、`max_results`（默认 10）可选。
客户端自动翻页抓全后截断。

返回 `list[dict]`：`middle_station` 中转站、`wait_time` 换乘等待、`total_duration` 总历时、
`segments` 两段行程（各含 `code`/`from_station`/`to_station`/`depart_time`/`arrive_time`/
`duration`/`seats`，seats 键名同上，另含 `first_class_sleeper` 一等卧、`premium_seat` 特等座）。

#### train_route — 经停站

输入：`train`（车次号 G39 或官方编号）、`from_station`、`to_station`、`date` 均必填。
车次号入参会先经余票接口转换为官方编号。

返回 `list[dict]`：`station_no` 站序、`station_name` 站名、`arrive_time` 到达时刻、
`depart_time` 出发时刻、`stopover_time` 停站时长（首末站对应时刻为 `----`）。

#### train_price — 票价

输入：`from_station` / `to_station` / `date` 必填；`train`（车次号过滤，可选，缺省返回全部）、
`purpose_codes` 可选。

返回 `list[dict]`：`code` / `train_no` / `from_station` / `to_station` / `depart_time` /
`arrive_time` / `duration` + `prices` dict（键同 seats，值为元；12306 原始单位 0.1 元已换算）。

#### Live 版调用链路

```
TrainTicketToolLive._run(from_station="北京南", to_station="上海虹桥", date="2026-09-01")
  │
  ├─ resolve_station("北京南") → "VNP"                # 站名 → 电报码（内置站表）
  │
  └─ client.query_tickets("VNP", "AOH", date)
       GET /otn/leftTicket/init                       # 首次：取会话 Cookie
       GET /otn/leftTicket/queryI?leftTicketDTO.train_date=...&leftTicketDTO.from_station=VNP&...
       → data.result[]（"|" 分隔字符串）→ parse_ticket_row() 按列索引解析
```

---

### 6.6 hotel — 酒店查询（Mock/Live 双版本）

> 详细文档：`docs/hotel_tool.md`（工具契约）、`docs/A_hotel_tool_adapter.md`
> （A 侧适配）、`docs/C_hotel_data.md`（C 端消费数据）。本节为速览。

| 属性 | 值 |
|------|-----|
| **工具名** | `hotel` |
| **说明** | 酒店信息查询：按城市/日期/人数/星级/价格搜索，或查单个酒店房型价格明细、搜索标签 |
| **Mock 类** | `HotelTool`（两家北京示例酒店） |
| **Live 类** | `HotelToolLive`（RollingGo MCP：`searchHotels` / `getHotelDetail` / `getHotelSearchTags`） |
| **文件** | `tools/hotel_tool.py`、`tools/rollinggo_client.py` |
| **开关** | `settings.use_real_hotel_api`（需 `ROLLINGGO_API_KEY`） |

#### 输入参数（action=search/detail/tags 三动作共用 schema）

| 参数 | 类型 | 说明 |
|------|------|------|
| `action` | enum | `search` 搜索（需 `place` 或 `city`）/ `detail` 房型（需 `hotelId` 或 `name`）/ `tags` 搜索标签 |
| `place` / `city` | string | 目标地（city 等价于按城市搜，兼容 A 侧调用） |
| `checkInDate` / `checkOutDate` | string | 入离日期 |
| `stayNights` / `adultCount` / `roomCount` / `childCount` | int | 住宿人数/间数 |
| `starRatings` / `maxPricePerNight` / `requiredTags` | array/number | 过滤条件（search） |
| `hotelId` / `name` | string/int | detail 定位（两者至少其一） |

#### 返回数据

- `search` → `{"hotels": [hotel...], "count": n}`（Live 另带 `raw`）；每条 hotel：
  `id/name/name_en/brand/location{lat,lng}/star/rating/price_per_night/address/
  tags/booking_url/image_url/open`
- `detail` → `{hotelId, name, starRating, checkIn, checkOut, bookingUrl, rooms[...],
  raw}`；rooms 元素 snake_case：`room_name/rate_plan_id/average_price/currency/
  meal_amount/meal_type/on_request/cancel_policy/cancelable/room_info{...}`
  （**C1 修复后 Mock 与 Live 同构**，详见 `docs/C_hotel_data.md` §一.2）
- `tags` → RollingGo 标签原样返回（Live，带 1h TTL 缓存）

#### 边界

hotel_tool **只读查询**，不负责预订/下单/支付（`docs/hotel_tool.md` §1）；
`booking_url` 仅为预订落地页透传。

---

### 6.7 web_fetch / web_search — 网页抓取与搜索（Mock/Live 双版本）

| 属性 | 值 |
|------|-----|
| **工具名** | `web_fetch` / `web_search` |
| **说明** | 抓取指定 URL 网页正文（支持 CSS 选择器）/ 关键词搜索网页列表 |
| **Mock 类** | `WebFetchTool` / `WebSearchTool`（故宫示例内容） |
| **Live 类** | 对应 `*Live` 版本，共享 `WebClient`（urllib 直抓 + Bing 搜索，BeautifulSoup 解析） |
| **文件** | `tools/web_fetch_tool.py`、`tools/web_search_tool.py`、`tools/web_client.py` |
| **开关** | `settings.use_real_web`（无需 API Key，非 Demo 即 Live） |

#### web_fetch

输入：`url`（必填）、`selector`（CSS 选择器，可选）、`max_length`（正文截断，默认 5000）。
返回：`{url, final_url, title, text, links[{text,url}], fetch_time}`。
链路：`WebClient.fetch(url)`（gzip + charset 检测）→ BeautifulSoup 提取正文/链接。

#### web_search

输入：`query`（必填）、`max_results`（默认 5）。
返回：`{query, results[{title,url,snippet}], count}`。
链路：`WebClient.search(query)` → **Bing 搜索**（`www.bing.com/search`）→
解析 `li.b_algo` 结果块。

---

### 6.8 技能（Skill）—— 面向意图的组合工具（0829 起）

技能 = `Skill(BaseTool)` 子类，构造注入原子工具/共享 client 并组合调用；
输出为意图级结构（`output_schema` 声明），自动进入 `ToolProvider.list_for_llm()`
白名单。设计详见 `docs/tool_encapsulation_design_20260828.md` §3。

| 工具名 | 组合 | 输出要点 |
|--------|------|----------|
| `weather_brief` | weather + forecast + air_quality + warning | `{city, current, forecast_hours, air_quality, warnings, summary}`；单段失败降级空段 |
| `train_trip` | train_ticket（选班次）+ train_price（二等座价） | 单一班次 `{code, from_station, to_station, depart_time, arrive_time, transport_minutes, cost_per_person, source}`——与 A 侧城际交通契约对齐 |

train_trip 站名解析顺序：①估算表城市对（"北京"按城市展开为北京南等，避免按
北京站直查漏站）→ ②站名/电报码直查 → ③ValueError（v1 城市对覆盖范围 =
估算表城市对）。选班次：`earliest` 历时最短（默认）/ `cheapest` 二等座最低；
12306 返回同城其他车站车次时，以实际班次到发站为准。

**动作技能（safety=action，不进 LLM/只读白名单）**：

| 工具名 | prepare（幂等组装意图） | commit（仅批准链路） |
|--------|------------------------|---------------------|
| `hotel_book` | 查询并组装酒店预订意图（房价/地址/booking_url） | 拒绝直调——RollingGo 当前无下单工具（0829 探测），预订/支付走人工 |
| `ticket_book` | 经 train_trip 组装车票意图（班次/二等座价） | 拒绝直调——12306 无公开购票 API，出票走官方候补/人工 |

`ActionSkill(Skill)` 基类约定两段式：LLM/自动化可发起 prepare（产生待确认
意图），commit 仅经 ActionQueue approve → 执行器注册表触发；支付恒 MANUAL。

---

## 7. Mock/Live 切换机制

### 配置方式

在 `config/settings.py` 中：

```python
@dataclass
class Settings:
    demo_mode: bool = False  # True → 强制 Mock 模式

    amap_api_key: str = ""        # 高德地图 API Key
    qweather_api_key: str = ""   # 和风天气 API KEY
    qweather_api_host: str = ""   # API Host（不带 https://）

    @property
    def use_real_api(self) -> bool:
        """天气：当 demo_mode=False 且配置了 API Key + Host 时，使用真实 API"""
        return not self.demo_mode and bool(self.qweather_api_key and self.qweather_api_host)

    @property
    def use_real_map_api(self) -> bool:
        """地图：当 demo_mode=False 且配置了 API Key 时，使用真实 API"""
        return not self.demo_mode and bool(self.amap_api_key)
```

### 切换逻辑

在 `tools/__init__.py` 的 `build_registry()` 中：

```python
# 地图 + 交通 + 景点 + 餐饮工具：按配置独立切换 Mock / Live（共享 AmapClient）
if settings.use_real_map_api:
    amap_client = AmapClient(api_key=settings.amap_api_key)
    registry.register(MapToolLive(amap_client))
    registry.register(TrafficToolLive(amap_client, world))       # world 用于交通 override
    registry.register(ScenicToolLive(amap_client, world))       # world 用于排队/票价 fallback
    registry.register(FoodToolLive(amap_client))
else:
    registry.register(MapTool())
    registry.register(TrafficTool())
    registry.register(ScenicTool(world))
    registry.register(FoodTool())

# 天气工具：按配置独立切换 Mock / Live
if settings.use_real_api:
    client = QWeatherClient(api_key=..., api_host=...)
    registry.register(WeatherToolLive(client, world))            # world 用于天气 override
    registry.register(WeatherWarningToolLive(client))
    registry.register(AirQualityToolLive(client))
    registry.register(WeatherForecastToolLive(client))
else:
    registry.register(WeatherTool(world))
    registry.register(WeatherWarningTool(world))
    registry.register(AirQualityTool(world))
    registry.register(WeatherForecastTool(world))

# 火车票查询组：按配置独立切换 Mock / Live（12306 公开接口，无需 API Key）
if settings.use_real_train_api:
    train_client = TrainClient()
    registry.register(TrainTicketToolLive(train_client))         # 4 个 Live 共享一个客户端
    registry.register(TrainTransferToolLive(train_client))
    registry.register(TrainRouteToolLive(train_client))
    registry.register(TrainPriceToolLive(train_client))
else:
    registry.register(TrainTicketTool())
    registry.register(TrainTransferTool())
    registry.register(TrainRouteTool())
    registry.register(TrainPriceTool())
```

> **注意**：天气和地图的 Mock/Live 切换互相独立。可以只配天气 Key（地图走 Mock），也可以只配地图 Key（天气走 Mock），或两者都配。
> `MapToolLive`、`TrafficToolLive`、`ScenicToolLive` 和 `FoodToolLive` 共享同一个 `AmapClient` 实例，地理编码缓存互通。
>
> **MockWorld override 机制**：`WeatherToolLive`、`TrafficToolLive` 和 `ScenicToolLive` 都接收可选的 `MockWorld` 参数。
> 在 Live 模式下，`MockWorld` 充当「突发事件 override 层」：
> - `set_weather(condition="暴雨", rain_probability=85)` → 覆盖 API 天气数据
> - `set_traffic_delay("北京", "故宫", delay_min=45)` → 覆盖 API 交通延误
> - `set_queue("故宫", 120)` → 覆盖景点排队时长（ScenicToolLive fallback）
> 这样 Demo 可以在真实 API 数据基础上注入模拟突发事件，展示决策闭环。

### API Key 安全管理

| 文件 | 说明 | 是否提交 Git |
|------|------|-------------|
| `config/settings.py` | 默认空值，从环境变量读取 | ✅ 提交 |
| `config/local_settings.py` | 真实 API Key，运行时覆盖 | ❌ 已 gitignore |
| `config/local_settings.example.py` | 模板文件 | ✅ 提交 |
| `.env.example` | 环境变量模板 | ✅ 提交 |

使用流程：
1. 复制 `config/local_settings.example.py` → `config/local_settings.py`
2. 填入真实 API Key 和 Host
3. `settings.py` 底部自动加载 `local_settings.py`，覆盖默认值

---

## 8. 和风天气 API 端点汇总

### 认证方式

- **认证类型**：API KEY（非 JWT）
- **请求头**：`X-QW-Api-Key: {your_api_key}`
- **响应压缩**：支持 gzip，请求头添加 `Accept-Encoding: gzip`

### 端点一览表

| # | 端点 | 方法 | 用途 | 调用工具 | 关键返回字段 |
|---|------|------|------|----------|-------------|
| 1 | `/geo/v2/city/lookup` | GET | 城市搜索 → Location ID + 坐标 | QWeatherClient.get_location_id() / get_location_coord() | `location[0].id` / `location[0].lat` / `location[0].lon` |
| 2 | `/v7/weather/now` | GET | 实况天气 | WeatherToolLive._fetch_now() | `now.temp/text/icon/windScale/humidity/precip/feelsLike/vis` |
| 3 | `/v7/indices?type=5` | GET | 天气指数（UV） | WeatherToolLive._fetch_uv_index() | `daily[0].category` |
| 4 | `/weatheralert/v1/current/{lat}/{lon}` | GET | 天气预警（v1） | WeatherWarningToolLive | `alerts[].headline/eventType.name/color.code/description` |
| 5 | `/airquality/v1/current/{lat}/{lon}` | GET | 空气质量（v1） | AirQualityToolLive | `indexes[].aqi/category` + `pollutants[].concentration.value` |
| 6 | `/v7/weather/24h` | GET | 逐小时预报 | WeatherForecastToolLive | `hourly[].fxTime/temp/iconCode/text/precip` |

### 完整 URL 格式

```
https://{api_host}{endpoint}?{query_params}

示例：
https://abc1234xyz.def.qweatherapi.com/v7/weather/now?location=101010100
```

### API 返回字段 → 工具返回字段映射总表

#### 实况天气 `/v7/weather/now`

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `now.temp` | `temperature_c` | `float()` |
| `now.text` / `now.icon` | `condition` | icon → `_QWEATHER_ICON_TEXT` 映射，fallback 到 text |
| `now.feelsLike` | `feels_like` | `float()` |
| `now.windScale` | `wind_kmh` | → `_WIND_SCALE_KMH` 蒲福风级换算 |
| `now.windDir` | `wind_dir` | 直接透传（风向，如 "东北"） |
| `now.humidity` | `humidity` | `int()` |
| `now.precip` | `rain_probability` | `> 0` → 80，否则 10 |
| `now.vis` | `visibility_km` | `float()` |

#### 天气指数 `/v7/indices?type=5`

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `daily[0].category` | `uv_index` | `int()`，失败默认 0 |

#### 天气预警 `/weatheralert/v1/current/{lat}/{lon}`（v1）

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `alerts[]` | `warnings` | 遍历映射 |
| `alerts[].headline` | `warnings[].title` | 直接透传 |
| `alerts[].eventType.name` | `warnings[].type` | 直接透传 |
| `alerts[].color.code` | `warnings[].level` | 直接透传（blue/yellow/orange/red） |
| `alerts[].description` | `warnings[].text` | 直接透传 |
| — | `has_warning` | `len(warnings) > 0` |

> **v1 迁移**：旧端点 `/v7/warning/now?location={id}` 已废弃（返回 403），
> 新端点使用经纬度路径参数，响应从 `warning[]` 改为 `alerts[]`，字段结构完全不同。

#### 空气质量 `/airquality/v1/current/{lat}/{lon}`（v1）

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `indexes[code=us-epa].aqi` | `aqi` | `int()`，优先取 us-epa，否则取第一个 |
| `indexes[code=us-epa].category` | `category` | 直接透传 |
| `pollutants[code=pm2p5].concentration.value` | `pm25` | `float()` |
| `pollutants[code=pm10].concentration.value` | `pm10` | `float()` |
| `pollutants[code=no2].concentration.value` | `no2` | `float()` |
| `pollutants[code=so2].concentration.value` | `so2` | `float()` |
| `pollutants[code=co].concentration.value` | `co` | `float()` |
| `pollutants[code=o3].concentration.value` | `o3` | `float()` |

> **v1 迁移**：旧端点 `/v7/air/now?location={id}` 已废弃（返回 403），
> 新端点使用经纬度路径参数，响应从单个 `now` 对象改为 `indexes` + `pollutants` 两个数组。
> AQI 从 `indexes` 数组中按 `code=us-epa` 提取，污染物浓度从 `pollutants` 数组按 `code` 匹配。

#### 逐小时预报 `/v7/weather/24h`

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `hourly[].fxTime` | `hours[].time` | 取末 5 字符（HH:MM） |
| `hourly[].temp` | `hours[].temp` | `float()` |
| `hourly[].iconCode` / `hourly[].text` | `hours[].condition` | iconCode → `_QWEATHER_ICON_TEXT` 映射 |
| `hourly[].precip` | `hours[].rain_probability` | `> 0` → 80，否则 10 |
| — | `summary` | 统计 `rain_probability > 50` 的小时数生成摘要 |

---

## 附：和风天气现象代码映射表（`_QWEATHER_ICON_TEXT`）

| 代码 | 中文 | 代码 | 中文 | 代码 | 中文 |
|------|------|------|------|------|------|
| 100 | 晴 | 101 | 多云 | 102 | 少云 |
| 103 | 晴间多云 | 104 | 阴 | 300 | 阵雨 |
| 301 | 强阵雨 | 302 | 雷阵雨 | 303 | 强雷阵雨 |
| 304 | 雷阵雨伴有冰雹 | 305 | 小雨 | 306 | 中雨 |
| 307 | 大雨 | 308 | 极端降雨 | 309 | 毛毛雨 |
| 310 | 暴雨 | 311 | 大暴雨 | 312 | 特大暴雨 |
| 313 | 冻雨 | 350 | 阵雨 | 399 | 雨 |
| 400 | 小雪 | 401 | 中雪 | 402 | 大雪 |
| 403 | 暴雪 | 404 | 雨夹雪 | 405 | 雨雪天气 |
| 406 | 阵雨夹雪 | 407 | 阵雪 | 499 | 雪 |
| 500 | 薄雾 | 501 | 雾 | 502 | 霾 |
| 503 | 扬沙 | 504 | 浮尘 | 507 | 沙尘暴 |
| 508 | 强沙尘暴 | 509 | 浓雾 | 510 | 强浓雾 |
| 511 | 中度霾 | 512 | 重度霾 | 513 | 严重霾 |
| 514 | 大雾 | 515 | 特强浓雾 | 900 | 热 |
| 901 | 冷 | 999 | 未知 | | |

---

## 9. 高德地图 API 端点汇总

### 认证方式

- **认证类型**：API Key（query string 参数）
- **参数名**：`key`
- **基础域名**：`https://restapi.amap.com`
- **坐标系**：GCJ-02（国测局坐标）
- **响应压缩**：支持 gzip，请求头添加 `Accept-Encoding: gzip`

### 端点一览表

| # | 端点 | 方法 | 用途 | 调用方法 | 关键返回字段 |
|---|------|------|------|----------|-------------|
| 1 | `/v3/geocode/geo` | GET | 地理编码（地址→坐标） | `AmapClient.geocode()` | `geocodes[0].location` (lng,lat) |
| 2 | `/v5/place/text` | GET | 关键词搜索 POI（v5） | `AmapClient.search_poi()` | `pois[].name/location/address/business` |
| 3 | `/v5/place/around` | GET | 周边搜索 POI（v5） | `AmapClient.search_poi_around()` | `pois[].name/location/address/business/distance` |
| 4 | `/v3/direction/transit/integrated` | GET | 公交路线规划 | `AmapClient.get_route(mode="transit")` | `route.transits[0].distance/duration` |
| 5 | `/v3/direction/driving` | GET | 驾车路线规划 | `AmapClient.get_route(mode="driving")` | `route.paths[0].distance/duration` |
| 6 | `/v4/direction/bicycling` | GET | 骑行路线规划 | `AmapClient.get_route(mode="riding")` | `paths[0].distance/duration` |
| 7 | `/v3/direction/walking` | GET | 步行路线规划 | `AmapClient.get_route(mode="walk")` | `route.paths[0].distance/duration` |

> **v5 升级说明**：POI 搜索端点从 `/v3/place/*` 升级到 `/v5/place/*`，参数变更：
> `extensions=all` → `show_fields=business,opentime_today,opentime_week,rating,cost,tag,alias`；
> `city` → `region` + `city_limit=true`；`offset` → `page_size`。
> v5 深度信息嵌套在 `business` 对象内（v3 在 `biz_ext` 内），`_normalize_poi()` 优先读 v5 `business`，fallback 到 v3 `biz_ext`。
> geocode/route 端点仍用 v3/v4（v5 无对应端点）。

### 完整 URL 格式

```
https://restapi.amap.com{endpoint}?key={api_key}&{query_params}

示例：
https://restapi.amap.com/v5/place/text?keywords=故宫&key=your_key&show_fields=business,opentime_today,opentime_week,rating,cost,tag,alias
```

### API 返回字段 → 工具返回字段映射

#### 地理编码 `/v3/geocode/geo`

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `geocodes[0].location` | `(lat, lng)` | 分割 `"lng,lat"` → `float()` |

#### POI 搜索 `/v5/place/text` 和 `/v5/place/around`（v5）

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `pois[].name` | `name` | 直接透传 |
| `pois[].location` | `lat` / `lng` | 分割 `"lng,lat"` → `float()` |
| `pois[].address` | `address` | 直接透传 |
| `pois[].business.rating` | `rating` | `_safe_float()`，fallback v3 `biz_ext.rating` |
| `pois[].business.cost` | `cost` | `_safe_float()`，fallback v3 `biz_ext.cost` |
| `pois[].business.tag` | `tag` | 直接透传，fallback v3 顶层 `tag` |
| `pois[].business.tel` | `tel` | 直接透传，fallback v3 顶层 `tel` |
| `pois[].business.opentime_today` | `opentime_today` | 直接透传（v5 新增，v3 无此字段） |
| `pois[].business.opentime_week` | `opentime_week` | 直接透传（v5 新增，v3 无此字段） |
| `pois[].business.alias` | `alias` | 直接透传（POI 别名，如 "紫禁城"） |
| `pois[].business.business_area` | `business_area` | 直接透传（POI 所属商圈） |
| `pois[].distance` | `distance` | `_safe_float()`（仅周边搜索返回） |

#### 路线规划 `/v3/direction/*`

**MapToolLive 路线规划**：

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `route.transits[0].distance` 或 `route.paths[0].distance` | `distance_km` | `int() / 1000`（米→公里） |
| `route.transits[0].duration` 或 `route.paths[0].duration` | `duration_min` | `int() / 60`（秒→分钟） |
| `route.transits[0].cost` | `fare` | `float()`（公交票价，仅 transit 模式） |
| `route.paths[0].tolls` | `fare` | `float()`（驾车过路费，仅 driving 模式） |
| — | `transit` | mode → 中文（公交/驾车/骑行/步行） |

**TrafficToolLive 交通状态**：

| API 字段 | 工具返回字段 | 转换 |
|----------|-------------|------|
| `route.transits[0].distance` 或 `route.paths[0].distance` | `distance_km` | `round(distance / 1000, 2)`（米→公里） |
| `route.transits[0].distance` 或 `route.paths[0].distance` | `note` | `距离 {distance/1000:.1f}km` |
| `route.transits[0].duration` 或 `route.paths[0].duration` | `duration_min` | `round(duration / 60)`（秒→分钟） |
| — | `congestion` | 驾车模式按速度推断（< 15km/h 拥堵 / < 30km/h 缓行 / 否则畅通） |
| — | `delay_min` | 驾车模式：`max(0, duration_min - round(distance_km/40*60))` |
| — | `note` | `距离 {x}km`，拥堵时追加 `，{congestion}延误约 {delay_min} 分钟` |

> **注意**：`MapToolLive` 和 `TrafficToolLive` 都调用高德路线规划 API，但返回结构不同：
> - `MapToolLive` 返回 `distance_km` / `duration_min` / `transit` / `fare`（路线信息，fare 从公交 cost 或驾车 tolls 填充）
> - `TrafficToolLive` 返回 `duration_min` / `congestion` / `delay_min` / `note` / `distance_km`（交通状态，含拥堵推断）

### 错误处理

高德 API 返回 `status` 字段：
- `"1"` — 成功
- `"0"` — 失败，检查 `infocode` 和 `info` 字段

常见错误码：
| infocode | 含义 |
|----------|------|
| `10001` | API Key 不正确 |
| `10003` | 日调用量超限 |
| `10004` | 请求过于频繁 |
| `10009` | 请求资源不存在 |
| `10010` | 无访问权限 |

---

## 10. 12306 API 端点汇总

### 认证方式

无 API Key。会话机制：先 `GET /otn/leftTicket/init` 拿到 Set-Cookie
（JSESSIONID/route 等），后续请求回传 `Cookie` 头。12306 无 token / stlCookie 机制。

### 端点一览表

| 用途 | 方法 | 端点 | 关键参数 |
|------|------|------|----------|
| 会话初始化 | GET | `https://kyfw.12306.cn/otn/leftTicket/init` | —（取 Set-Cookie） |
| 余票查询 | GET | `/otn/leftTicket/queryI` | `leftTicketDTO.train_date`、`leftTicketDTO.from_station`、`leftTicketDTO.to_station`、`purpose_codes` |
| 中转换乘 | GET | `/lcquery/queryG` | `train_date`、`from_station_telecode`、`to_station_telecode`、`middle_station`、`result_index`（每页 10 条翻页）、`can_query=Y`、`isShowWZ`、`purpose_codes`、`channel=E` |
| 经停站 | GET | `/otn/czxx/queryByTrainNo` | `train_no`（官方编号）、`from_station_telecode`、`to_station_telecode`、`depart_date` |
| 票价 | GET | `/otn/leftTicketPrice/queryAllPublicPrice` | 同余票参数；响应 `data[].queryLeftNewDTO` |
| 车站表 | GET | `/otn/resources/js/framework/station_name.js` | —（已内置 `tools/train/data/station_name.js`，可运行 `scripts/update_stations.py` 思路更新） |

> **query 后缀字母会轮换**：余票/换乘接口 URL 的末字母由 12306 不定期更换
> （余票曾 `queryU`→`queryG`→`queryI`，换乘曾 `queryI`→`queryG`）。
> 客户端跟随重定向；若持续反爬拦截，按真实跳转地址更新 `tools/train/client.py` 的 `_QUERY_PATHS`。

### 请求头（全部查询必须携带）

`User-Agent`（Chrome）、`Referer: https://kyfw.12306.cn/otn/leftTicket/init`、
`Host: kyfw.12306.cn`、`Accept: application/json, text/javascript, */*; q=0.01`、
`Accept-Language: zh-CN,zh;q=0.9`、`X-Requested-With: XMLHttpRequest`、
`Origin: https://kyfw.12306.cn`、`Connection: keep-alive`

### 余票响应解析（`data.result[]` 为 "|" 分隔字符串）

| 列索引 | 含义 | 列索引 | 含义 |
|--------|------|--------|------|
| `[1]` | `预订`/`停运` 标记 | `[28]` | 硬卧余票 |
| `[2]` | 官方编号（在"预订"标记后一列） | `[29]` | 硬座余票 |
| `[3]` | 车次号 | `[30]` | 二等座余票 |
| `[6]` / `[7]` | 出发/到达站电报码 | `[31]` | 一等座余票 |
| `[8]` / `[9]` | 出发/到达时刻 | `[32]` | 商务座余票 |
| `[10]` | 历时 | `[33]` | 动卧余票 |
| `[21]` | 高级软卧余票 | `[23]` / `[24]` / `[26]` | 软卧 / 软座 / 无座余票 |

余票值：数字 / `有` / `无` / `候补`；`--` 表示该车次无此坐席。

### 约束与注意

- **预售期**：仅支持查询今天 ~ 今天+14 天，参数校验在 `validate_depart_date()`
- **反爬**：命中后 302 到错误页（URL 含 `error.html` / `/ntce/`），客户端识别为业务错误
- **证书**：12306 证书链在部分 Python 环境校验失败，客户端关闭了证书校验（参考实现同款）
- **价格单位**：`queryAllPublicPrice` 原始价格单位 0.1 元，工具输出已换算为元
