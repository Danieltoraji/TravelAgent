# TravelAgent —— 自主旅行管家

帮助用户完成整个旅行生命周期（规划 → 持续监控 → 智能决策 → 安全执行），而不只是生成一份攻略。

## 目录结构

```text
TravelAgent/
├── 人物B工作报告.md            # 人物B（系统负责人）工作报告
├── 任务整理.md                 # 项目需求 / 分工 / 架构文档
├── core/schemas.py             # 全项目共享 JSON 接口契约（A/B/C 对齐锚点）
├── config/settings.py          # 轮询频率、API Key 占位、Demo 开关
├── tools/                      # 工具层：统一抽象 + 6 个领域 Tool（Mock）
│   ├── base_tool.py            #   BaseTool 抽象基类 + ToolRegistry
│   ├── map_tool.py             #   地图（POI 搜索 / 路线）
│   ├── weather_tool.py         #   天气
│   ├── scenic_tool.py          #   景点（开放 / 排队 / 预约）
│   ├── traffic_tool.py         #   交通
│   ├── food_tool.py            #   餐饮
│   ├── booking_tool.py         #   预约（只准备，不付款）
│   └── mock_data.py            #   Mock 数据源 + 剧情模拟
├── monitor/monitor_scheduler.py  # 定时监控调度器
├── execution/execution_agent.py  # 持续监控执行体（项目核心）
├── booking/booking_manager.py    # 预约状态机 + ActionQueue 契约
├── itinerary/                    # .ics 日历 + Markdown 行程单导出
├── app/service.py                # 可选 FastAPI 服务层（供 Web 前端）
├── demo/demo_scenario.py         # 比赛 Demo 剧情脚本
└── tests/                        # 单元测试
```

## 运行

```bash
# 单元测试（核心零依赖，标准库即可）
python -m unittest discover -s tests -v

# Demo 剧情脚本
python -m demo.demo_scenario

# 可选：服务层
pip install -r requirements.txt
uvicorn app.service:app --reload --port 8000
```

## 角色分工

| 成员 | 职责 | 模块 |
| --- | --- | --- |
| A（Agent 负责人） | 智能决策 | Planner、Decision Engine、RePlanner、Memory、Prompt |
| B（系统负责人，本仓库） | 工具与执行 | Tool Agents、API 封装、Monitor Scheduler、Booking、Calendar、Execution Agent |
| C（产品负责人） | 展示与交互 | Web 前端、日志、Action Queue、Permission Manager、Markdown/PDF 导出 |
