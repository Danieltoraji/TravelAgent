"""A/B 源码一致性保护（四小时作战包 I-01 / 0:20-0:55）。

A 主目录为唯一编辑源；B 通过 ``a_side/`` 加载 A 侧逻辑。本测试对比
``data_transmission`` 核心文件两侧内容，漂移即失败——"A 测试通过"不再被
当作"B 集成通过"的替代品（B 运行时优先加载自己的仓库及 a_side）。

对比前做行尾规范化（\\r\\n → \\n），避免 checkout 换行转换误报。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 本文件位于 B 仓库 tests/ 下：
#   parents[0]=tests, parents[1]=B 仓库根(TravelAgent/TravelAgent), parents[2]=A 仓库根
_B_ROOT = Path(__file__).resolve().parents[1]
_A_ROOT = Path(__file__).resolve().parents[2]

# 双方共有的同步文件（A data_transmission ↔ B a_side/data_transmission）
# P1/P2/P3 新增模块（enums/intercity_strategy/place_normalizer/quota_manager/
# live_errors/adapters/tool_specs）已并入清单——这些是 A 侧权威定义的接口层，
# 漂移必须被本测试拦截（P3-F 收尾补全）。
SYNC_FILES = [
    # 原核心
    "city_travel.py",
    "live_data.py",
    "travel.py",
    "air_routes.py",
    "air_routes.json",
    "demo_candidate.py",
    # P1：口径枚举集中
    "enums.py",
    # P2：城际策略框架
    "intercity_strategy.py",
    # P3：地名归一化 / 额度管家 / 异常 / 适配器 / ToolSpec 注册表
    "place_normalizer.py",
    "quota_manager.py",
    "live_errors.py",
    "adapters.py",
    "tool_specs.py",
]


def _norm(text: bytes) -> bytes:
    return text.replace(b"\r\n", b"\n")


def test_ab_core_files_in_sync():
    for name in SYNC_FILES:
        a_path = _A_ROOT / "data_transmission" / name
        b_path = _B_ROOT / "a_side" / "data_transmission" / name
        assert a_path.is_file(), f"A 侧缺失: {a_path}"
        assert b_path.is_file(), f"B 侧缺失（未同步）: {b_path}"
        a_data = _norm(a_path.read_bytes())
        b_data = _norm(b_path.read_bytes())
        assert a_data == b_data, (
            f"A/B 漂移: {name}\n"
            f"  A: {a_path}\n  B: {b_path}\n"
            "请以 A 主目录为唯一编辑源，重新同步 a_side。"
        )