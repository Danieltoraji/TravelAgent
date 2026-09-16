"""LLM 正餐过滤（2026-09-15，用户拍板：餐厅选择用 LLM 兜底）。

关键词黑名单（adapters.is_non_meal_name）只能挡枚举过的品牌/品类——
"航天馒头""某某私房菜"这类长尾无法穷举，正餐判断本质是语义理解，交给 LLM。

设计：
- **批量**：一次规划的所有候选餐厅名去重后**单次** LLM 调用判「哪些不适合
  作为旅行正餐」，结果按名称缓存（同锚点多顿复用不再调用）；
- **门控**：``USE_LLM_TOOLS``（与搜索计划/决策回路同开关）；
- **失败不阻断**：LLM 失败 → 原样返回（兜底失效 ≠ 没饭吃）；全排除 → 放宽
  保留原池（有得吃总比没饭强，与关键词黑名单同哲学）；
- **注入式**：返回过滤函数挂到 ``RestaurantResolver(meal_filter_fn=...)``，
  transport 层保持无 LLM 依赖。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("call_llm.meal_filter")

from call_llm.client_factory import create_llm_client  # noqa: E402  模块级：便于测试 patch

MEAL_FILTER_SYSTEM = (
    "你是旅行规划助手，负责筛选适合放进旅行行程的**正餐餐厅**（午餐/晚餐）。\n"
    "给你一批候选餐厅名，请找出**不适合作为旅行正餐**的项：\n"
    "- 咖啡店/奶茶/茶饮/甜品/烘焙糕点；\n"
    "- 便利店/超市/小卖部/零食零食铺；\n"
    "- 快餐盒饭/馒头包子铺/纯外卖店；\n"
    "- 其它明显不适合坐下来吃一顿正餐的（酒吧/网吧等）。\n"
    "保留：面馆/锅贴/本地菜馆/茶餐厅/特色小吃店等**能吃一顿正餐**的。\n"
    "只输出 JSON：{exclude: [不适合的餐厅名列表]}，没有则输出空列表。"
)

MEAL_FILTER_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "不适合作为旅行正餐的餐厅名列表",
        },
    },
    "required": ["exclude"],
}


def build_llm_meal_filter(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Optional[Callable[[List[Any]], List[Any]]]:
    """构造 ``meal_filter_fn(candidates) -> candidates``；门控关/建客户端失败 → None。

    ``candidates`` 为 ``Restaurant`` 列表（只用 ``.name``）；LLM 判定按名称
    缓存（单次规划去重后通常只有一次真实调用）。``USE_LLM_TOOLS`` 关 → None。
    """
    import os

    if os.environ.get("USE_LLM_TOOLS", "").strip().lower() not in ("1", "true", "yes"):
        return None
    try:
        client = create_llm_client(
            model_name=model_name,
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            ask_user_if_missing=False,
            system_instruction=MEAL_FILTER_SYSTEM,
            max_tokens=800,
        )
    except Exception as exc:  # noqa: BLE001  无 key/配置坏 → 兜底失效不阻断
        logger.warning("LLM 正餐过滤客户端创建失败，过滤不启用：%s", exc)
        return None

    excluded: Dict[str, bool] = {}   # name → 是否被 LLM 判为非正餐

    def meal_filter(candidates: List[Any]) -> List[Any]:
        names = [getattr(r, "name", "") for r in candidates]
        unknown = [n for n in names if n and n not in excluded]
        if unknown:
            listing = "\n".join(f"- {n}" for n in unknown)
            try:
                result = client.generate(
                    messages=[{
                        "role": "user",
                        "content": f"候选餐厅名单：\n{listing}\n请按规则筛选。",
                    }],
                    response_schema=MEAL_FILTER_SCHEMA,
                )
                for name in (result.get("content") or {}).get("exclude") or []:
                    excluded[str(name).strip()] = True
                for n in unknown:
                    excluded.setdefault(n, False)
            except Exception as exc:  # noqa: BLE001  LLM 失败 → 不过滤（不阻断）
                logger.warning("LLM 正餐过滤失败，保留原候选：%s", exc)
                return candidates
        kept = [r for r in candidates if not excluded.get(getattr(r, "name", ""), False)]
        # 全排除 → 放宽保留原池（有得吃总比没饭强，与关键词黑名单同哲学）
        return kept or candidates

    return meal_filter
