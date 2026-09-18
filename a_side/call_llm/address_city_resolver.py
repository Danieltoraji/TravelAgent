"""LLM 出发地/返程地归一 + 目的地性质判定（方案 b v2，2026-09-19）。

用户在 C 端填的出发地/目的地可能是：详细地址（小区/街道/门牌号）、具体
城市、区域/省名（「东北」「云南」「江浙沪」）。``PlaceNormalizer``（城市级
内置 ∪ 站名）不认 → 城际真源全 error → driving 兜底：能出图但不查火车班次。

**v2（R2 治本，用户拍板「区域判定交给 LLM」）**：``classify`` 把「猜主城」
升级为「性质判定二选一」——详细地址给 city；区域/省名给 region + LLM 凭
世界知识自由提名的城市序列（真实世界区名枚举不完，词典只是保底）；真
判断不了才 unknown。区域分支由调用方接入多城管线（region_city_planner
v3 选城 + 逐城验证链 + 剔城兜底幻觉）。``resolve`` 保留（兼容既有调用方
与测试），语义 = classify 的 city 分支。

- **门控**：``USE_LLM_TOOLS``（与池审核/估时/正餐过滤同一门控；关 → 不启用，
  行为与既往一致）；
- **失败不阻断**：LLM 超时/解析失败 → None / unknown（调用方保持原值走
  自驾兜底）；
- **缓存**：同 ``(raw, other_city)`` 一次规划内只调一次 LLM；
- **写回纪律不变**：返回的城市**调用方必须再经 PlaceNormalizer 二次确认**
  （站表全量城市集 430 城兜底后，二次确认只拦真幻觉，不拦真实城市）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("call_llm.address_city_resolver")

ADDRESS_CITY_SYSTEM = (
    "你是旅行规划助手。用户填写的出发地/目的地可能是：详细地址（小区/街道/"
    "写字楼/门牌号）、具体城市、区域或省名（如「东北」「云南」「江浙沪」）、"
    "带省/市前缀的地名。请判断它的性质：\n"
    "- type=city：详细地址或具体城市 → 给出所属**地级市**名（如「天津市"
    "东丽区XX小区」→ 天津；「西安」→ 西安）；\n"
    "- type=region：区域/省名（一名多地，适合一次连游多城）→ 在 cities 里"
    "凭你的地理与旅行知识给出该区域内**值得游览的城市序列**（按合理访问"
    "顺序，3-6 个，不局限于省会大城市；如 云南 → 大理、丽江、昆明、西双"
    "版纳；东北 → 沈阳、长春、哈尔滨、延吉）；\n"
    "- type=unknown：无法判断（国家/国外/文本过短/无地理含义）→ city 与 "
    "cities 都留空。\n"
    "只输出 JSON：{\"type\": \"city|region|unknown\", \"city\": \"地级市名"
    "或空\", \"cities\": [\"城市名\", ...], \"reason\": \"一句理由\"}"
)

ADDRESS_CITY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": ["city", "region", "unknown"]},
        "city": {"type": "string", "description": "type=city 时的地级市名"},
        "cities": {
            "type": "array",
            "items": {"type": "string"},
            "description": "type=region 时的候选城市序列（LLM 自由提名）",
        },
        "reason": {"type": "string"},
    },
    "required": ["type"],
}


def build_address_city_resolver(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 20,
) -> Optional[Callable[..., Any]]:
    """构造 resolver（v2 双入口）。

    - ``resolve(raw_place, other_city) -> Optional[str]``：兼容入口（v1
      语义）——返回归一出的主城名（region/unknown/失败 → None）；
    - ``classify(raw_place, other_city) -> Optional[Dict]``：v2 入口——
      ``{"type": "city|region|unknown", "city": str|None,
      "cities": [str, ...], "reason": str}``（失败 → None）。

    门控关/客户端创建失败 → None（调用方跳过 LLM 归一，行为与既往一致）。
    返回的城市**调用方必须再经 PlaceNormalizer 二次确认后才写回 content**
    （本函数只负责解读地址，不负责验证城市合法性）。
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
            system_instruction=ADDRESS_CITY_SYSTEM,
            max_tokens=500,
        )
    except Exception as exc:  # noqa: BLE001  配置坏 → 不启用
        logger.warning("LLM 地址归一客户端创建失败，不启用：%s", exc)
        return None

    cache: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {}

    def classify(
        raw_place: str, other_city: str, role: str = "destination"
    ) -> Optional[Dict[str, Any]]:
        raw = str(raw_place or "").strip()
        if not raw:
            return None
        key = (raw, str(other_city or "").strip(), str(role or "destination"))
        if key in cache:
            return cache[key]
        is_origin = str(role or "").strip() == "origin"
        try:
            result = client.generate(
                messages=[{
                    "role": "user",
                    "content": (
                        f"该地点是用户的{'出发地' if is_origin else '目的地'}。\n"
                        f"行程另一端：{other_city or '（未填）'}\n"
                        f"待判断的地点：{raw}\n"
                        + (
                            "它是详细地址、具体城市，还是区域/省名？"
                            "注意出发地是单点：若是区域/省名，请给出最适合"
                            "作为跨城交通枢纽的主城（type=city）。"
                            if is_origin else
                            "它是详细地址、具体城市，还是区域/省名？"
                        )
                    ),
                }],
                response_schema=ADDRESS_CITY_SCHEMA,
            )
            meta = result.get("content") or {}
        except Exception as exc:  # noqa: BLE001  失败不阻断
            logger.warning("LLM 地址归一失败（%s）：%s", raw, exc)
            cache[key] = None
            return None
        rtype = str(meta.get("type") or "").strip().lower()
        city = str(meta.get("city") or "").strip() or None
        cities = [
            str(c).strip() for c in (meta.get("cities") or [])
            if str(c or "").strip()
        ]
        if rtype not in ("city", "region", "unknown"):
            # 模型漏给 type → 从 city/cities 推断（兼容 v1 式应答）
            rtype = "city" if city else ("region" if cities else "unknown")
        if rtype == "city" and not city:
            rtype = "unknown"
        if rtype == "region" and not cities:
            rtype = "unknown"
        if rtype == "region" and is_origin:
            # 出发地是单点：区域降级为枢纽主城（首个提名城）
            rtype = "city"
            city = cities[0] if cities else None
            cities = []
        out = {
            "type": rtype,
            "city": city if rtype == "city" else None,
            "cities": cities if rtype == "region" else [],
            "reason": str(meta.get("reason") or "")[:120],
        }
        cache[key] = out
        logger.info(
            "LLM 地点性质判定：%s(%s) → %s（%s）", raw,
            "出发地" if is_origin else "目的地",
            out["type"] + (
                f":{out['city']}" if out["city"]
                else (":" + "、".join(out["cities"][:4]) if out["cities"] else "")
            ),
            out["reason"],
        )
        return out

    def resolve(raw_place: str, other_city: str) -> Optional[str]:
        """v1 兼容入口：city 分支返回城市名，其余 None。"""
        meta = classify(raw_place, other_city)
        if meta is None:
            return None
        return meta.get("city")

    # classify 挂在 resolve 上，调用方按能力探测（getattr）
    resolve.classify = classify  # type: ignore[attr-defined]
    return resolve
