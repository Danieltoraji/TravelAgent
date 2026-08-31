# 事件注入手册（Event Injection Cookbook）

> 服务于演示剧情 / 事件流程设计：用什么注入、怎么注入、注入后 App 表现什么、怎么编排。
> 端点契约与代码对应：`django_server/api/views.py` 的 `debug_inject`；决策链路
> `execution/execution_agent.py::handle_event / _significant`。

---

## 0. 一句话

`POST /api/debug/inject/` 构造一条 `MonitorEvent` 并走**真实链路**
（缓冲进 `/api/events` → 影响判定 → A 侧 LLM 决策 → 重规划 → 回填
`/api/replans` + 更新 `/api/timeline`）。App 每 5 秒轮询，**注入后一个
轮询周期内自动展示**，App 零改动。

- 演示环境：`http://39.96.89.133:8000`（云服务器，已部署）
- 本地环境：`http://127.0.0.1:8000`
- 鉴权：若服务器设了 `DEBUG_INJECT_TOKEN`，请求需带 `X-Debug-Token` 头
  （脚本用 `--token`）

---

## 1. 事件类型 × 阈值 × data 字段（设计剧情的基本单位）

| event_type | 触发决策的阈值（`_significant`） | 关键 data 字段 | 注入后 App 表现 |
| --- | --- | --- | --- |
| `weather` | `rain_probability >= 60` | `condition`, `rain_probability`, `uv_index`, `temperature_c`… | 事件卡片；达阈值 → LLM 评估 → 可能重规划 |
| `scenic` | `queue_min >= 50`（= impact_threshold） | `queue_min`, `open_hours`, `price`… | 事件卡片；达阈值 → 可能重规划 |
| `traffic` | `delay_min >= 30` | `delay_min`, `congestion`, `duration_min`, `mode`… | 事件卡片；达阈值 → 可能重规划 |
| `booking` | 有 `hotel_id` 且 `hotel_full=true` 或 `price_delta` 非空 | `hotel_id`, `hotel_name`, `hotel_full`, `price_delta` | **硬规则**：满房直接触发换宿决策（可不依赖 LLM 判定） |
| `food` | **永不触发决策**（`_significant` 无 food 分支） | 任意 | 仅进事件流展示（如「餐厅推荐更新」），不参与重规划 |

注意：

- **place 缺省规则**：`weather`/`traffic` 缺省挂当前城市；`scenic`/`booking`/`food` 必填。
- **booking 的 hotel_id 自动映射**：不传 `hotel_id` 时用 place 名称顶替（与满房回调同风格）。
- **影响评分细节**由 A 侧 LLM 给出（`decision.impact` 0–1），B 侧只做客观阈值初筛。

---

## 2. 预设场景卡（一键剧情）

| scenario | 事件 | 注入数据 | 典型剧情文案 | 预期结果 |
| --- | --- | --- | --- | --- |
| `storm` | weather | 暴雨，降雨 85% | 「天气突变：暴雨」 | 达阈值 → LLM 重规划（实测影响分 0.75，涉及全部户外景点） |
| `queue` | scenic | 排队 120 分钟 | 「故宫排队暴涨 20→120 分钟」 | 达阈值 → 重规划（压缩/调整游览顺序） |
| `traffic_jam` | traffic | 延误 45 分钟 | 「北京→故宫 交通拥堵延误 45 分钟」 | 达阈值 → 重规划 |
| `hotel_full` | booking | 满房 | 「酒店满房，需换宿」 | **硬规则**直接触发换酒店决策 + 预订 Action |

对应 payload：

```json
{"scenario": "storm"}
{"scenario": "queue", "place": "故宫"}
{"scenario": "traffic_jam", "place": "北京-故宫"}
{"scenario": "hotel_full", "place": "布丁酒店(北京西站店)"}
```

---

## 3. 三种注入方式

### 方式 A：脚本（推荐，可编排、可带 token）

```bash
# 单发
python demo/inject_events.py --base http://39.96.89.133:8000 storm
python demo/inject_events.py --base http://39.96.89.133:8000 queue --place 故宫

# 剧情三连（storm → queue → traffic_jam），间隔可配
python demo/inject_events.py --base http://39.96.89.133:8000 --all --interval 5

# 原始注入（任意类型 + 任意数据）
python demo/inject_events.py raw --event-type traffic --place 北京-故宫 \
    --data '{"delay_min": 60, "congestion": "严重拥堵"}'
```

### 方式 B：curl（单条卡点）

```bash
curl -X POST http://39.96.89.133:8000/api/debug/inject/ \
  -H "Content-Type: application/json" \
  -d '{"scenario": "storm"}'
```

### 方式 C：编程调用（脚本/自动化/App 调试面板）

```python
import requests
requests.post("http://39.96.89.133:8000/api/debug/inject/",
              json={"scenario": "queue", "place": "故宫"},
              headers={"X-Debug-Token": "xxx"})
```

---

## 4. 响应字段（设计流程时用它判断「该等多久/下一步」）

```json
{
  "status": "ok",
  "event": {"event_type": "weather", "place": "北京", "data": {...}},
  "significant": true,          // 是否达 B 侧阈值
  "decision": "replanned",      // replanned | recorded | hook_error | not_significant
  "replan": {"id": "replan-2", "decision": {"reason": "...", "diff_summary": [], "impact": 0.75}},
  "timeline_changed": true
}
```

| decision | 含义 | 流程含义 |
| --- | --- | --- |
| `replanned` | 重规划完成，新时间轴已应用 | App 下个轮询会刷新路线；可进入「展示 diff」环节 |
| `recorded` | 达阈值、A 侧处理过但判无需重规划 | 剧情未达重规划条件（如单事件影响不足），需叠加事件 |
| `hook_error` | 决策引擎报错（多为 LLM Key/网络） | 检查服务器 DEEPSEEK_API_KEY |
| `not_significant` | 未达阈值 | 事件只展示不决策；检查 data 数值（如降雨 85 应为 85 而非 0.85） |

---

## 5. 剧情编排建议（事件流程设计）

### 节奏参数（实测）

- App 轮询周期：**5 秒**（`App.tsx` 定时器）
- LLM 决策耗时：**约 4 秒**（注入 → 新时间轴落地，实测 storm 11:01:06 → 11:01:10）
- 因此单事件完整可见周期 ≈ **10 秒内**（注入 → App 看到事件 → App 看到重规划）

### 推荐剧本时间线（三连击）

| 时刻 | 动作 | App 表现 | 讲解要点 |
| --- | --- | --- | --- |
| T0 | App 提交规划（自动清空旧状态） | 展示初始路线 | 正常规划结果 |
| T0+2min | 注入 `storm` | 事件卡片「天气突变」→ ~5s 后决策卡片 + 路线调整 | 天气影响评估（影响分） |
| T0+3min | 注入 `queue 故宫` | 新事件 → 再次重规划 | 排队连锁影响 |
| T0+4min | 注入 `traffic_jam` | 新事件 → 第三次重规划 | 多事件叠加，最终方案 |
| T0+5min | （可选）注入 `hotel_full` | 硬规则换宿 + 预订 Action 入队 | 决策 → 动作 → 用户确认链路 |

### 编排注意点

1. **事件间隔 ≥ 10 秒**（`--interval 10` 以上）：给 LLM 决策 + App 轮询留时间，
   否则事件与重规划会互相叠压，视频观感乱。
2. **单事件不一定触发重规划**（`recorded`）：设计剧情时优先用 preset 数值
   （它们都远超阈值）；若自定义 data，对照第 1 节阈值。
3. **叠加效应是特性**：多次注入会逐次重规划，正好展示「持续监控 → 动态调整」。
4. **`persist_world` 默认别开**：开启后假池状态被改写，后续真实轮询会持续
   产生同类事件并可能重复触发决策（想展示「异常持续存在」时才用，
   并接受多轮重规划）。
5. **booking 是硬规则**：`hotel_full` 不需要 LLM 判定也能走通（适合做
   「无 LLM 也能演示」的保底剧情）。
6. **food 只展示不决策**：适合做「事件流丰富度」而非「重规划」剧情。
7. **状态复位**：新 `POST /api/plan/` 自动清空 events/replans/actions；
   或服务器 `docker compose restart web`。演示前务必复位一次。
8. **公网演示**：云服务器注入端点当前无鉴权（若未配 Secret）；演示完建议
   提交 `deploy.yml` 的 `DEBUG_INJECT_TOKEN` 透传并重新部署。

---

## 6. 就绪检查清单（每次演示前）

```bash
# 1. 服务器在线 + 新代码
curl -s -o /dev/null -w "%{http_code}\n" http://39.96.89.133:8000/api/debug/inject/   # 期望 405

# 2. 注入链路（先建行程后注入；plan 会清空旧状态）
curl -s http://39.96.89.133:8000/api/status/ | grep timeline_set                     # true

# 3. 决策链路（Key 生效）
python demo/inject_events.py --base http://39.96.89.133:8000 storm                    # 期望 decision=replanned

# 4. 复位（可选，演示前清场）
# SSH 服务器：docker compose restart web
```

---

## 7. 实测发现与已修复（2026-08-31 云服务器验证 + A 侧修复）

**A 侧 RePlanner 在「live 真源规划 + 假图候选池」组合下存在 id 体系断点**，
已定位并修复（`a_side/algorithoms/replanner.py` + `b_contract.py` +
`django_server/api/views.py`，待部署）：

| 断点 | 现象 | 修复 |
| --- | --- | --- |
| queue 增量修复空转 | 注入排队后 `decision=replanned` 但 `diff_summary=[]`、时间轴不变 | 候选池 key（`BJ_XXX`）与计划节点 id（`scenic_N`）两套体系 → 新增 `_pool_name_index` 名称/别名兜底，贯穿受影响天定位 / `_restore_day_spots` / `_day_must_keys`；兜底景点保留 live 时长（避免大景点时长被假池突变）；`_build_changes` 按名称归并（避免同名景点误报 removed+added） |
| must 保护失效 | live 计划重规划时必去景点（故宫）可被移除 | `is_must_visit` 经 `Place.details` 在 B→A 往返中透传（`_node_to_place` 写 / `trip_timeline_to_plan` 读） |
| 换宿选不回新酒店 | 酒店满房后 diff 显示 `[hotel_changed]` 但酒店本体不变 | B 侧注入端点 `_resolve_hotel_id`：place 名称 → 假池 Hotel.id；live 酒店（如 `577984`）映射失败回退名称，可显式传 `data.hotel_id` |
| weather 降权不生效 | storm 注入后时间轴不变 | `_translate_weather_event`：事件未指定景点（城市级天气）时对候选池全部景点降权（fallback 全量重跑时生效） |

**修复后实测（本地复现）**：注入景山公园排队 300 → `changes=[removed 景山公园（为遵守每日出行时长限制）]`，
故宫博物院零变化。**「小景点排队激增 → 重规划移除该小景点、大景点不动」的演示效果达成。**

**当前演示剧情（修复部署后）**：

```
小雨（未达阈值，展示克制） → 小景点排队（重规划移除该景点，diff 清晰） → 酒店满房（换宿 + Action 链路）
```
