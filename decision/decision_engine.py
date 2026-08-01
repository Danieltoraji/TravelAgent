"""Decision Engine stub：影响评分 + Replan 决策。

对应《任务整理.md》第五节 Decision Engine：
  - 收到 MonitorEvent 后先评估影响大小，而不是直接重规划；
  - 影响评分：天气 40 / 排队 80 / 交通 20 / 预算 5；
  - 超过阈值才 Replan，否则忽略。

本 stub 让 B 侧能独立跑通完整闭环，A 可后续替换为 LLM 驱动的版本。
"""

from __future__ import annotations

import copy
import logging
from typing import List

from core.schemas import (
    DecisionRequest,
    EventType,
    MonitorEvent,
    Place,
    ReplanRequest,
    TripTimeline,
)

logger = logging.getLogger("decision")


# 影响评分表（对应 任务整理.md 第五节）
IMPACT_SCORES: dict[EventType, int] = {
    EventType.WEATHER: 40,
    EventType.SCENIC: 80,      # 排队
    EventType.TRAFFIC: 20,
    EventType.FOOD: 5,         # 餐饮归到预算类
}


class DecisionEngine:
    """可注入 ExecutionAgent.decision_hook 的决策引擎 stub。

    用法:
        engine = DecisionEngine(impact_threshold=50)
        agent = ExecutionAgent(timeline, decision_hook=engine)
    """

    def __init__(self, impact_threshold: float = 50.0) -> None:
        self.impact_threshold = impact_threshold
        self.history: list[ReplanRequest | None] = []  # 决策历史（供展示/调试）

    def __call__(self, req: DecisionRequest) -> ReplanRequest | None:
        """作为 decision_hook 被调用。返回 ReplanRequest 或 None。"""
        total = self._score(req.events)
        logger.info("DecisionEngine: 事件 %d 个, 总分 %d, 阈值 %d",
                    len(req.events), total, self.impact_threshold)

        if total < self.impact_threshold:
            # 影响可忽略，不重规划
            self.history.append(None)
            return None

        # 需要重规划 —— 生成新时间轴
        replan = self._replan(req)
        self.history.append(replan)
        return replan

    def _score(self, events: List[MonitorEvent]) -> int:
        """计算总影响分：遍历 events，按 IMPACT_SCORES 查表累加。"""
        total = 0
        for ev in events:
            score = IMPACT_SCORES.get(ev.event_type, 0)
            total += score
        return total

    def _replan(self, req: DecisionRequest) -> ReplanRequest:
        """生成重规划方案（stub 版：简单调整，不调 LLM）。

        策略:
          - 找到触发 Replan 的那个 event（影响分最高的）；
          - 景点排队过高 → 把该景点挪到下午；
          - 天气暴雨 → 给所有户外景点加备注，建议室内活动；
          - 生成 diff_summary 说明改了什么（Explainable）。
        """
        timeline = copy.deepcopy(req.current_timeline)
        reason = ""
        diff: list[str] = []

        # 找影响最高的 event
        top_event = max(req.events, key=lambda e: IMPACT_SCORES.get(e.event_type, 0))
        data = top_event.data or {}

        if top_event.event_type == EventType.SCENIC:
            # 景点排队过高 → 挪到下午
            place_name = top_event.place
            queue_min = int(data.get("queue_min", 0))
            reason = f"{place_name}预计排队 {queue_min} 分钟，影响过大"
            self._move_to_afternoon(timeline, place_name)
            diff.append(f"将 {place_name} 调整至下午，避开上午排队高峰")

        elif top_event.event_type == EventType.WEATHER:
            # 天气暴雨 → 建议室内活动
            condition = data.get("condition", "恶劣天气")
            rain_prob = int(data.get("rain_probability", 0))
            reason = f"天气{condition}（降雨概率 {rain_prob}%），户外活动受影响"
            self._mark_outdoor_note(timeline, f"因{condition}建议改为室内活动")
            diff.append(f"因{condition}，户外景点建议改为室内备选方案")

        elif top_event.event_type == EventType.TRAFFIC:
            delay = int(data.get("delay_min", 0))
            reason = f"交通延误 {delay} 分钟"
            diff.append("已记录交通延误，行程时间顺延")

        else:
            reason = "综合影响超过阈值"
            diff.append("行程已调整")

        return ReplanRequest(
            new_timeline=timeline,
            reason=reason,
            diff_summary=diff,
        )

    @staticmethod
    def _move_to_afternoon(timeline: TripTimeline, place_name: str) -> bool:
        """把指定景点挪到下午（14:00）。返回是否找到并修改。"""
        for day in timeline.days:
            for item in day.items:
                if item.name == place_name and item.category == "scenic":
                    item.arrival = "14:00"
                    return True
        return False

    @staticmethod
    def _mark_outdoor_note(timeline: TripTimeline, note: str) -> None:
        """给所有户外景点（scenic）的 open_time 追加备注。"""
        for day in timeline.days:
            for item in day.items:
                if item.category == "scenic":
                    item.open_time = f"{item.open_time}（{note}）"
