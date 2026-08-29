# 代码实现裂缝与修复措施（2026-08-28）

> **本文档定位**：收录当前架构内可直接修复的**点状缺陷**（point fix）——不引入新抽象，
> 每条修复独立可交付。抽象与泛化设计（三轴分类、技能层、两段式动作等）见姊妹篇
> **《工具封装设计优化方案》**（`docs/tool_encapsulation_design_20260828.md`，下称"设计文档"）。
>
> **分界与接口**：本文档的修复先于设计文档实施，且为后者铺路——每条修复写成独立
> 函数/独立改动，设计文档的泛化机制直接注册复用它们，无返工。少数问题的正解就是
> 设计本身，本文档只标注"移交"（见 §6 移交清单）。
>
> 本文档取代《工具封装考察报告》（`tool_encapsulation_review_20260828.md`，已删除）
> 中的"发现"与"L0/L3"部分；行号以 2026-08-28 工作区为准。

---

## 目录

- [使用说明](#使用说明)
- [1. 工具契约裂缝（C1–C7）](#1-工具契约裂缝c1c7)
- [2. 执行侧裂缝（E1–E8）](#2-执行侧裂缝e1e8)
- [3. A 侧消费裂缝（A1–A3）](#3-a-侧消费裂缝a1a3)
- [4. C 端 / 文档 / 流程裂缝（R1–R5）](#4-c-端--文档--流程裂缝r1r5)
- [5. 修复批次顺序](#5-修复批次顺序)
- [6. 移交清单（归设计文档解决）](#6-移交清单归设计文档解决)

---

## 使用说明

每条裂缝统一五段格式：**现象 → 证据（文件:行号）→ 影响 → 修复措施 → 验收标准**。
修复措施遵守三条约束：

1. **不改调用方契约**：修复后现有消费者（A 侧 live_data、B 侧 execution/booking、
   C 端 REST）零改动即可工作；
2. **不引入新抽象**：需要新框架才能修的，进 §6 移交清单；
3. **Mock/Live 以 Live 为准**：两版不一致时统一到 Live 形状（Live 才是真实数据形状）。

---

## 1. 工具契约裂缝（C1–C7）

### C1 hotel detail：Mock 与 Live 两套命名

- **现象**：同一 `action=detail`，Mock 返回 `roomRatePlans`（camelCase：
  `roomName/averagePrice/mealTypeStr/isOnRequest`…），Live 返回 `rooms`
  （snake_case：`room_name/average_price/meal_type/on_request`…）且多 `raw` 键。
- **证据**：`tools/hotel_tool.py:116-146`（Mock）vs `341-385`（`_normalize_detail_result`）。
- **影响**：消费方必须写两套解析；"调用方零改动"承诺在 detail 上失效。
- **修复**：Mock 的 detail 返回改为与 Live 完全一致（`rooms` + snake_case 子字段，
  `bookingUrl` 顶层保留）；Mock 数据值保持示例值。
- **验收**：新增同构断言测试——`HotelTool().execute(action="detail",...).data` 与
  `HotelToolLive(mock_client).execute(...).data` 的键集合（浅层）完全一致。

### C2 food：Mock 缺 `location` 坐标

- **现象**：Mock 两条固定餐厅无 `location` 键；Live 输出 `"lng,lat"` 串。
- **证据**：`tools/food_tool.py:35-42`（Mock）vs `115-117`（Live）；A 侧
  `live_data.py:596-619` **无坐标即丢弃餐厅**。
- **影响**：假数据模式下所有 Mock 餐厅无法参与通勤计算，A 侧候选为空。
- **修复**：Mock 两条餐厅补 `location`（王府井一带真实经纬度串，如
  `"116.410,39.914"`），其余键不动。
- **验收**：`live_data.make_live_restaurants_provider` 在 Mock registry 上能取到
  带坐标餐厅（单测断言 normalize 输出非空）。

### C3 train_ticket：Live 丢失 `*_code` 电报码

- **现象**：Mock 行含 `from_station_code/to_station_code`；Live `_run` 显式
  `pop` 掉换成了中文名。
- **证据**：`tools/train/tools.py:50-62`（Mock）vs `94-95`（Live pop）。
- **影响**：Mock/Live 字段集不一致；下游（经停/票价联动、C 端站牌展示）拿不到
  电报码。
- **修复**：Live 保留 `*_code`（不再 pop），`from_station/to_station` 中文名照常
  输出——两键并存，与 Mock 一致。
- **验收**：`test_train_tool.py` 增断言 Live 输出含 `from_station_code`；更新现有
  断言。

### C4 map batch_route：`mode` 字段语义混用

- **现象**：同一 `mode` 键，Mock 返回中文描述串（"地铁1号线 + 步行800m"），
  Live 返回英文模式名（"driving"）。
- **证据**：`tools/map_tool.py:256` vs `419`。
- **影响**：按 `mode` 分支/展示的消费方两边行为不同。
- **修复**：统一为英文模式名；Mock 的中文描述串挪到新字段 `transit_text`（Live 已
  有同名字段先例，见城际 route 返回），旧键内容不变者无需迁移。
- **验收**：batch_route 相关单测更新后全绿；`mode` 值域 ⊆ {transit, driving,
  walking, riding}。

### C5 input_schema 全程无人校验

- **现象**：`input_schema` 只用于展示；`registry.call → execute → _run(**kwargs)`
  纯直传，参数名拼错抛 TypeError、必填缺失靠各 `_run` 手写 ValueError（Mock 版
  多数不写）。
- **证据**：`tools/base_tool.py:31`（注释"供上层校验与展示"）、`145-146`；
  `food_tool.py:33`（签名无 **kwargs）。
- **影响**：LLM tool_call（设计文档 §6）与 C 端直调的参数错误被吞成笼统 ERROR；
  schema 与实现漂移无告警。
- **修复**：`BaseTool.execute` 在调用 `_run` 前做**轻量校验**（required 缺失 +
  property type 基本匹配），失败抛 `ValueError("参数校验失败: ...")`（业务错误，
  不重试）；不引入 jsonschema 库（延续零依赖），只覆盖 schema 里声明了的字段。
- **前置依赖**：先完成 C6（schema 本身要正确），否则校验一上线就误伤。
- **验收**：缺 required 参数 → ERROR 且 error 含字段名；多传未声明参数 → 保留
  现状（不报错，避免破坏 **kwargs 直传兼容）；全量测试适配。

### C6 schema 与实现不同步

- **现象/证据/修复**（三条独立小项）：
  - `scenic` schema 无 `city` 参数，但 Live `_run` 签名有 `city="北京"`
    （`scenic_tool.py:161`）→ schema 补 `city`；
  - `hotel` Mock 版 detail 不校验 `hotelId/name`（`hotel_tool.py:147` 返回空
    roomRatePlans），Live 强校验（L243-244）→ Mock 补同款 ValueError；
  - `weather` 系 schema `required: ["city"]` 但实现 `city or "北京"` 兜底
    （`weather_tool.py:40` 等 8 处）→ 实现去掉兜底、缺失即 ValueError
    （与 required 语义对齐；`CITY_MOCK` 兜底保留给 Mock 构造参数而非静默替换）。
- **影响**：schema 是未来 LLM 白名单与 C 端文档的真相源，漂移即事故。
- **验收**：C5 校验上线后全量测试通过（校验器本身就是验收器）。

### C7 `ToolStatus.NO_DATA` 死状态

- **现象**：枚举定义了 `NO_DATA`（`core/schemas.py:34`），文档列了三态
  （`tool_introduction.md:132-138`），代码从未产出；空结果一律 `OK + []`。
- **影响**：消费方（如 A 侧 `_tool_payload`）无法区分"查询失败"与"查询成功但
  无数据"；文档失真。
- **修复（两步走）**：第一步（本文档）——**文档对齐现实**：标注 NO_DATA 为
  预留位、当前空结果返回 `OK + []`；第二步（设计文档 action 模式就绪后）——
  在语义明确处启用（如 scenic/train 查询空结果）。**不建议现在直接启用**：
  A 侧 `_tool_payload` 对非 ok 一律返回 None 降级假源，贸然启用会改变回退语义。
- **验收**：文档更新；全量测试不受影响。

---

## 2. 执行侧裂缝（E1–E8）

### E1 HOTEL_BOOK 动作是死信（无执行器）

- **现象**：重规划换宿后产出 `ActionItem(title="预订{新酒店}",
  target="hotel:{name}", type="HOTEL_BOOK")`，但没有任何代码消费 `hotel:` 前缀
  ——动作永远 PENDING，无 BookingRecord、无确认码。
- **证据**：`agent_runtime.py:65-73`（产出）vs `booking_manager.py:241-244`
  （`_mark_action` 只匹配 `target=="booking:{id}"`）。
- **影响**：换宿闭环在"建议"处断链——A 侧选了新酒店，B 侧永远不会真的去订。
- **修复**：新增独立处理函数 `_execute_hotel_booking(name) -> BookingRecord`
  （建议放 `booking_manager.py`）：
  1. 调 `hotel` 工具 `action="search", city=timeline.city` 按名称匹配取
     price/address 等字段；
  2. 调 `self.prepare(place=name, booking_type="hotel", ...)` 生成真实
     BookingRecord；
  3. 原 HOTEL_BOOK 动作标记 EXECUTED 并在 note 回填新 `booking_id`
     （target 补挂 `booking:{id}`，后续 confirm 走既有链路）。
  写成独立函数是为了让设计文档的执行器注册表（`{kind: executor}`）直接注册它。
- **验收**：单测——满房换宿闭环后，ActionQueue 中 HOTEL_BOOK 变 EXECUTED 且
  存在对应 `booking_type="hotel"` 的 BookingRecord。

### E2 approve 与 confirm 是两条平行线

- **现象**：`POST /api/actions/{id}/approve/` 只把 ActionItem 翻成 APPROVED
  （`views.py:307-314`），不触发任何执行；`POST /api/booking/{id}/confirm/`
  产生真实副作用却不校验对应动作是否已批准（`views.py:229-255`）。
- **影响**：批准语义与执行语义脱钩——"已批准"的动作无人执行，未批准的预订
  可以被直接 confirm，权限模型形同虚设。
- **修复**：approve 端点增加执行语义——对 `target` 可识别的动作（`booking:{id}`
  → 调 `booking_manager.confirm(id)`；`hotel:{name}` → E1 的处理函数），
  approve 即授权并执行；执行失败保持既有 FAILED/BLOCKED 闭环。**交互语义变化，
  需与 C 端确认前端文案与轮询逻辑后再上线**；对暂无执行器的 target
  （`calendar:` 等）保持仅标记。
- **验收**：集成测试——approve(book 动作) 后 BookingRecord 进入 SUBMITTED；
  confirm 对未 approve 的动作行为不变（本条不收紧 confirm，避免 breaking，
  收紧与否记入设计文档决策项）。

### E3 booking_type 枚举三处不一致

- **现象**：工具 schema enum `["scenic","hotel","transport"]` 缺 `food`
  （`booking_tool.py:36-40`）；manager 注释/动词表支持 4 种
  （`booking_manager.py:41,157`）；自动预约实际只产出 scenic/food
  （`execution_agent.py:282-284`）。
- **影响**：schema 校验（C5）上线后 `booking_type="food"` 会被误拒。
- **修复**：`booking_tool.py` schema enum 补 `"food"`；三处口径写进注释互相引用。
- **验收**：`execute(action="prepare", booking_type="food")` 通过校验。

### E4 `mark_confirmed` 无生产调用方

- **现象**：`SUBMITTED → CONFIRMED`（"服务方确认"）只有 demo 与测试调用，
  无端点无后台任务。
- **证据**：`booking_manager.py:197-201`；全仓引用仅 `demo_scenario.py:299`。
- **影响**：状态机断尾，C 端永远看不到 CONFIRMED。
- **修复**：暴露 `POST /api/booking/{id}/mark-confirmed/`（及 FastAPI 同款），
  语义注明"服务方确认回调（Demo 期人工/脚本触发）"；生产化时由 Live 适配层
  回调（设计文档动作模式）。
- **验收**：端点集成测试 SUBMITTED→CONFIRMED；非法前置状态 → 400。

### E5 ActionQueue 无持久化、无审计字段

- **现象**：队列是内存 list（`booking_manager.py:65`），重启即失；动作只有
  `created_at`，无决定时间/决定人。
- **影响**：审批审计缺失（`docs/交付文档.md:359` 自列为未实现项）。
- **修复**：`enqueue/_mark_action` 时同步落盘 `logs/actions.json`（单用户 Demo
  规模，json 追加写即可），启动时加载未决动作；ActionItem 增 `decided_at/
  decided_by=""` 可选字段（默认空，to_dict 透出）。
- **验收**：重启进程后 `GET /api/actions/` 仍返回未决动作；审批后落盘含
  decided_at。

### E6 时间轴无法表达预约状态

- **现象**：`Place`（`core/schemas.py:156-181`）没有任何 booking 关联字段；
  时间轴与 BookingRecord/ActionQueue 之间无外键。C 端无法展示"已预约/已确认"。
- **修复**：`Place` 增可选字段 `booking_id=""`、`booking_status=""`（默认空，
  向后兼容，asdict 自动透出）；`execution_agent._maybe_auto_book` 成功后回填
  对应 Place（按 name+date 定位）。
- **验收**：自动预约后 `GET /api/timeline/` 对应 Place 携带 booking_id；
  旧 timeline 载入不受影响。

### E7 booking 的 hotel/transport 类型 prepare 不自动填充

- **现象**：自动填充分支只有 scenic/food（`booking_manager.py:107-127`），
  hotel/transport 类型 prepare 时 price/tel/address/open_hours 全留空。
- **影响**：C 端确认页信息不全；满房闭环之外的真实预订缺数据。
- **修复（现行架构内的点状版）**：
  - `hotel` 分支：调 `hotel` 工具 `action="search", city=...`，按名称取最匹配
    条目填 `price=price_per_night`、`address`（tel/open_hours 无来源，留空并
    注释）；
  - `transport` 分支：调 `train_ticket`+`train_price`（from/to 取 Place 或
    booking 上下文，date=target_date）选最快直达，`price` 取二等座价、note 记
    车次/时刻；
  - 写成两个独立私有方法 `_autofill_hotel/_autofill_transport`，设计文档的
    lodging/train_trip 技能就绪后改为调用技能。
- **验收**：单测——hotel/transport 类型 prepare 后 price/address 非空（Mock 数据
  下）；查询失败时字段留空且不阻断 prepare。

### E8 未兑现的占位：BookingRequest 与 booking_plan.md

- **现象**：`BookingRequest` dataclass（`core/schemas.py:273-279`）全仓零引用；
  `docs/booking_plan.md` 是 0 字节空文件。
- **修复**：删除 `BookingRequest`（YAGNI，真实需求由设计文档两段式动作承载）；
  删除空 `booking_plan.md`（规划内容已由本系列两份文档承载）。
- **验收**：删除后全量测试通过（应无引用）。

---

## 3. A 侧消费裂缝（A1–A3）

### A1 `tool_specs` 死代码

- **现象**：`execution_agent.py:183` 把 `tool_specs` 放进
  `DecisionRequest.context`，下游无任何消费者（`BDecisionHook` 只读
  `context["impact_threshold"]`，context 不进 prompt）；白构造、白传输入。
- **修复**：从 `DecisionRequest.context` 移除 `tool_specs` 构造（省 token 与
  误导）；`tests/test_tool_provider.py` 相关断言同步调整。真正的"LLM 看工具"
  由设计文档 §6 function calling 方案取代（`list_for_llm()` 走 OpenAI tools
  参数，不进 context）。
- **验收**：全量测试通过；DecisionRequest 序列化不再含 tool_specs。

### A2 LLM 客户端 `tools` 参数死代码

- **现象**：`DSClient.py:97-99`、`GLMClient.py:78-80` 支持
  `params["tools"]+tool_choice`，但全仓 4 处 `generate()` 无一传 tools；
  循环也不理解 `finish_reason=="tool_calls"`（会当非 JSON 重试后抛错）。
- **修复**：**本文档只标注不修**——保留参数位并加注释
  "预留：function calling 回路见设计文档 §6"；启用（转换器+回路+降级）整体
  属设计文档 §6，分散修会留下半成品回路。
- **验收**：无代码变化；注释与设计文档互相引用。

### A3 live_data normalize 别名链脆弱

- **现象**：`live_data.py` 各 normalize 用大量别名候选与形状猜测取字段
  （`name/title/poi_name`、坐标三形状、时长三候选键 + "秒≥600"启发式、
  营业时间正则抽数，L212-289、L502-555、L596-619）。
- **影响**：契约漂移的"症状吸收器"——能扛住小漂移，但掩盖违约且难维护
  （每真源化一个领域就长一截）。
- **修复（本文档内的缓解版）**：给每个 normalize 补"主字段"注释块（声明唯一
  真相字段名，对照各工具文档）；别名命中非主字段时打 `logger.debug`（不告警，
  先收集漂移频率）。治本（output_schema + 主字段强契约）在设计文档 §2/§3。
- **验收**：日志埋点后跑一遍 live 冒烟，记录别名命中统计（作为设计文档的
  收敛依据）。

---

## 4. C 端 / 文档 / 流程裂缝（R1–R5）

### R1 工具调用双入口过滤不一致（按既定决策：只标注，不动代码）

- **现象**：`POST /api/tools/invoke/` 走只读白名单（`views.py:88-100`）；
  `POST /api/tools/<name>/invoke/` **无过滤且 `@csrf_exempt`**（`views.py:103-110`），
  C 可直调 booking 写操作；FastAPI 侧同构（`app/service.py:192-208`）。
- **修复**：不改代码（2026-08-28 决策）；本文档即标注载体，另在两个无过滤视图
  顶部加 `# WARNING: 无 readonly 过滤，booking 等写工具可被直调（见 .../R1）`
  注释。未来收紧属 breaking change，须先与 C 确认依赖。
- **验收**：注释合入；无行为变化。

### R2 `tool_introduction.md` 与代码脱节

- **现象**：hotel 工具零章节（RollingGo 未提及；详情散落 `hotel_tool.md` 等
  三份独立文档）；web_fetch/web_search 零章节；map 缺 `batch_route` 与城际
  字段、scenic 缺 `action/limit`、food 缺 `city/limit/location`；架构图/文件
  结构/双版本覆盖面表述停在 train 加入前。
- **修复**：补 hotel 章节（正文摘要 + 链接三份既有文档）、web 两工具章节；
  更新 map/scenic/food 参数与返回表；架构图与文件结构补
  hotel_tool/rollinggo_client/web_*/train/。
- **验收**：逐工具对照 input_schema 校对一遍（可让 C5 的校验器输出 schema 清单
  辅助核对）。

### R3 smoke 不覆盖 train / web

- **现象**：`django_server/smoke/smoke_acceptance.py` 无任何 train 端点/工具
  用例，web 工具亦无。
- **修复**：清单 1 增补：`POST /api/tools/train_ticket/invoke`（Mock 假数据下
  断言 status=ok 且含 code/depart_time）；web_search 同理。
- **验收**：容器内冒烟脚本本地跑通。

### R4 `USE_LIVE_DATA` 缺席 `.env.example`

- **现象**：开关只在 `deploy.yml:68-71` 出现，本地模板无此变量说明。
- **修复**：`.env.example` 运行模式节补 `USE_LIVE_DATA=` 与注释（真源数据接入
  总开关，作用于 A 侧规划；默认跟随 DEMO_MODE）。
- **验收**：新环境按模板配置即可复现服务器行为。

### R5 环境依赖缺失导致测试假失败（rapidfuzz 教训）

- **现象**：A 侧可选依赖（`rapidfuzz`/`openai`，requirements.txt 已声明）缺失时，
  planner hook 全部返回空时间轴，测试呈现为"断言 days 非空失败"，排查成本高
  （2026-08-28 实际发生，曾误判为代码回归）。
- **修复**：① README 测试节加显式提醒"先 `pip install -r requirements.txt`"；
  ② `tests/conftest.py` 增加 import 探测：缺 rapidfuzz 时对相关测试模块
  `pytest.skip(allow_module_level=True, reason="缺 rapidfuzz，请安装 requirements.txt")`，
  让假失败变成显式跳过。
- **验收**：卸载 rapidfuzz 跑测试 → 显示 skipped 而非 failed。

---

## 5. 修复批次顺序

按依赖关系排四批，批内条目相互独立、可并行：

| 批次 | 条目 | 前置 | 预估 | 备注 |
|---|---|---|---|---|
| 批次 1（立即可做） | C1 C2 C3 C4、C7（文档对齐）、E3、E5、E8、R2 R3 R4 R5、A1、A3（埋点） | 无 | ~1 天 | 全部无行为风险或纯增量 |
| 批次 2（校验上线） | C6 → **C5**（先修 schema 再开校验） | 批次 1 的 C 系基础 | ~0.5 天 | C5 上线前跑全量回归 |
| 批次 3（需 C 确认） | E2；E1（交互简单可提前） | E3（枚举统一后动作类型才稳定） | ~0.5–1 天 | approve 语义变化须 C 确认 |
| 批次 4（行为增强） | E4、E6、E7、R1（注释） | 批次 2（校验不误伤新分支） | ~1 天 | E7 是设计文档 lodging/train_trip 的前置体验版 |

**总量约 3 天**。全部完成后即为设计文档声明的"基线"：设计文档各项可零返工开工
（接口对照见各条"修复措施"中"独立函数"说明与下节移交清单）。

---

## 6. 移交清单（归设计文档解决）

以下问题**不在本文档修**——它们的正解是设计文档的某项设计，在本文档层面修
属于修两遍：

| # | 问题 | 为什么不在这修 | 归属 |
|---|------|----------------|------|
| 1 | 城际交通 map 估算表 hack（`map_tool.py:206-237` + `b_planner_hook.py:286-292`） | 正解是 train_trip 技能供数，估算表保留为技能回退 | 设计文档 §3 |
| 2 | normalize 别名链治本（A3 只做埋点缓解） | 治本 = output_schema 强契约 + 技能输出即意图级结构 | 设计文档 §2/§3 |
| 3 | LLM tools 参数启用（A2 只标注） | 需要转换器 + 回路 + 降级整套，且依赖 C5 校验与白名单分层 | 设计文档 §6 |
| 4 | readonly 布尔 → safety 权限轴、LLM/C/内部三份白名单分层 | 是元数据框架变更 | 设计文档 §2 |
| 5 | `_capture_hotel_data` 特例缓存 | 泛化为领域缓存钩子属框架变更 | 设计文档 §4 |
| 6 | R1 双入口收紧 | breaking change，已决策不动 | 维持标注 |
