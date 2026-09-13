# B 侧工作进展简报

> 角色：人物 B（系统负责人 · 工具与执行）
> 更新日期：2026-08-06
> 详细报告见 `人物B工作报告.md`，契约对齐见 `data_structure_alignment.md`

---

## 一、已完成 ✅（M1–M4 全部完成）

| 阶段 | 内容 | 状态 |
| --- | --- | --- |
| M1 | 工具层 + Mock 跑通 | ✅ |
| M2 | Scheduler + Execution 跑通 Demo 剧情 | ✅ |
| M3 | Booking / Calendar + 契约联调（骨架） | ✅ |
| M3.5 | 真实 API 接入 | ✅ |
| M4 | Demo 混合模式闭环（真实 API + 模拟突发事件） | ✅ |

**核心产出**：

- **契约层**：`core/schemas.py` — 全项目共享 JSON 接口契约（A/B/C 对齐锚点）
- **工具层**：`tools/` — `BaseTool` 统一抽象 + 9 个 Tool（Mock/Live 双版本，环境变量自动切换）
- **执行核心**：`monitor/monitor_scheduler.py`（定时轮询）+ `execution/execution_agent.py`（影响判定 → `DecisionRequest`）
- **预约/导出**：`booking/booking_manager.py`（预约状态机，只准备不付款）+ `itinerary/`（.ics / Markdown 导出）
- **真实 API**：高德（地图/交通/景点/餐饮，POI 已升级 v5）、和风天气（含 v1 迁移）
- **测试**：125 个单元测试全部通过；Demo 混合模式闭环脚本

---

## 二、待办 ⏳

- **M1 与 A 联调**：确认 `DecisionRequest` 契约，A 消费后返回 `ReplanRequest`
- **M2 与 C 联调**：确认 `ActionItem` 契约，C 消费 B 产出的动作
- **M5 生产化**：调度器容错重试、超时、日志落盘、配置热更新

---

## 三、最新进展：三方数据结构对齐（2026-08-06）

以 `data_structure_alignment.md` 为协商稿，系统化梳理 A/C 差异：

- **8 个与 A/C 协商项**（B 无法单独解决）：
  - 🔴 P0：① 决策流程步骤数不同（B 2 步 vs A/C 4 步 → 方案：A 的 `decision_hook` 直接返回完整 `ReplanRequest`）；② `TripTimeline` 必须携带完整 `Place` 对象（B 工具只认地名/坐标，不接受 `spot_id`）
  - 🟡 P1–P3：异步支持、`id`/`name` 并存、`items`/`arrival` 键名、事件粒度、`permission`/`status` 枚举映射
- **6 个 B 内部调整项**（可自行实施，加可选字段不破坏现有逻辑）：
  - `Place` 加 `id` / `end_time`
  - `MonitorEvent` 加 `spot_id`
  - `ActionItem` 加 `type` / `date` / `quantity`
  - `ReplanRequest` 加 `need_replan` / `impact` / `affected_spots`
  - `TripTimeline` 加 `id` / `total_cost` / `walking_distance`
- 附**完整字段对照表**（Place / MonitorEvent / ReplanRequest / ActionItem / TripTimeline 五张表），作为 A/C 联调的谈判基线

---

## 四、一句话总结

骨架与真实 API 已全部落地（M1–M4 完成），当前重心转向**三方契约对齐**，以 `data_structure_alignment.md` 为协商稿推进 M1/M2 的 A/C 联调。
