"""LLM 出发地/返程地归一（地名映射方案 b 过渡版，2026-09-17）：详细地址 → 主城。

用户在 C 端出发地填小区级地址（「天津东丽区旭茗苑」）→ ``PlaceNormalizer``
（城市级内置 ∪ 站名）不认 → 城际真源全 error → driving 估算兜底：能出图但
不查火车班次（天津→西安的高铁方案完全不会出现在候选里，2026-09-16 实测）。
本模块在归一化未命中时补一层 LLM：详细地址/区域名 → 主城，交回调用方经
``PlaceNormalizer`` 二次确认（**已知城市才写回**——LLM 幻觉天然被拦），
命中即走既有真源班次精排；原文仍留在 ``content.origin_address`` 供
「家→车站」市内腿实测（两份数据各喂各的消费者，口径不变）。

- **门控**：``USE_LLM_TOOLS``（与池审核/估时/正餐过滤同一门控；关 → 不启用，
  行为与既往一致）；
- **失败不阻断**：LLM 超时/解析失败/city 为空 → None（调用方保持原值走
  自驾兜底）；
- **缓存**：同 ``(raw, other_city)`` 一次规划内只调一次 LLM。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, Optional, Tuple

logger = logging.getLogger("call_llm.address_city_resolver")

ADDRESS_CITY_SYSTEM = (
    "你是旅行规划助手。用户填写的出发地/返程地可能是：详细地址（小区/街道/"
    "写字楼/门牌号）、区域或方向名（如「东北」「江浙沪」）、带省/市前缀的"
    "地名。请判断它属于哪个**地级市**（用于查询跨城火车/航班班次）：\n"
    "- 详细地址 → 所属地级市（如「天津市东丽区XX小区」→ 天津；只给市名，"
    "不要编造区县或街道）；\n"
    "- 区域/方向名 → 该区域内最适合作为跨城交通枢纽的主城（如 东北 → 沈阳），"
    "并在 reason 说明；\n"
    "- 已经是城市名 → 原样返回该市；\n"
    "- 无法判断（范围过大无合理主城/国家/国外/文本过短）→ city 返回空字符串。\n"
    "只输出 JSON：{\"city\": \"地级市名或空\", \"reason\": \"一句理由\"}"
)

ADDRESS_CITY_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "city": {"type": "string", "description": "地级市名；无法判断为空串"},
        "reason": {"type": "string"},
    },
    "required": ["city"],
}


def build_address_city_resolver(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 20,
) -> Optional[Callable[[str, str], Optional[str]]]:
    """构造 ``resolve(raw_place, other_city) -> Optional[str]``。

    门控关/客户端创建失败 → None（调用方跳过 LLM 归一，行为与既往一致）。
    返回值为归一出的主城名（未命中/失败 None），**调用方必须再经
    PlaceNormalizer 二次确认后才写回 content**（本函数只负责解读地址，
    不负责验证城市合法性）。
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
            max_tokens=300,
        )
    except Exception as exc:  # noqa: BLE001  配置坏 → 不启用
        logger.warning("LLM 地址归一客户端创建失败，不启用：%s", exc)
        return None

    cache: Dict[Tuple[str, str], Optional[str]] = {}

    def resolve(raw_place: str, other_city: str) -> Optional[str]:
        raw = str(raw_place or "").strip()
        if not raw:
            return None
        key = (raw, str(other_city or "").strip())
        if key in cache:
            return cache[key]
        try:
            result = client.generate(
                messages=[{
                    "role": "user",
                    "content": (
                        f"行程目的地：{other_city or '（未填）'}\n"
                        f"待判断的地点：{raw}\n"
                        "它属于哪个地级市？"
                    ),
                }],
                response_schema=ADDRESS_CITY_SCHEMA,
            )
            meta = result.get("content") or {}
        except Exception as exc:  # noqa: BLE001  失败不阻断
            logger.warning("LLM 地址归一失败（%s）：%s", raw, exc)
            cache[key] = None
            return None
        city = str(meta.get("city") or "").strip()
        if not city or city == raw:
            # 空判断 / 原样返回本身都视为「无可归一城市」
            city_out = None if not city else city
            cache[key] = city_out
            logger.info("LLM 地址归一：%s → %s（%s）", raw, city_out or "无法判断",
                        str(meta.get("reason") or "")[:80])
            return city_out
        cache[key] = city
        logger.info("LLM 地址归一：%s → %s（%s）", raw, city,
                    str(meta.get("reason") or "")[:80])
        return city

    return resolve
