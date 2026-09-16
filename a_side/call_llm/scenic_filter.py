"""LLM 候选池审核（2026-09-16，用户设计：选完交大模型审核，过了加时间字段，
没过重选——如此迭代）。

背景：襄阳实测「古隆中游客中心」与「古隆中」同时入池；餐厅侧同款长尾
（咖啡店/蜜雪冰城）。非景点/低质 POI 的识别是语义问题，枚举不完，交 LLM
审核——一次调用同时输出：verdict（池整体是否合格）/ exclude（剔除名单：
非景点、低质、与主题不符）/ more_keywords（fail 时的补搜建议词）。

设计（与 ``meal_filter``/``duration_estimator`` 同款 builder 模式）：
- **门控**：``USE_LLM_TOOLS``；**失败不阻断**：审核失败 → 原池返回；
- **迭代**：调用方（b_planner_hook）最多 2 轮——fail → 剔除 + 按建议词补搜
  → 再审；pass 或轮次耗尽 → 交时长估算。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("call_llm.scenic_filter")

from call_llm.client_factory import create_llm_client  # noqa: E402  模块级：测试 patch

POOL_REVIEW_SYSTEM = (
    "你是旅行规划助手，负责审核候选景点池的质量。\n"
    "给你一批候选 POI 名单（某目的地的景点搜索召回结果），请审核：\n"
    "1. 剔除**不值得排入行程的项**：景区服务设施（游客中心/游客服务部/售票处/"
    "检票口/停车场/索道站/码头）、商业设施（纪念品店/特产商店/商铺）、"
    "交通设施（客运站/高铁站/加油站）、重复条目（同一景点只保留一个名字规范的）；\n"
    "2. 判断整池是否合格（verdict）：非景点/重复项占比高、或与用户偏好主题"
    "明显不符 → fail；基本干净 → pass；\n"
    "3. fail 时给出 more_keywords（3~5 个**本地可搜的补充搜索词**，用于补召回"
    "更符合主题的景点；pass 时给空列表）。\n"
    "只输出 JSON：{verdict: pass|fail, exclude: [剔除名列表], "
    "more_keywords: [词], reason: 一句理由}。"
)

POOL_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "剔除名列表（非景点/重复/低质）",
        },
        "more_keywords": {
            "type": "array",
            "items": {"type": "string"},
            "description": "fail 时的补搜建议词",
        },
        "reason": {"type": "string"},
    },
    "required": ["verdict", "exclude", "more_keywords", "reason"],
}


def build_llm_pool_review(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Optional[Callable[[List[Dict[str, Any]]], Tuple[List[Dict[str, Any]], Dict[str, Any]]]]:
    """构造 ``pool_review_fn(pool) -> (cleaned_pool, meta)``。

    门控关/客户端失败 → None（调用方跳过审核，行为与既往一致）。
    ``meta`` = {verdict, exclude, more_keywords, reason}。
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
            system_instruction=POOL_REVIEW_SYSTEM,
            max_tokens=800,
        )
    except Exception as exc:  # noqa: BLE001  配置坏 → 不启用
        logger.warning("LLM 池审核客户端创建失败，不启用：%s", exc)
        return None

    def pool_review(
        pool: List[Dict[str, Any]],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        names = [
            str(s.get("name") or "").strip()
            for s in (pool or []) if isinstance(s, dict)
        ]
        names = [n for n in names if n]
        if not names:
            return (pool or []), {"verdict": "pass", "exclude": [],
                                  "more_keywords": [], "reason": "空池跳过审核"}
        listing = "\n".join(f"- {n}" for n in names)
        try:
            result = client.generate(
                messages=[{
                    "role": "user",
                    "content": (
                        f"候选 POI 名单（{len(names)} 个）：\n{listing}\n请审核。"
                    ),
                }],
                response_schema=POOL_REVIEW_SCHEMA,
            )
            meta = result.get("content") or {}
        except Exception as exc:  # noqa: BLE001  审核失败 → 原池（不丢景点）
            logger.warning("LLM 池审核失败，保留原池：%s", exc)
            return (pool or []), {"verdict": "pass", "exclude": [],
                                  "more_keywords": [], "reason": "审核失败跳过"}
        verdict = meta.get("verdict") if meta.get("verdict") in ("pass", "fail") else "pass"
        exclude = {str(n).strip() for n in (meta.get("exclude") or []) if str(n).strip()}
        cleaned = [
            s for s in (pool or [])
            if str(s.get("name") or "").strip() not in exclude
        ]
        meta_out = {
            "verdict": verdict,
            "exclude": sorted(exclude),
            "more_keywords": [str(k) for k in (meta.get("more_keywords") or [])][:5],
            "reason": str(meta.get("reason") or ""),
        }
        # 全被剔除 → 放宽保留原池（评分层还会筛，宁多勿漏）
        return (cleaned or (pool or [])), meta_out

    return pool_review
