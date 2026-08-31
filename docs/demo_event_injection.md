# 突发事件注入（演示重规划）说明

演示视频需要「注入突发事件 → 展示重规划」。本方案在后端加了一个调试专用端点
`POST /api/debug/inject/`，它**走真实链路**：构造 `MonitorEvent` →
`ExecutionAgent.handle_event()` → 影响判定 → A 侧决策（`BDecisionHook`）→
重规划 → 回填 `/api/replans`、`/api/timeline`、Action Queue。

Android App **零改动**：它照常轮询 `/api/events`、`/api/replans`、
`/api/timeline`，注入后下个轮询周期即可看到事件与重规划结果。

---

## 一、前置条件

1. 后端已启动：`python manage.py runserver 0.0.0.0:8000`（在 `django_server/` 下）。
2. 已建好行程：`POST /api/plan/`（或 `POST /api/timeline/`）成功，`GET /api/status/`
   中 `timeline_set` 为 true。**没有时间轴时注入端点返回 400。**
3. 决策 hook 是 A 侧 LLM（`BDecisionHook`），注入后重规划需要数秒；若未配置
   LLM Key，`decision` 会返回 `hook_error`（事件仍会进 `/api/events`，但不重规划）。

## 二、注入方式（三种）

### 方式 1：curl 单条（推荐，演示卡点用）

```bash
# 暴雨：降雨概率 10% → 85%
curl -X POST http://127.0.0.1:8000/api/debug/inject/ \
  -H "Content-Type: application/json" \
  -d '{"scenario": "storm"}'

# 排队暴涨：故宫 20 → 120 分钟
curl -X POST http://127.0.0.1:8000/api/debug/inject/ \
  -H "Content-Type: application/json" \
  -d '{"scenario": "queue", "place": "故宫"}'

# 交通拥堵延误 45 分钟
curl -X POST http://127.0.0.1:8000/api/debug/inject/ \
  -H "Content-Type: application/json" \
  -d '{"scenario": "traffic_jam", "place": "北京-故宫"}'

# 酒店满房（触发换宿决策）
curl -X POST http://127.0.0.1:8000/api/debug/inject/ \
  -H "Content-Type: application/json" \
  -d '{"scenario": "hotel_full", "place": "皇城景观酒店"}'
```

### 方式 2：脚本（剧情三连 / 原始注入）

```bash
# 剧情三连：storm → queue(故宫) → traffic_jam，间隔 3 秒
python demo/inject_events.py --all --interval 3

# 单发
python demo/inject_events.py storm
python demo/inject_events.py queue --place 故宫

# 原始注入（任意事件类型 + 自定义数据）
python demo/inject_events.py raw --event-type scenic --place 故宫 \
    --data '{"queue_min": 120}'
```

脚本打印注入结果摘要（是否达阈值 / 是否重规划 / 原因 / diff），仅标准库依赖。

### 方式 3：穿透后从任意设备触发

```bash
python demo/inject_events.py --base http://节点地址:端口 storm
```

把 `--base` 换成 Sakura TCP 隧道的地址即可。演示时可用手机/另一台电脑触发，
效果更真实（观众看不到是脚本在发）。

## 三、请求体参考

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `scenario` | 二选一 | 预设：`storm` / `queue` / `traffic_jam` / `hotel_full` |
| `event_type` | 二选一 | 原始注入：`weather` / `scenic` / `traffic` / `food` / `booking` |
| `place` | 视类型 | queue/traffic_jam/hotel_full 与 scenic/food/booking 必填；weather/traffic 缺省挂城市 |
| `data` | 原始注入时 | 事件数据对象，如 `{"queue_min": 120}` |
| `persist_world` | 否 | `true` 时同步写进假池（MockWorld），后续轮询持续可见。**默认关**：写进假池后轮询会再次产生同类事件，可能重复触发决策 |
| `rule_name` / `spot_id` | 否 | 透传给 MonitorEvent（默认 `debug-inject` / 空） |

预设场景对照（对齐 `demo/demo_scenario.py` 剧情）：

| scenario | 事件 | 数据 | 达阈值条件（`_significant`） |
| --- | --- | --- | --- |
| `storm` | weather | `rain_probability=85` | ≥ 60 |
| `queue` | scenic | `queue_min=120` | ≥ impact_threshold(50) |
| `traffic_jam` | traffic | `delay_min=45` | ≥ 30 |
| `hotel_full` | booking | `hotel_full=true, hotel_id=place` | 有 hotel_id 且满房 |

## 四、响应说明

```json
{
  "status": "ok",
  "event": { "...": "MonitorEvent 序列化" },
  "significant": true,
  "decision": "replanned",          // replanned | recorded | hook_error | not_significant
  "replan": { "id": "replan-1", "decision": { "reason": "...", "diff_summary": [...] } },
  "timeline_changed": true
}
```

- `not_significant`：数据未达影响阈值（如降雨 < 60%），不触发决策；
- `recorded`：达阈值、A 侧已处理但认为无需重规划；
- `replanned`：重规划完成，`/api/timeline` 已更新、`/api/replans` 有新记录；
- `hook_error`：达阈值但决策引擎报错（多半是 LLM Key/网络问题），事件仍在 `/api/events`。

## 五、鉴权与安全

- 服务端设 `DEBUG_INJECT_TOKEN=xxx`（环境变量）后，请求必须带
  `X-Debug-Token: xxx` 头，否则 401。未设置时端点开放（与项目无认证风格一致），
  但每次注入会在服务端日志打警告。
- 注入端点暴露在公网（穿透）时**务必设置 token**，且演示结束立即关闭隧道。
- 本项目其余写接口（`/api/tools/*/invoke/` 等）同样无认证，穿透本身就是临时手段。

## 六、演示剧本建议

1. 手机连隧道地址，App 进入行程页（先 `POST /api/plan/` 建好时间轴）。
2. 正常数据流跑 1–2 分钟（真实 API 轮询展示「真源」）。
3. 卡点：电脑上 `python demo/inject_events.py storm`（或 curl）。
4. App 轮询到事件 → 展示影响判定 → 数秒后出现重规划新路线与 diff 说明。
5. 需要连续剧情时用 `--all`（间隔 5–8 秒，留足 LLM 决策时间）。
6. 录完关闭隧道。

## 七、常见问题

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 400 `No timeline set` | 还没建行程 | 先 POST /api/plan/ |
| 400 `place is required` | scenic/booking 等未传地点 | 补 `place` |
| 400 `unknown scenario` | 预设名拼错 | 用 storm/queue/traffic_jam/hotel_full |
| `hook_error` | A 侧 LLM 未配 Key 或超时 | 检查服务端日志；配置 Key 或接受只展示事件不重规划 |
| 401 | token 不匹配 | 请求带 `X-Debug-Token` 头（脚本用 `--token`） |
| 注入成功但 App 没反应 | App 轮询周期未到 | 等一个轮询周期或手动下拉刷新；确认 App 连的是隧道地址 |
