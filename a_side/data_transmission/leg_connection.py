"""换乘接续共享口径（架构整理 P5.5 前置抽取：demo 链路与通用去程精排共用）。

用户拍板的换乘缓冲口径（阶段 2，8.31）：
- 飞 → 火：航班到达 + 出机场 30min + 市内转场 + 进站 30min；
- 火 → 飞：火车到达 + 出站 20min + 市内转场 + 提前 1.5h 到机场（值机/安检）。

市内转场分钟来自确定性表（``DEMO_TRANSFER_MINUTES``，高德实算后续接入——
由调用方注入 ``transfer_provider`` 替换 ``lookup_transfer_minutes``）。

此前口径只存在于 ``demo_candidate.py``（固定 Demo 剧情链路）；去程班次精排
（``planner_parts.trip_segments._select_outbound_combination``，2026-09-04）
需要同一套口径 → 抽到本模块单点定义，demo_candidate re-export 保持历史
import 路径兼容。
"""

from __future__ import annotations

from typing import Dict, Optional, Tuple

# -- 换乘缓冲 / 转场（用户拍板口径） --
AIR_ARRIVE_BUFFER_MIN = 30       # 飞→火：航班到达 + 出机场/行李缓冲
RAIL_CHECKIN_BUFFER_MIN = 30     # 飞→火：进站/安检缓冲
RAIL_ARRIVE_BUFFER_MIN = 20      # 火→飞：火车到达 + 出站缓冲
AIR_CHECKIN_BUFFER_MIN = 90      # 火→飞：提前 1.5h 到机场（值机/安检）
DEFAULT_TRANSFER_MIN = 45        # 转场兜底（站/机场未收录时）

# 确定性转场分钟（站/机场中文名对）；查表前做「去 站/机场 后缀」归一化
DEMO_TRANSFER_MINUTES: Dict[Tuple[str, str], int] = {
    ("常州奔牛机场", "常州北站"): 45,
    ("常州奔牛机场", "常州站"): 50,
    ("北京大兴机场", "北京南站"): 50,
    ("北京首都机场", "北京南站"): 60,
    ("上海虹桥国际机场", "上海虹桥站"): 15,
    ("上海浦东国际机场", "上海站"): 60,
}


def _norm_place(name: str) -> str:
    """站/机场名归一化：去空白与「站/机场/国际机场」尾缀（常州北 vs 常州北站）。"""
    text = str(name or "").strip()
    for suffix in ("国际机场", "机场", "站"):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            break
    return text


def lookup_transfer_minutes(
    from_place: str,
    to_place: str,
    table: Optional[Dict[Tuple[str, str], int]] = None,
) -> int:
    """站/机场名 → 转场分钟；未收录回退 ``DEFAULT_TRANSFER_MIN``。

    后续接入高德实算（AmapClient）时由调用方注入 ``transfer_provider`` 替换本表。
    """
    table = table if table is not None else DEMO_TRANSFER_MINUTES
    f, t = _norm_place(from_place), _norm_place(to_place)
    for (a, b), minutes in table.items():
        if _norm_place(a) == f and _norm_place(b) == t:
            return minutes
    return DEFAULT_TRANSFER_MIN


def required_gap_minutes(mode_a: str, mode_b: str, transfer_minutes: int) -> int:
    """前段 mode_a 到达 → 后段 mode_b 出发 所需的最小间隔（缓冲 + 转场）。

    用户拍板口径（见模块 docstring）；同类相接（不应出现）按转场分钟兜底。
    """
    pair = (str(mode_a or ""), str(mode_b or ""))
    if pair == ("air", "train"):
        return AIR_ARRIVE_BUFFER_MIN + int(transfer_minutes) + RAIL_CHECKIN_BUFFER_MIN
    if pair == ("train", "air"):
        return RAIL_ARRIVE_BUFFER_MIN + int(transfer_minutes) + AIR_CHECKIN_BUFFER_MIN
    return int(transfer_minutes)
