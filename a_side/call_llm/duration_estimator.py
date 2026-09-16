"""LLM 游览时长估算（2026-09-16，用户拍板：时长应由大模型估算）。

背景：B 侧 scenic 给每个 POI 候选固定 ``suggest_duration=120``（高德不提供
建议游玩时长），A 侧仅少数城市有人工时长表（spot_durations.json）——襄阳
实测全部景点排 2 小时整。修复：LLM 按景点名称/类别批量估算游览时长，覆盖
默认值；人工标注值与估算失败值不动。

设计（与 ``meal_filter`` 同款 builder 模式）：
- **批量**：一次规划全部需估算景点去重后**单次** LLM 调用，结果按名称缓存；
- **门控**：``USE_LLM_TOOLS``；**失败不阻断**：估算失败保持原值（默认 2h
  兜底总比没行程强）；范围钳制 30~360 分钟；
- 原地填 ``spot["duration"]``（返回 None，调用方拿原列表）。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("call_llm.duration_estimator")

from call_llm.client_factory import create_llm_client  # noqa: E402  模块级：测试 patch

DURATION_SYSTEM = (
    "你是旅行规划助手，负责估算景点/场馆的**合理游览时长**（分钟）。\n"
    "参考量级：小型展馆/纪念地/广场 30~60；寺观/普通公园 60~90；"
    "博物馆/遗址群/主题街区 90~180；大型山水景区/古镇/大型园区 120~240；"
    "超大综合景区（需换乘/多片区）可到 240~360。"
    "只输出 JSON：{durations: [{name, minutes}]}（minutes 为 30~360 整数），"
    "不要解释。"
)

DURATION_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "durations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "minutes": {"type": "integer",
                                "description": "30~360 整数分钟"},
                },
                "required": ["name", "minutes"],
            },
        },
    },
    "required": ["durations"],
}

# 估算范围钳制（防 LLM 给离谱值）
_MIN_MINUTES, _MAX_MINUTES = 30, 360


def build_llm_duration_estimator(
    *,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout: int = 30,
) -> Optional[Callable[[List[Dict[str, Any]]], None]]:
    """构造 ``duration_estimator_fn(spots) -> None``（原地填 duration）。

    ``USE_LLM_TOOLS`` 关 / 客户端创建失败 → None（调用方跳过，保持默认 2h）。
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
            system_instruction=DURATION_SYSTEM,
            max_tokens=1200,
        )
    except Exception as exc:  # noqa: BLE001  无 key/配置坏 → 不启用
        logger.warning("LLM 时长估算客户端创建失败，不启用：%s", exc)
        return None

    cached: Dict[str, int] = {}   # 景点名 → 估算分钟

    def estimator(spots: List[Dict[str, Any]]) -> None:
        names = [
            str(s.get("name") or "").strip()
            for s in (spots or []) if isinstance(s, dict)
        ]
        unknown = [n for n in names if n and n not in cached]
        if unknown:
            listing = "\n".join(f"- {n}" for n in unknown)
            try:
                result = client.generate(
                    messages=[{
                        "role": "user",
                        "content": (
                            f"以下 {len(unknown)} 个景点的建议游览时长（分钟）：\n"
                            f"{listing}"
                        ),
                    }],
                    response_schema=DURATION_SCHEMA,
                )
                for item in (result.get("content") or {}).get("durations") or []:
                    name = str(item.get("name") or "").strip()
                    minutes = item.get("minutes")
                    if name and isinstance(minutes, (int, float)):
                        cached[name] = int(min(max(int(minutes), _MIN_MINUTES),
                                               _MAX_MINUTES))
                for n in unknown:
                    cached.setdefault(n, 0)   # LLM 漏答 → 0 = 不覆盖默认值
            except Exception as exc:  # noqa: BLE001  LLM 失败 → 不覆盖
                logger.warning("LLM 时长估算失败，保持默认时长：%s", exc)
                return
        for spot in (spots or []):
            if not isinstance(spot, dict):
                continue
            estimated = cached.get(str(spot.get("name") or "").strip())
            if estimated and estimated > 0:
                spot["duration"] = estimated

    return estimator
