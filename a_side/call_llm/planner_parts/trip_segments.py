"""P4 拆分：城际来去程段构造器（TripSegmentAttacher）。

自 ``b_planner_hook.BPlannerHook`` 拆出（BPlannerHook 拆分后只剩编排）：
- 城际段构建 / 注入 / 兼容 wrapper（原 ``_build_trip_segments`` /
  ``_inject_trip_segments`` / ``_attach_trip_segments``）
- 来去程窗的默认注入（原 ``_ensure_default_travel_schedule``）
- 城际地名归一化（原 ``_normalize_intercity_places``，P3.1 接线）
- 首末日窗口与返程组合选择的纯函数（原模块级函数，测试直接 import，
  经 ``b_planner_hook`` re-export 保持 `TravelAgent.call_llm.b_planner_hook`
  命名空间兼容）

**类身份约定**：与 ``data_transmission/b_contract.py`` 一致，契约导入全部用
顶层 ``import core.schemas``；``TripSegmentAttacher`` 是 mixin，方法签名与
原 BPlannerHook 私有方法完全一致，经继承保留在 ``BPlannerHook`` 实例上
（现有测试直接调用，零改动）。
"""

from __future__ import annotations

import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_REPO_ROOT = str(Path(__file__).resolve().parent.parent.parent)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import logging

logger = logging.getLogger("call_llm.planner_parts.trip_segments")

from data_transmission.b_contract import _as_date  # noqa: E402
from data_transmission.enums import Source  # noqa: E402


# 到达日接驳缓冲（方案 A，用户 9.1 拍板）：城际到达后到第一个景点的缓冲分钟。
# 未来演进为「机场/车站 → 酒店 的转场时长 + 30min」（local 接驳 legs 现为占位）。
_ARRIVAL_BUFFER_MINUTES = 90

# 离开日缓冲（用户 9.2 拍板 60min）：末日游玩截止 = 返程出发时刻 − 该值
# （语义：出发前 1h 到站/机场）。
_DEPARTURE_BUFFER_MINUTES = 60


# ---------------------------------------------------------------------------
# 纯函数：首末日窗口 + 返程组合选择（测试直接 import，勿改名）
# ---------------------------------------------------------------------------


def _first_day_start_from_segments(
    segments: List[Dict[str, Any]],
    buffer_minutes: int = _ARRIVAL_BUFFER_MINUTES,
) -> Optional[str]:
    """城际去程到达时刻 + 接驳缓冲 → 首日起点 ``HH:MM``（无去程段 → None）。

    - 只认 ``type=="transport"`` 且 ``details.kind=="outbound"`` 的段（travel.py
      make_segment 产出：``end_minutes = 出发时刻 + total_minutes`` 即到达时刻）；
    - 起点 = ``max(09:00, 到达 + 缓冲)``：到达早于 09:00 仍 09:00 起（景点开门前）；
    - 罕见超长/跨日联运（到达+缓冲 ≥ 24h）→ 截断次日并保底 09:00（当天不可达，
      次日从 09:00 起为保守语义，先不细究跨日到达日归属）。
    """
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("type") != "transport":
            continue
        if (seg.get("details") or {}).get("kind") != "outbound":
            continue
        end = seg.get("end_minutes")
        if not isinstance(end, (int, float)) or end <= 0:
            continue
        start = max(9 * 60, int(end) + int(buffer_minutes))
        if start >= 24 * 60:
            start = max(9 * 60, start % (24 * 60))
        return f"{start // 60:02d}:{start % 60:02d}"
    return None


def _last_day_end_from_segments(
    segments: List[Dict[str, Any]],
    buffer_minutes: int = _DEPARTURE_BUFFER_MINUTES,
) -> Optional[int]:
    """返程段出发时刻 − 离开缓冲 → 末日游玩截止分钟（无返程段 → None）。

    return 段 start_minutes 语义（9.2 修正）：**从目的地出发的时刻**（=
    最晚到家 return_time − 返程总耗时，见 travel.build_trip_segments 注释），
    不是 return_time 本身；返程过早就保底 09:00（极早返程属行程设计问题）。
    """
    return_seg = _find_return_segment(segments)
    if return_seg is None:
        return None
    start = return_seg.get("start_minutes")
    if not isinstance(start, (int, float)) or start <= 0:
        return None
    return max(9 * 60, int(start) - int(buffer_minutes))


def _find_return_segment(
    segments: List[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    for seg in segments or []:
        if not isinstance(seg, dict) or seg.get("type") != "transport":
            continue
        if (seg.get("details") or {}).get("kind") == "return":
            return seg
    return None


def _intercity_leg_candidates(return_seg: Dict[str, Any]) -> List[Tuple[int, List[Dict[str, Any]]]]:
    """return 段 legs 里各 intercity 段的真源候选 → [(前缀耗时, 候选列表), …]。

    - 候选：train: code/depart_time/arrive_time/price…；air:
      flight_no/depart_time/arrive_time…；
    - 前缀耗时 = 该段之前所有 intercity 段的 (duration_min + buffer_min) 之和
      （如 北京飞南宁 3h10+90 值机缓冲 后接 南宁高铁段 → 高铁段前缀 280min），
      用于把「段内发车时刻」倒推成「整链起点 = 离开目的地时刻」；
    - 候选为空的段（如 estimated 航段无班次）跳过——选班只在实际有班次的段上发生。
    """
    out: List[Tuple[int, List[Dict[str, Any]]]] = []
    prefix = 0
    for leg in (return_seg.get("details") or {}).get("legs") or []:
        if leg.get("kind") != "intercity":
            continue
        cands = [c for c in (leg.get("candidates") or []) if isinstance(c, dict)]
        if cands:
            out.append((prefix, cands))
        dur = leg.get("duration_min") or 0
        buf = leg.get("buffer_min") or 0
        try:
            prefix += int(dur) + int(buf)
        except (TypeError, ValueError):
            prefix += 0
    return out


def _hhmm_to_minutes_loose(text: Any) -> Optional[int]:
    """候选时刻（'HH:MM' 或 'YYYY-MM-DD HH:MM[:SS]'）→ 分钟；不可解析 → None。"""
    if not text:
        return None
    token = str(text).strip()
    if " " in token:
        token = token.split(" ")[-1]  # 带日期的形式取时间部分
    try:
        hour, minute = token.split(":", 2)[:2]
        value = int(hour) * 60 + int(minute)
        return value if 0 <= value < 24 * 60 else None
    except (ValueError, AttributeError):
        return None


def _select_return_combination(
    return_seg: Dict[str, Any],
    earliest_departure: int,
    transfer_buffer_minutes: int = 60,
) -> Optional[Tuple[int, int, float]]:
    """按「到家 ≤ return_time」从真源候选选「出发 ≥ earliest 的最晚班次组合」。

    真源候选选「出发 ≥ earliest 的最晚班次组合」。

    - return_time = ``return_seg["end_minutes"]``（travel.py 修正后 end = 最晚到家）；
    - **组合语义（9.2 b 多段链修正）**：选班段可能是链的中/尾段（贵港链 =
      北京飞南宁 estimated + 南宁东→贵港高铁 live，真实班次只在高铁段）——
      整链起点（= 离开目的地时刻）= 选班段发车 − 该段前缀耗时（含值机缓冲），
      保证倒推出的起飞时刻与所选高铁接得上（反链推导，而非把高铁发车当
      整链起点）；
    - 段级粗选 + 换乘粗卡（前段到达 ≤ 后段发车 − transfer_buffer），
      **不做 Day 4 级真实接续校验**（交接 §四.2）；
    - 跨日班次（arrive < depart）当天到不了家 → 直接排除；
    - 返回 (整链起点分钟, 到家分钟, 组合票价和)；无可行 → None。
    """
    per_leg = _intercity_leg_candidates(return_seg)
    if not per_leg:
        return None
    return_time = return_seg.get("end_minutes")
    if not isinstance(return_time, (int, float)) or return_time <= 0:
        return None
    best: Optional[Tuple[int, int, float]] = None

    def walk(
        leg_index: int,
        prev_arrive: Optional[int],
        dep_first_chain: Optional[int],
        arr_last: Optional[int],
        cost: float,
        prefix: int,
    ) -> None:
        nonlocal best
        if leg_index == len(per_leg):
            if (
                dep_first_chain is not None
                and dep_first_chain >= earliest_departure
                and (best is None or dep_first_chain > best[0])
            ):
                best = (dep_first_chain, int(arr_last or 0), cost)
            return
        is_last = leg_index == len(per_leg) - 1
        for cand in per_leg[leg_index][1]:
            dep = _hhmm_to_minutes_loose(cand.get("depart_time"))
            arr = _hhmm_to_minutes_loose(cand.get("arrive_time"))
            if dep is None or arr is None:
                continue
            if arr < dep:  # 跨日（凌晨到）→ 当天到不了家，排除
                continue
            dep_chain = dep - prefix  # 整链起点 = 离开目的地时刻
            if leg_index == 0:
                if dep_chain < earliest_departure:
                    continue
                dep_first_chain = dep_chain
            elif prev_arrive is not None and dep < prev_arrive + transfer_buffer_minutes:
                continue  # 换乘粗卡：下段发车 ≥ 上段到达 + 缓冲
            if is_last and arr > return_time:
                continue  # 到家 ≤ 最晚到家时间
            price = cand.get("price") or cand.get("cost_per_person")
            walk(
                leg_index + 1,
                arr,
                dep_first_chain,
                arr if is_last else arr_last,
                cost + (float(price) if isinstance(price, (int, float)) else 0.0),
                per_leg[leg_index + 1][0]
                if leg_index + 1 < len(per_leg) else 0,
            )

    walk(0, None, None, None, 0.0, per_leg[0][0])
    return best


def _windowed_last_day_end(
    segments: List[Dict[str, Any]],
    buffer_minutes: int = _DEPARTURE_BUFFER_MINUTES,
) -> Optional[int]:
    """末日截止：优先用返程真源候选「最晚可行班次出发 − 缓冲」，否则反推兜底。

    反推（return_time − 总耗时）把返程当连续可选；真源候选是**离散班次**——
    「末段到家 ≤ return_time 中首段出发最晚」的班次决定末日最晚能玩到几点，
    通常比反推松弛（如返程 19:30 有班 → 末日可玩到 18:30，而非 14:10）。
    """
    return_seg = _find_return_segment(segments)
    if return_seg is not None:
        combo = _select_return_combination(return_seg, earliest_departure=0)
        if combo is not None:
            dep_min = combo[0]
            return max(9 * 60, dep_min - buffer_minutes)
    return _last_day_end_from_segments(segments, buffer_minutes)


def _rebuild_return_with_schedule(
    plan: Dict[str, Any],
    segments: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """末日行程排定后按「实际离开目的地时间」从真源候选重选返程班次。

    - 最早可离开 = 末日最后事件结束 + 离开缓冲（60min，用户 9.2 拍板）；
    - 选中班次 → 重建 return 段真实发到时刻（替换反推占位）；
    - 无候选 / 候选都不满足 → 保留原段（反推占位兜底），不谎报班次。
    """
    return_seg = _find_return_segment(segments)
    if return_seg is None:
        return segments
    plan_days = plan.get("days") or []
    if not plan_days:
        return segments
    nodes = plan_days[-1].get("route_details") or []
    ends = [
        n.get("end_minutes")
        for n in nodes
        if isinstance(n.get("end_minutes"), (int, float)) and n.get("type") != "meal"
    ]
    if not ends:
        return segments
    earliest_departure = int(max(ends)) + _DEPARTURE_BUFFER_MINUTES
    combo = _select_return_combination(return_seg, earliest_departure)
    if combo is None:
        return segments
    dep_min, arr_min, cost = combo
    return_seg["start_minutes"] = dep_min
    return_seg["end_minutes"] = arr_min
    return_seg["duration_minutes"] = arr_min - dep_min
    details = dict(return_seg.get("details") or {})
    if cost > 0:
        details["cost_per_person"] = round(cost, 2)
        details["source"] = Source.LIVE.value
    return_seg["details"] = details
    return segments


# ---------------------------------------------------------------------------
# Mixin：TripSegmentAttacher（BPlannerHook 继承，method 签名同原私有方法）
# ---------------------------------------------------------------------------


class TripSegmentAttacher:
    """城际来去程段构造器（mixin）。

    依赖宿主实例属性：``requirement`` / ``_tool_provider`` / ``_use_live``。
    """

    def _ensure_default_travel_schedule(self) -> Dict[str, Any]:
        """Web 场景无交互：缺 ``travel_schedule`` 时注入默认来去程窗。

        默认策略（计划 §4.1）：去程首日 09:00 前出发、返程末日 18:00 前；
        无 ``start_date`` → 无法定默认窗，跳过（保底不生成来去程段）。
        已具备完整 schedule 的请求不动（幂等）。
        """
        content = self.requirement.get("content") or {}
        schedule = content.get("travel_schedule") or {}
        keys = ("departure_date", "departure_time", "return_date", "return_time")
        if all(schedule.get(key) for key in keys):
            return content
        if not content.get("start_date"):
            return content
        days_n = int(content.get("days") or 1)
        start = _as_date(content["start_date"])
        end = start + timedelta(days=max(0, days_n - 1))
        content["travel_schedule"] = {
            "departure_date": start.isoformat(),
            "departure_time": "09:00",
            "return_date": end.isoformat(),
            "return_time": "18:00",
        }
        return content

    def _normalize_intercity_places(self, content: Dict[str, Any]) -> None:
        """P3.1 接线：城际来去程 origin/destination 进工具层前过地名归一化。

        贵港事故（8.30）止血的 A 侧闭环：LLM 备注解析/用户输入可能产出带
        省级前缀的地名（「广西贵港」）→ 12306 站名解析只认「贵港」→ 城际
        真源全 error → 静默 driving 兜底。归一化层（``PlaceNormalizer``）
        建好后一直无生产引用（P0–P3 检验发现），此处接上主链：

        - 城市级内置（估算表 ∪ 航路 ∪ 贵港补充）先行，站级 city 反查等
          B 侧注入 ``station_resolver`` 后再补；
        - 归一成功**写回 content**（provider 构造与 ``build_trip_segments``
          内部同源读到干净地名），失败/未识别记 warning 用原值（不阻断
          规划——脏地名不再静默级联，而是可追踪的 warning）；
        - 惰性 import：normalizer 依赖估算表/航路表，仅城际段构建时加载。
        """
        try:
            from data_transmission.place_normalizer import build_place_normalizer

            normalizer = build_place_normalizer()
        except Exception as exc:  # noqa: BLE001  归一化层不可用不阻断规划
            logger.warning("PlaceNormalizer 初始化失败，城际地名不归一：%s", exc)
            return
        for key in ("origin", "destination"):
            raw = (content.get(key) or "").strip()
            if not raw:
                continue
            try:
                result = normalizer.normalize(raw)
            except Exception as exc:  # noqa: BLE001  归一化异常不阻断规划
                logger.warning("城际地名归一化异常（%s=%s）：%s", key, raw, exc)
                continue
            if not result.matched:
                candidates = "、".join(result.fuzzy_candidates or ())
                logger.warning(
                    "城际地名未识别（%s=%s），用原值；候选：%s",
                    key, raw, candidates or "无",
                )
                continue
            canonical = result.city or result.canonical
            if canonical and canonical != raw:
                logger.info(
                    "城际地名归一：%s=%s → %s（%s）", key, raw, canonical, result.method
                )
                content[key] = canonical

    def _build_trip_segments(self) -> List[Dict[str, Any]]:
        """构建城际来去程段（**一次**查询：demo 候选 → 主链 build_trip_segments）。

        - 方案 A（到达日时间轴重叠修复）调用时机改为规划**前**：取去程段
          ``end_minutes``（城际到达时刻）计算首日起点；规划后仅
          ``_inject_trip_segments`` 写入——避免重复查询 12306/juhe 消耗额度；
        - demo 候选链路不读 plan 参数（纯 requirement 驱动，见 demo_candidate.py
          build_demo_trip_segments），此处传空 plan ``{}`` 安全；
        - 失败返回 []（不阻断规划），与旧 ``_attach_trip_segments`` 语义一致。
        """
        from data_transmission.travel import build_trip_segments

        self._ensure_default_travel_schedule()
        # P3.1 接线：城际来去程 origin/destination 进工具层前过地名归一化
        # （贵港事故：LLM 解析出「广西贵港」→ 剥省前缀「贵港」→ 12306 认）。
        # 归一成功写回 content（provider 构造与 build_trip_segments 内部同源
        # 读到干净地名）；归一失败/未识别记 warning 用原值（不阻断规划）。
        content_ref = self.requirement.get("content")
        if isinstance(content_ref, dict):
            self._normalize_intercity_places(content_ref)
        provider = None
        if self._use_live and getattr(self, "_tool_provider", None) is not None:
            # 8.29 真源：组合城际 provider（train 12306 → flight juhe → map 估算兜底）。
            from data_transmission.live_data import make_live_intercity_provider

            content = self.requirement.get("content") or {}
            # P3-D2a：主链城际真源查询共享 (name,o,d,date) 缓存——同对同日期
            # 只查一次真源（命中不计数、正负都缓存），去程/返程 BFS 与直达段
            # 不重复打 12306/juhe；一次规划一个 cache dict。
            provider = make_live_intercity_provider(
                self._tool_provider,
                content.get("travel_schedule") or {},
                origin=(content.get("origin") or "").strip(),
                destination=(content.get("destination") or "").strip(),
                cache={},
            )
        # 固定 Demo 场景（锦州→上海）优先走候选链路（fixture，断网可复现）。
        demo_segments: List[Dict[str, Any]] = []
        try:
            from data_transmission.demo_candidate import build_demo_trip_segments

            demo_segments = build_demo_trip_segments(
                {}, self.requirement,
                tool_provider=getattr(self, "_tool_provider", None),
            )
        except Exception as exc:  # noqa: BLE001  Demo 链路失败 → 回退原链
            logger.warning("demo chain segments failed: %s", exc)
            demo_segments = []
        if demo_segments:
            return demo_segments
        try:
            return build_trip_segments(
                {}, self.requirement, travel_provider=provider
            )
        except Exception as exc:  # noqa: BLE001  城际段失败不阻断规划
            logger.warning("build_trip_segments failed: %s", exc)
            return []

    def _inject_trip_segments(
        self, plan: Dict[str, Any], segments: List[Dict[str, Any]]
    ) -> None:
        """规划后把**预构建**的城际段写入 ``plan["trip_segments"]``（不重复查询）。

        批次 3（预算 transit）：同时写 ``plan["estimated_transit_cost"]``（= 去程 +
        返程人均费用之和 × 人数，预算五项的权威来源；replan 时由 replanner 保留，
        ``_plan_cost_summary`` 优先读它，缺省再从 trip_segments 兜底推导）。
        """
        if not segments:
            return
        plan["trip_segments"] = segments
        per_person = sum(
            float((seg.get("details") or {}).get("cost_per_person") or 0.0)
            for seg in segments
        )
        visitor_number = int(
            plan.get("visitor_number")
            or (self.requirement.get("content") or {}).get("visitor_number")
            or 1
        )
        plan["estimated_transit_cost"] = round(per_person * visitor_number, 2)

    def _attach_trip_segments(self, plan: Dict[str, Any]) -> None:
        """城际来去程段构建 + 写入（**兼容旧接口**，构建+注入一步完成）。

        生产路径请改用 ``_build_trip_segments()``（规划前一次）+
        ``_inject_trip_segments(plan, segments)``（规划后只写不重查），
        避免 live 场景重复查询 12306/juhe。本 wrapper 保留给既有测试/外部调用。
        """
        segments = self._build_trip_segments()
        self._inject_trip_segments(plan, segments)