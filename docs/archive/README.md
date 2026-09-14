# docs/archive — 已归档历史文档

> 2026-09-13 归档。**只有代码是可信的**：以下文档是特定时点的快照/报告，其中描述的
> 架构（单用户单例、app/service.py 服务层、决策 stub 等）已被后续演进推翻，
> 不再反映仓库现状。现状请看 [../../README.md](../../README.md) 与 docs/ 下的现行文档。

| 文档 | 时点 | 归档原因 |
| --- | --- | --- |
| 任务整理.md | 2026-07-29 | 最初的项目需求/分工整理，早期版本 |
| 人物B工作报告.md | 2026-07-31 | 早期里程碑报告（174 测试时代），已被实际进展覆盖 |
| B侧工作进展简报.md | 2026-08-06 | 早期进展简报，同上 |
| data_requirements.md | 2026-08-06 | 最初需求 schema 草案；现行契约以 `core/schemas.py` 与 `/api/plan/` 实际请求体为准 |
| data_structure_alignment.md | 2026-08-06 | 三方对齐协商快照（基线还是 app/service.py 时代）；契约早已演进 |
| 交付文档.md | 2026-08-21 | 旧交付口径（FastAPI 服务层）；现行交付面是 `django_server`，A/C 接入见 sync_notes 系列 |
| code_defects_and_fixes_20260828.md | 2026-08-28 | 带日期的点状缺陷修复快照，修复均已落地 |
| tool_encapsulation_design_20260828.md | 2026-08-28 | P0–P5 封装方案设计稿，方案已全部实施完毕（实施状态见文内批注） |
| sync_notes_pr3_5_for_ac_20260829.md | 2026-08-29 | 带日期的 A/C 同步记录，被后续 sync notes 取代 |
| sync_notes_p3p4_flight_for_ac_20260829.md | 2026-08-29 | 同上（P3/P4 + 航班真源接入记录） |

现行（未归档）文档：`tool_introduction.md`（工具层参考）、`chat_api.md`（C 端对话）、
`transport_contract.md`（交通段契约）、`hotel_tool.md` / `A_hotel_tool_adapter.md` /
`C_hotel_data.md`（酒店）、`demo_event_injection.md` / `event_injection_cookbook.md`
（演示注入）、`sync_notes_multiuser_for_ac_20260912.md`（多用户改造接入，最新）。
