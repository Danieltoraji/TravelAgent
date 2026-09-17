"""区域目的地 → 多城计划决策层（方案 c 阶段 1，2026-09-17）。

destination 为区域名（「东北」）且行程天数足够时，产出 **city_plan**：
按访问序的每城天数分配（``[{city, day_from, day_to}]``）。阶段 1 决策与
执行解耦——city_plan 只记录与告知，执行仍按首城（主城）走方案 b 单城
管线，线上零行为变化；阶段 2+ 起执行层逐城消费。

**规则定界**（不调 LLM 的确定性边界，决策引擎哲学）：
- ``expand_region`` 未命中 → 非区域，None（调用方走方案 b 地址归一）；
- ``days < 3`` → None（行程太短撑不起换城，单主城合理）；
- LLM 产出硬校验：城市 ⊆ 区域城市集、Σ天数 = days、每城 ≥1、城数 ≥2；
  违反 → 重问一次 → 再失败 None（回退方案 b 单主城，不阻断）。

**门控**：``USE_LLM_TOOLS``（与池审核/估时/地址归一同门控）；**失败不阻断**。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("call_llm.region_city_planner")

REGION_CITY_SYSTEM = (
    "你是旅行规划助手。用户以区域名（如「东北」）作为旅行目的地，请把该"
    "区域展开为一次连游的**城市序列**并分配每城天数：\n"
    "- 只从给出的候选城市里选（不要编造区域外城市）；\n"
    "- 顺序符合地理走向与交通现实（相邻城市间有高铁/直达交通）；\n"
    "- Σ每城天数 == 总天数，每城至少 1 天；结合用户偏好把天数多分给"
    "更值得深度游的城市；\n"
    "- 城市数不超过总天数；若区域里只有 1 个城市值得去，也至少给出 2 个\n"
    "  （连游需换城）；\n"
    "只输出 JSON：{\"cities\": [{\"city\": \"城市名\", \"days\": 整数}]}，"
    "按访问顺序排列。"
)

REGION_CITY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "cities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "city": {"type": "string"},
                    "days": {"type": "integer"},
                },
                "required": ["city", "days"],
            },
        },
    },
    "required": ["cities"],
}

_MIN_MULTI_CITY_DAYS = 3      # 短于 3 天不做多城（规则定界）


def _validate_allocation(
    cities: List[Dict[str, Any]],
    region_cities: Tuple[str, ...],
    days: int,
) -> Optional[List[Dict[str, int]]]:
    """LLM 分配 → city_plan（硬校验）；不合格返回 None。"""
    region_set = set(region_cities)
    plan: List[Dict[str, int]] = []
    seen = set()
    total = 0
    for entry in cities:
        city = str((entry or {}).get("city") or "").strip()
        try:
            d = int((entry or {}).get("days") or 0)
        except (TypeError, ValueError):
            return None
        if not city or city in seen or city not in region_set or d < 1:
            return None
        seen.add(city)
        plan.append({"city": city, "day_from": total + 1, "day_to": total + d})
        total += d
    if total != days or len(plan) < 2:
        return None
    return plan


def build_region_city_planner(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Optional[Callable[[str, int, List[str], str], Optional[List[Dict[str, int]]]]]:
    """构造 ``plan(region, days, preferred_tags, origin) -> city_plan | None``。

    city_plan 元素形如 ``{"city": "沈阳", "day_from": 1, "day_to": 2}``
    （按访问序，day 从 1 计）。门控关/客户端创建失败 → None（调用方走
    方案 b 单主城归一）。返回 None = 不做多城（回退单主城，不阻断）。
    """
    import os

    if os.environ.get("USE_LLM_TOOLS", "").strip().lower() not in ("1", "true", "yes"):
        return None
    try:
        from call_llm.client_factory import create_llm_client

        client = create_llm_client(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            ask_user_if_missing=False,
            system_instruction=REGION_CITY_SYSTEM,
            max_tokens=500,
        )
    except Exception as exc:  # noqa: BLE001  配置坏 → 不启用
        logger.warning("LLM 多城计划客户端创建失败，不启用：%s", exc)
        return None

    def plan(
        region: str, days: int, preferred_tags: Optional[List[str]] = None,
        origin: str = "",
    ) -> Optional[List[Dict[str, int]]]:
        from data_transmission.place_normalizer import PlaceNormalizer

        region_cities = PlaceNormalizer().expand_region(region)
        if not region_cities:
            return None                       # 非区域名 → 方案 b 单城归一
        if days < _MIN_MULTI_CITY_DAYS:
            return None                       # 短行程不换城
        listing = "、".join(region_cities)
        prefs = "、".join(preferred_tags or []) or "（无特别偏好）"
        user = (
            f"区域：{region}（候选城市：{listing}）\n"
            f"总天数：{days}\n出发地：{origin or '（未填）'}\n"
            f"用户偏好：{prefs}\n请给出连游城市序列与每城天数。"
        )
        plan: Optional[List[Dict[str, int]]] = None
        for attempt in (1, 2):                # 硬校验不过重问一次
            try:
                result = client.generate(
                    messages=[{"role": "user", "content": user}],
                    response_schema=REGION_CITY_SCHEMA,
                )
                meta = result.get("content") or {}
            except Exception as exc:  # noqa: BLE001  失败不阻断
                logger.warning("LLM 多城计划失败（%s %d天）：%s", region, days, exc)
                return None
            plan = _validate_allocation(
                meta.get("cities") or [], region_cities, days
            )
            if plan is not None:
                logger.info(
                    "LLM 多城计划：%s %d天 → %s", region, days,
                    " → ".join(f"{p['city']}{p['day_to'] - p['day_from'] + 1}天"
                               for p in plan),
                )
                return plan
            user = (
                user + "\n（上次分配未通过校验：Σ天数必须等于总天数、"
                "每城至少 1 天、城市只能来自候选列表，请重新分配。）"
            )
        logger.warning("LLM 多城计划两次校验不过，回退单主城（%s）", region)
        return None

    return plan
