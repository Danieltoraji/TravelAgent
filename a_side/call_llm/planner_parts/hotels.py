"""P4 拆分：住宿附着器（HotelAttacher）。

自 ``b_planner_hook.BPlannerHook`` 拆出（BPlannerHook 拆分后只剩编排）：
- 酒店选择写入计划（原 ``_attach_hotels``）
- 真源酒店池（原 ``_live_hotel_pool`` / ``_load_live_hotels_with_fallback`` /
  ``_live_hotel_provider_or_none``，B4 HotelTool / RollingGo MCP，失败回退假池）

**类身份约定**：``HotelAttacher`` 是 mixin，方法签名与原 BPlannerHook 私有
方法完全一致，经继承保留在 ``BPlannerHook`` 实例上（测试零改动）。
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import logging

logger = logging.getLogger("call_llm.planner_parts.hotels")


class HotelAttacher:
    """住宿附着（mixin）。

    依赖宿主实例属性：``requirement`` / ``_tool_provider`` / ``_use_live`` /
    ``city`` / ``_travel_time_provider``。
    """

    def _attach_hotels(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        """把住宿安排写入计划（``plan["accommodation"]``，与 main.py 口径一致）。

        酒店初始规划接入（8.27，服务器先前不选酒店）：``select_hotels_for_plan``
        按晚数选常驻酒店并做预算 / 通勤校验，产出每晚 bookings + hotel_cost；
        ``plan_to_trip_timeline`` 消费它生成 hotel 段。无目的地 / 无景点 / 无酒店
        数据时返回 None，计划保持无住宿段（不阻断规划）。

        8.30 酒店真源（B4 HotelTool）：真源模式下注入 ``hotel_provider``（RollingGo
        MCP → 真源酒店候选）与 ``travel_time_provider``（矩阵真源分钟）；真源选店
        失败由 ``HotelSelector`` 内部回退假池，不阻断规划。
        """
        try:
            from transport.hotels import select_hotels_for_plan

            # 8.29：真源矩阵模式注入 travel_time_provider → 酒店↔景点通勤走矩阵真源分钟
            # 8.30：hotel_provider = RollingGo 真源酒店（失败回退假池，见 HotelSelector）
            acc = select_hotels_for_plan(
                self.requirement,
                plan,
                hotel_provider=self._live_hotel_provider_or_none(),
                travel_time_provider=self._travel_time_provider,
            )
        except Exception as exc:  # noqa: BLE001  选酒店失败不阻断规划本身
            logger.warning("select_hotels_for_plan failed: %s", exc)
            acc = None
        if acc:
            plan["accommodation"] = acc
        return plan

    def _live_hotel_pool(self) -> List[Any]:
        """真源酒店候选（B4 HotelTool / RollingGo MCP），失败回退假池。

        8.30 酒店真源：``_use_live`` + 注入 ``tool_provider`` 时优先走
        ``make_live_hotel_provider``（真源酒店，含真实坐标/价格）；空池或
        工具异常 → 回退 ``load_hotels`` 假池（不阻断规划）。候选坐标随后
        并入阶段 1 矩阵 → 酒店↔景点通勤真源分钟。
        """
        if getattr(self, "_live_hotel_pool_cache", None) is None:
            self._live_hotel_pool_cache = self._load_live_hotels_with_fallback()
        return self._live_hotel_pool_cache

    def _load_live_hotels_with_fallback(self) -> List[Any]:
        """真源优先取酒店池；空 / 异常 / 未启用 → 假池。"""
        if self._use_live and self._tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                hotels = list(make_live_hotel_provider(self._tool_provider)(self.city))
                if hotels:
                    return hotels
                logger.warning("hotel 工具返回空池（city=%s），回退假池", self.city)
            except Exception as exc:  # noqa: BLE001
                logger.warning("hotel 真源失败，回退假池：%s", exc)
        try:
            from data_transmission.hotel import load_hotels

            return list(load_hotels(self.city))
        except Exception:  # noqa: BLE001
            return []

    def _live_hotel_provider_or_none(self) -> Optional[Any]:
        """真源酒店 provider（供 ``select_hotels_for_plan`` 注入）；未启用 → None。

        仅注入函数本身（不在此调用）；执行失败由 ``HotelSelector`` 内部回退假池。
        """
        if self._use_live and self._tool_provider is not None:
            try:
                from data_transmission.live_data import make_live_hotel_provider

                return make_live_hotel_provider(self._tool_provider)
            except Exception:  # noqa: BLE001
                return None
        return None