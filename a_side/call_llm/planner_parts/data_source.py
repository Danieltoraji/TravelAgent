"""P4 拆分：数据源解析器（DataSourceResolver）。

自 ``b_planner_hook.BPlannerHook`` 拆出（BPlannerHook 拆分后只剩编排）：
- 真源优先 + 失败回退假数据的三态判定（原 ``_generate_live_or_fallback``）
- 假数据（或回退）管线（原 ``_run_pipeline``）
- 候选池宽度 / 必去景点名（原 ``_pool_days_limit`` / ``_must_visit_names``）

**类身份约定**：``DataSourceResolver`` 是 mixin，方法签名与原 BPlannerHook
私有方法完全一致，经继承保留在 ``BPlannerHook`` 实例上（测试零改动）。
依赖宿主编排方法/属性：``_spots_provider`` / ``_live_spots_provider`` /
``_travel_time_provider`` / ``_planner`` / ``_empty_timeline`` /
``_inject_trip_segments`` / ``_attach_hotels`` / ``_build_trip_segments`` /
``_live_hotel_pool`` / ``_live_plan_with_restaurants`` / ``city`` /
``requirement`` / ``plan_id`` / ``start_date`` / ``_tool_provider`` /
``_live_spots_source``。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import logging

logger = logging.getLogger("call_llm.planner_parts.data_source")

from core.schemas import TripTimeline  # noqa: E402
from data_transmission.b_contract import (  # noqa: E402
    _as_date,
    plan_to_trip_timeline,
)
from data_transmission.enums import PipelineSource  # noqa: E402

from call_llm.planner_parts.trip_segments import (  # noqa: E402
    _first_day_start_from_segments,
    _rebuild_return_with_schedule,
    _windowed_last_day_end,
)


class DataSourceResolver:
    """数据源三态解析（mixin）：live / fake / live_fallback。"""

    def _pool_days_limit(self) -> int:
        """候选池宽度（8.30 扩容）：随行程天数联动 ``max(10, days×5)``。

        2 天行程 10 家（历史行为不变），4 天 20，7 天 35（翻页取整）。
        天数解析失败回退 10——池子宁窄不炸。
        """
        try:
            days = int((self.requirement.get("content") or {}).get("days") or 2)
        except (TypeError, ValueError):
            days = 2
        return max(10, days * 5)

    def _must_visit_names(self) -> List[str]:
        """必去景点名（constraints + preferences 两处取并集，去重保序）。

        传给 B 侧 scenic 工具的 ``ensure_spots``：搜索结果未覆盖的必去景点
        逐个精确查找强拉入库（demo1 教训：七彩丹霞排名 38、山丹马场关键词
        召回死角——必去是硬约束，不依赖搜索排名）。
        """
        content = self.requirement.get("content") or {}
        names: List[str] = []
        for holder in (
            (content.get("constraints") or {}).get("must_visit"),
            (content.get("preferences") or {}).get("must_visit"),
        ):
            for name in holder or []:
                name = str(name).strip()
                if name and name not in names:
                    names.append(name)
        return names

    def _search_plan_once(self, city: str) -> Optional[Dict[str, Any]]:
        """9.2 十二节 A：LLM 定制候选池搜索计划（惰性生成一次并缓存）。

        - gate 关 / 无 planner → None（B 侧 scenic 走内置固定词表，零回归）；
        - 成功 → ``{"buckets": [...]}``；LLM 失败 / 校验不过 → planner 内部
          返回 None（同样回退固定词表），此处只做一次尝试不重试。
        """
        planner = getattr(self, "_search_planner", None)
        if planner is None or getattr(self, "_search_plan_tried", False):
            return getattr(self, "_search_plan", None)
        self._search_plan_tried = True
        content = self.requirement.get("content") or {}
        try:
            plan = planner.plan_for(
                str(city or ""),
                days=int(content.get("days") or 2),
                preferred_tags=(content.get("preferences") or {}).get(
                    "preferred_tags"
                ),
                must_visit=self._must_visit_names(),
            )
        except Exception as exc:  # noqa: BLE001  计划失败不阻断候选池
            logger.warning("search_plan 生成失败（%s）：%s", city, exc)
            plan = None
        self._search_plan = plan
        return plan

    def _generate_live_or_fallback(self) -> TripTimeline:
        """真源优先：候选池 / 规划任一步失败 → 回退假数据管线（保留失败原因）。

        8.30 矩阵瘦身（两阶段，替代先前 36 节点一次性整矩阵）：
        - 阶段 1：一次 ``batch_route`` 只含「候选景点 + 假池酒店」（不再预置 20 家
          餐厅）→ 排一版**无餐厅**计划，确定每天用餐窗口临近的景点（锚点）；
        - 阶段 2：只对「计划内景点 × 真源餐厅」补一张正交小矩阵（远小于全集合
          n²），合并后带餐厅重新规划；餐厅 / 增量矩阵失败 → 沿用阶段 1 计划
          （无餐厅段），不拖累 scenic / 酒店真源链路。
        """
        from data_transmission.live_data import _coord_str, make_live_matrix_fn

        try:
            spots = self._live_spots_provider(self.city)
        except Exception as exc:  # noqa: BLE001
            reason = f"真实数据接入失败，已回退假数据：{exc}"
            timeline = self._run_pipeline(
                self._spots_provider, None, source=PipelineSource.FAKE.value
            )
            self.last_data_source = PipelineSource.LIVE_FALLBACK.value
            self.last_error = reason
            return timeline
        # 到达日重叠修复（方案 A）：规划前构建城际段**一次**（不重复查询
        # 12306/juhe），取去程到达时刻 + 90min 接驳缓冲 → 首日起点；规划后
        # 仅 ``_inject_trip_segments`` 写入。spots 失败早已回退，此处构建
        # 只在真源规划路径执行。
        segments = self._build_trip_segments()
        first_day_start_time = _first_day_start_from_segments(segments)
        # 末日截止优先真源离散班次（最晚可行班出发 − 缓冲），无候选走反推兜底
        last_day_end_minutes = _windowed_last_day_end(segments, self.requirement)
        base_matrix: Dict[Tuple[str, str], Tuple[float, int]] = {}
        live_hotels = self._live_hotel_pool()  # 8.29：假池酒店候选（坐标并入矩阵 → 通勤真源）
        try:
            if self._travel_time_provider is not None:
                self._travel_time_provider.set_name_map(
                    self._live_spots_source.names
                )
                # 阶段 1：scenic 已返回真实坐标 → 一次 batch_route 取候选+酒店矩阵。
                # 坐标直连（B 侧跳过地理编码）：消灭 QPS 突刺（10021）与怪名 POI
                # 编码失败（30001）；矩阵构建失败与规划失败同走回退假源。
                source_spots = self._live_spots_source.spots or self._live_spots_source(
                    self.city,
                    limit=max(10, self._pool_days_limit()),
                    ensure_spots=self._must_visit_names(),
                )
                name_to_coord = {
                    spot["name"]: coord
                    for spot in source_spots
                    if spot.get("name") and (coord := _coord_str(spot.get("location")))
                }
                # 8.29 酒店通勤真源化：假池酒店候选坐标并入矩阵（B4 HotelTool
                # 就绪前酒店本体仍是候选源；通勤先真源化）→ HotelSelector 走矩阵分钟。
                for hotel in live_hotels:
                    coord = _coord_str(
                        {"lat": hotel.location[0], "lng": hotel.location[1]}
                    )
                    if coord and hotel.name not in name_to_coord:
                        name_to_coord[hotel.name] = coord
                if name_to_coord:
                    base_matrix = make_live_matrix_fn(
                        self._tool_provider, city=self.city
                    )(name_to_coord)
                    self._travel_time_provider.set_matrix(
                        base_matrix, name_to_coord=name_to_coord
                    )
                    # 酒店 id → 点名（与 scenic 增量合并，set_name_map 为 update 语义）
                    self._travel_time_provider.set_name_map(
                        {hotel.id: hotel.name for hotel in live_hotels}
                    )
                # 阶段 1 规划：restaurants=None → meal 段抽象无餐厅（plan_multi_day
                # 不自行拉假池餐厅），只用于确定用餐锚点 + 计划内景点集合。
                plan1 = self._planner(
                    self.requirement,
                    spots,
                    travel_time_provider=self._travel_time_provider,
                    restaurants=None,
                    first_day_start_time=first_day_start_time,
                    last_day_end_minutes=last_day_end_minutes,
                )
                # 阶段 2：锚点确定后，只对候选景点 × 真源餐厅补增量矩阵再重排
                plan = self._live_plan_with_restaurants(
                    plan1, spots, name_to_coord, base_matrix, live_hotels,
                    first_day_start_time=first_day_start_time,
                    last_day_end_minutes=last_day_end_minutes,
                )
            else:
                plan = self._planner(
                    self.requirement, spots,
                    first_day_start_time=first_day_start_time,
                    last_day_end_minutes=last_day_end_minutes,
                )
        except Exception as exc:  # noqa: BLE001
            reason = f"真实数据接入失败，已回退假数据：{exc}"
            timeline = self._run_pipeline(
                self._spots_provider, None, source=PipelineSource.FAKE.value
            )
            self.last_data_source = PipelineSource.LIVE_FALLBACK.value
            self.last_error = reason
            return timeline
        if not isinstance(plan, dict) or not plan.get("days"):
            reason = "真实数据接入失败（规划未产出可用计划），已回退假数据"
            timeline = self._run_pipeline(
                self._spots_provider, None, source=PipelineSource.FAKE.value
            )
            self.last_data_source = PipelineSource.LIVE_FALLBACK.value
            self.last_error = reason
            return timeline

        self._current_plan = plan
        self.last_error = None
        self.last_data_source = PipelineSource.LIVE.value
        # 末日行程排定后按实际离开时间重选返程班次（无候选保留反推占位）
        self._inject_trip_segments(
            plan, _rebuild_return_with_schedule(plan, segments, self.requirement)
        )
        self._attach_hotels(plan)
        # 城际两阶段·阶段2（十三节）：酒店已知后站对精修（到达站重选 +
        # 尾/首腿实测填充）；无酒店坐标/无工具时原段返回
        self._refine_intercity_stations(plan)
        timeline = plan_to_trip_timeline(
            plan,
            city=self.city,
            start_date=self.start_date,
            plan_id=self.plan_id,
        )
        self._current_timeline = timeline
        return timeline

    def _run_pipeline(
        self,
        spots_provider: Callable[[str], Any],
        travel_time_provider: Any,
        source: str,
    ) -> TripTimeline:
        """假数据（或回退）管线：候选池 → 规划 → 时间轴；失败降级为空时间轴。"""
        # 1) 候选池
        try:
            spots = spots_provider(self.city)
        except Exception as exc:  # noqa: BLE001  失败降级为空时间轴
            self.last_error = f"候选池加载失败：{exc}"
            timeline = self._empty_timeline()
            self._current_timeline = timeline
            return timeline

        # 2) 规划
        # 到达日重叠修复（方案 A）+ 两段式完整化（离开缓冲 60min）：规划前
        # 构建城际段一次（本地 options 表或不联网 provider），取去程到达时刻
        # + 90min 接驳 → 首日起点、返程出发 − 60min → 末日截止。
        segments = self._build_trip_segments()
        first_day_start_time = _first_day_start_from_segments(segments)
        last_day_end_minutes = _windowed_last_day_end(segments, self.requirement)
        try:
            plan = self._planner(
                self.requirement,
                spots,
                travel_time_provider=travel_time_provider,
                first_day_start_time=first_day_start_time,
                last_day_end_minutes=last_day_end_minutes,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"规划失败：{exc}"
            timeline = self._empty_timeline()
            self._current_timeline = timeline
            return timeline
        if not isinstance(plan, dict) or not plan.get("days"):
            self.last_error = "规划未产出可用计划"
            timeline = self._empty_timeline()
            self._current_timeline = timeline
            return timeline

        self._current_plan = plan
        self.last_error = None
        self.last_data_source = source
        self._inject_trip_segments(
            plan, _rebuild_return_with_schedule(plan, segments, self.requirement)
        )
        self._attach_hotels(plan)
        # 城际两阶段·阶段2（十三节）：酒店已知后站对精修（到达站重选 +
        # 尾/首腿实测填充）；无酒店坐标/无工具时原段返回
        self._refine_intercity_stations(plan)
        timeline = plan_to_trip_timeline(
            plan,
            city=self.city,
            start_date=self.start_date,
            plan_id=self.plan_id,
        )
        self._current_timeline = timeline
        return timeline