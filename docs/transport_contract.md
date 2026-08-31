# 市内交通段契约（transport.details）

2026-09-01 起，`GET /api/timeline/`（及 `/api/plan/` 响应）的 transport 段
携带公共交通导航信息，C 端可直接渲染，无需兜底猜测。

## 字段表

| 字段 | 类型 | 示例 | 说明 |
| --- | --- | --- | --- |
| `from` / `to` | string | 故宫博物院 / 景山公园 | 起终点地名（矩阵透传） |
| `distance_km` | number | 1.76 | 总路程（公里） |
| `source` | string | `live` / `live_map_api` / `estimate` | 数据来源：真源公交 / 真源矩阵 / 假图估算 |
| `mode` | string | `transit` | 交通方式（enrich 后） |
| `duration_min` | number | 37 | 预计耗时（分钟，真源公交值，展示用） |
| `fare` | number | 2.0 | 票价（元，公交） |
| `transit` | string | 公交 | 方式中文名 |
| `transit_text` | string | 步行858m → 124路 1站 → 步行398m | **具体线路导航**（逐段拼接，公交段线路名+站数、步行段距离） |
| `walking_m` | number | 1256 | 步行总距离（米） |

## 渲染建议（C 端）

- 主行：`transit_text`（如「地铁8号线 3站 → 步行300m」）
- 副行：`{distance_km}km · 约{duration_min}分钟 · ¥{fare}`
- `source == "live"` 时正常展示；`live_map_api`/`estimate`（未 enrich 成功）时
  仅有 `from/to/distance_km`，可标注「估算」。
- `arrival/end_time` 仍是排程时长（矩阵分钟），与 `duration_min`（公交真源）
  可能不同——展示耗时优先用 `duration_min`，时间轴顺序仍按 `arrival/end_time`。

## 触发时机

| 场景 | 是否带导航 |
| --- | --- |
| `POST /api/plan/`（新规划） | ✅ enrich（并发 3，对齐高德 QPS，失败静默，plan 约 +2~8s） |
| 对话改行程（`update_timeline` 应用后） | ✅ enrich |
| 事件重规划（`/api/debug/inject/` 触发） | ⏳ 暂不带（后续可加） |
| demo 模式 / 无高德 key | ❌ 仅有矩阵透传字段（`from/to/distance_km/source`） |

## 实现位置

- 线路提取：`tools/amap_client.py::_extract_transit_route`（transit_text/walking_m）
- 透传：`tools/map_tool.py::_route_live` → `a_side/data_transmission/b_contract.py::_node_to_place`
- enrich：`django_server/runtime/agent_runtime.py::enrich_transport_details`（并发 3）
