"""降级告知（真源查不到 → 告知用户 + 假源/估算替代，2026-09-14 用户需求）。

「替代」已有（live→fake 三态兜底 / estimated 段如实降级 / 自驾兜底），缺的是
「告知」——C 端用户只看到结果，不知道哪段是估算/兜底。本模块把降级事件收成
**人话告知清单**，随计划透出（B 侧 /api/plan/ 响应与 /api/status/ 的
``notices`` 字段，只增不改）。

两类来源：
1. **段级估算扫描**（``segment_fallback_notices``，纯函数）：扫 trip_segments
   的 legs，把 estimated/推演的城际腿翻译成用户可读的告知（含端点与原因
   提示——12306 预售期/当日无航班/自驾兜底）；
2. **管线级兜底**（DataSourceResolver._add_notice）：spots/规划失败回退假池、
   酒店真源失败回退内置候选、live_fallback 整体降级，逐条 append（去重限流）。
"""

from __future__ import annotations

from typing import Any, Dict, List

# 告知上限（防极端计划刷屏；去重后超出截断）
MAX_NOTICES = 8

_MODE_TEXT = {"train": "火车", "air": "航班", "driving": "自驾"}


def segment_fallback_notices(segments: List[Dict[str, Any]]) -> List[str]:
    """扫 trip_segments → 降级告知清单（纯函数，不抛异常，坏输入给空表）。

    口径：
    - intercity 腿 ``source == estimated``（真源无班次：超出 12306 预售期 /
      当日无航班 / 估算航段）→ 「『A→B』航班段未查到当日真源班次，已按估算
      衔接，请出行前二次确认」；
    - 段级自驾兜底（mode=driving 的城际段，如返程班次全挂）→ 「未查到可行
      班次（可能超出 12306 预售期或当日无班次），已按自驾估算」。
    live 腿与市内衔接腿不产生告知（真源正常无需打扰）。
    """
    notices: List[str] = []
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("type") != "transport":
            continue
        details = seg.get("details") or {}
        kind = str(details.get("kind") or "")
        direction = "去程" if kind == "outbound" else "返程" if kind == "return" else ""
        # 段级自驾兜底（城际主段整段 driving）
        if str(details.get("mode") or "") == "driving":
            notices.append(
                f"{direction}（{details.get('from')}→{details.get('to')}）"
                "未查到可行班次（可能超出 12306 预售期或当日无班次），已按自驾估算"
            )
            continue
        for leg in details.get("legs") or []:
            if not isinstance(leg, dict) or leg.get("kind") != "intercity":
                continue
            if str(leg.get("source") or "") == "estimated":
                mode = _MODE_TEXT.get(str(leg.get("mode") or ""), "交通")
                notices.append(
                    f"『{leg.get('from')}→{leg.get('to')}』{mode}段"
                    "未查到当日真源班次（可能超出 12306 预售期或当日无航班），"
                    "已按估算衔接，请出行前二次确认"
                )
    return notices


def merge_notices(*batches: List[str]) -> List[str]:
    """合并去重 + 截断（保序；纯函数供测试与收集点共用）。"""
    out: List[str] = []
    for batch in batches:
        for text in batch or []:
            if text and text not in out:
                out.append(text)
    return out[:MAX_NOTICES]


def plan_fallback_notices(
    plan: Dict[str, Any], extra: List[str] = None
) -> List[str]:
    """最终计划 → 完整告知清单（段级扫描 + 管线级 extra 合并）。"""
    segments = (plan or {}).get("trip_segments") or []
    return merge_notices(segment_fallback_notices(segments), extra)
