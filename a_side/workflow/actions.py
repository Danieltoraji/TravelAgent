"""从重规划结果生成 Action 清单（A 的 Workflow 职责）。

C 角色的 Permission Manager + Action Queue 消费本模块的输出。接口契约见
`plan/接口契约.md` 第五节。本层只「准备」预约动作，不执行、不付款：
- 更新路线 / 更新日历 → low / auto（直接执行）
- 预约 / 取消 / 同步预约 → medium / confirm（加入队列等用户确认）
- 支付类 → high / manual（仅提醒人工；本模块不生成）
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from algorithoms.select_spots import match_name

# 风险 / 执行方式枚举
RISK_LOW, RISK_MEDIUM, RISK_HIGH = "low", "medium", "high"
EXEC_AUTO, EXEC_CONFIRM, EXEC_MANUAL = "auto", "confirm", "manual"

# exec → 展示符号（Action Queue 界面用）
EXEC_MARK = {EXEC_AUTO: "✔", EXEC_CONFIRM: "□", EXEC_MANUAL: "⚠"}

action_schema = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "title": "动作名称"},
        "risk": {
            "type": "string",
            "enum": [RISK_LOW, RISK_MEDIUM, RISK_HIGH],
            "title": "风险等级",
        },
        "exec": {
            "type": "string",
            "enum": [EXEC_AUTO, EXEC_CONFIRM, EXEC_MANUAL],
            "title": "执行方式",
            "description": "auto 直接执行 / confirm 等用户确认 / manual 仅提醒人工",
        },
        "detail": {"type": "string", "title": "动作说明（含原因）"},
    },
    "required": ["action", "risk", "exec"],
}


def _find_spot_by_name(
    name: str, candidate_spots: Sequence[Sequence[Dict[str, Any]]]
) -> Optional[Dict[str, Any]]:
    """在候选池里按名称/别名找景点（复用 select_spots.match_name）。"""
    for group in candidate_spots:
        for spot in group:
            if match_name(spot, name) in (1, 2):
                return spot
    return None


def build_actions(
    replan_result: Dict[str, Any],
    candidate_spots: Sequence[Sequence[Dict[str, Any]]],
) -> List[Dict[str, str]]:
    """把 ``replan()`` 输出转换为 Action 清单（C 的 Permission Manager 输入）。

    - changes 非空 → 「更新路线」（low/auto）
    - added / removed / move / rescheduled 的景点若需预约（``reservation_required``）
      → 对应预约动作（medium/confirm）
    """
    changes = replan_result.get("changes") or []
    actions: List[Dict[str, str]] = []

    if changes:
        actions.append(
            {
                "action": "更新路线",
                "risk": RISK_LOW,
                "exec": EXEC_AUTO,
                "detail": f"检测到 {len(changes)} 处行程变化，已生成新路线",
            }
        )

    for change in changes:
        if change.get("type") == "hotel_changed":
            hotel_name = change.get("spot") or "酒店"
            if change.get("to"):
                actions.append(
                    {
                        "action": f"预订{hotel_name}",
                        "risk": RISK_MEDIUM,
                        "exec": EXEC_CONFIRM,
                        "detail": (
                            f"住宿调整：{change.get('from') or '—'} → "
                            f"{change.get('to')}；{change.get('reason') or '原因见事件'}"
                        ),
                    }
                )
            else:
                actions.append(
                    {
                        "action": f"处理{hotel_name}预订",
                        "risk": RISK_MEDIUM,
                        "exec": EXEC_CONFIRM,
                        "detail": (
                            f"住宿不可行，需人工处理：{change.get('reason') or ''}"
                        ),
                    }
                )
            continue
        name = change.get("spot", "")
        spot = _find_spot_by_name(name, candidate_spots)
        if spot is None or not spot.get("reservation_required"):
            continue
        change_type = change.get("type")
        if change_type == "added":
            actions.append(
                {
                    "action": f"预约{name}门票",
                    "risk": RISK_MEDIUM,
                    "exec": EXEC_CONFIRM,
                    "detail": f"{name}已加入行程，需提前预约",
                }
            )
        elif change_type == "removed":
            actions.append(
                {
                    "action": f"取消/改签{name}预约",
                    "risk": RISK_MEDIUM,
                    "exec": EXEC_CONFIRM,
                    "detail": f"{name}已从行程移除，原预约需调整",
                }
            )
        else:  # move / rescheduled
            actions.append(
                {
                    "action": f"同步{name}预约时间",
                    "risk": RISK_MEDIUM,
                    "exec": EXEC_CONFIRM,
                    "detail": (
                        f"{name}行程时间已调整"
                        f"（{change.get('from')} → {change.get('to')}），预约需同步"
                    ),
                }
            )
    return actions


def format_actions_text(actions: Sequence[Dict[str, str]]) -> str:
    """渲染成 Action Queue 展示文本（Today's Actions）。"""
    if not actions:
        return "（无待办动作）"
    lines = ["Today's Actions:"]
    for action in actions:
        mark = EXEC_MARK.get(action.get("exec", EXEC_CONFIRM), "□")
        lines.append(
            f"{mark} {action['action']}（{action.get('risk')}/{action.get('exec')}）"
        )
        if action.get("detail"):
            lines.append(f"    {action['detail']}")
    return "\n".join(lines)
