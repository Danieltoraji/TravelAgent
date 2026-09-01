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


# 六个镜像目录（与 AGENTS.md「a_side 自动同步」范围一致）
MIRROR_DIRS = [
    "algorithoms",
    "call_llm",
    "data_transmission",
    "fake_spots",
    "transport",
    "workflow",
]


def test_ab_no_stale_files():
    """a_side 无多余 / 无缺失文件（防陈留旧文件漏检）。

    ``test_ab_core_files_in_sync`` 只查 ``SYNC_FILES`` 白名单内文件的漂移，
    查不到白名单外的陈留（历史案例：工作区已删的 decision_maker.md /
    planner.md / replanner.md 仍残留在 a_side，白名单测试全绿）。本守卫按
    6 个镜像目录比对两侧**文件名集合**：
    - a_side 出现工作区没有的文件 = 陈留（已删未清理）；
    - 工作区出现 a_side 没有的文件 = 漏同步（新增未复制）。
    两侧必须一致；内容一致性由 SHA 白名单测试 + 镜像纪律保证。
    """
    for sub in MIRROR_DIRS:
        a_dir = _A_ROOT / sub
        b_dir = _B_ROOT / "a_side" / sub
        if not a_dir.is_dir() and not b_dir.is_dir():
            continue

        def _file_set(root: Path) -> set:
            """递归收集镜像目录内全部文件相对路径（rglob，含子目录）。"""
            if not root.is_dir():
                return set()
            return {
                str(p.relative_to(root)).replace("\\", "/")
                for p in root.rglob("*")
                if p.is_file() and "__pycache__" not in p.parts
            }

        a_names = _file_set(a_dir)
        b_names = _file_set(b_dir)
        extras = sorted(b_names - a_names)
        missing = sorted(a_names - b_names)
        assert not extras, (
            f"a_side/{sub} 有多余文件（工作区已删未清理）: {extras}\n"
            "请在 a_side 侧删除同名残留。"
        )
        assert not missing, (
            f"a_side/{sub} 缺失文件（新增未同步）: {missing}\n"
            "请以 A 主目录为唯一编辑源复制到 a_side。"
        )