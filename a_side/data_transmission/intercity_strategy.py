"""城际候选策略框架（P2：四实现 → 一个候选策略接口）。

把 ``travel._resolve_intercity_route`` 的四级级联（直达 → 空铁候选 → 老 BFS
→ 直达兜底）改成**显式策略链**（架构整理方案 §三 P2）：

- ``IntercityStrategy``：统一接口 ``resolve(ctx) -> List[IntercityRoute]``
  （候选列表，可空）。链按顺序执行，**首个产出非空候选的策略胜出**；
- ``apply_budget_fallback``：预算回落后处理器（8.30 budget_per_leg 逻辑从
  级联第 2 级抽出），作用于任何策略的候选列表，按 ``scenario`` 三档保持
  现状文案（见函数 docstring）；
- ``resolve_intercity_chain``：链组装 + 异常不阻断（策略失败 → 记 warning
  继续下一策略，与现状 try/except 回落语义一致）。

金标准纪律（§七 杠杆二）：31 项 intercity 用例行为零回归——链的级联顺序、
回退语义、warning 文案与 ``_resolve_intercity_route`` 现状完全一致；
``train_edge_unbudgeted`` 通道保留（P3 额度管家完成后再撤销，见方案 §三 P2）。

策略实现类（DirectStrategy / AirRailStrategy / GraphBfsStrategy /
DirectFallbackStrategy）在本模块，随 P2 子任务逐步从 travel.py 迁移。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Protocol, Tuple

from data_transmission.city_travel import (
    AIR_BUFFER_MIN,
    DEFAULT_MAX_TOTAL_MINUTES,
    CityTravelEdge,
    IntercityRoute,
    find_city_travel_preferred,
    find_intercity_route,
)
from data_transmission.enums import Mode

logger = logging.getLogger("data_transmission.intercity_strategy")


@dataclass
class IntercityStrategyContext:
    """策略链共享上下文（跨策略缓存，避免重复调 provider）。

    ``direct`` / ``direct_minutes``（≤12h 判断用的完整耗时）由
    ``DirectStrategy`` 先行计算并写回，供 ``AirRailStrategy``（候选优于被迫
    直达判断）与 ``DirectFallbackStrategy``（直达到底兜底）复用——与现状
    ``_resolve_intercity_route`` 单函数内共享变量等价。
    """

    origin: str
    destination: str
    options: Dict[Tuple[str, str], Dict[str, CityTravelEdge]]
    provider: Optional[Callable[..., Optional[CityTravelEdge]]] = None
    priority: Optional[str] = None
    date_str: str = ""
    budget_per_leg: Optional[float] = None
    direct: Optional[CityTravelEdge] = None
    direct_minutes: Optional[int] = None

    @property
    def pair(self) -> str:
        return f"{self.origin} → {self.destination}"


class IntercityStrategy(Protocol):
    """候选策略统一协议：resolve(ctx) -> 候选列表（可空）。

    语义约定：返回的候选列表**已按偏好排好序**（首选 = candidates[0]），
    但**未做过预算处理**——预算回落统一由链上的 ``apply_budget_fallback``
    完成（scenario 由策略声明）。返回空列表 = 本策略无解，交下一策略。
    """

    budget_scenario: str = "candidates"  # direct | candidates | none

    def resolve(self, ctx: IntercityStrategyContext) -> List[IntercityRoute]:
        ...


def apply_budget_fallback(
    candidates: List[IntercityRoute],
    budget_per_leg: Optional[float],
    origin: str,
    destination: str,
    scenario: str = "candidates",
) -> IntercityRoute:
    """预算回落后处理器（8.30 budget_per_leg 逻辑，P2 从级联第 2 级抽出）。

    作用于**任何策略**产出的候选列表；``budget_per_leg`` 为 None → 原样返回
    首选（不约束，兼容旧调用方/测试）。三种场景（warning 文案保持现状）：

    - ``scenario="direct"``（DirectStrategy，单候选直达）：首选超预算 → 维持
      首选 + warning「建议用户上调预算或改 cost 偏好」——直达无替代，不回落；
    - ``scenario="candidates"``（AirRailStrategy，多候选联运）：首选超预算 →
      预算内最便宜候选回落（warning「回落预算内最便宜候选」）；预算内无可行
      → 维持首选 + warning「全部候选超单程预算」——不静默降级，用户可见可改；
    - ``scenario="none"``（GraphBfsStrategy / DirectFallbackStrategy）：现状
      这两级无预算处理（BFS 兜底段直接返回），后处理器对它们仅做候选选取，
      不回落不 warning。
    """
    if not candidates:
        raise ValueError("apply_budget_fallback 收到空候选列表")
    best = candidates[0]
    if budget_per_leg is None:
        return best
    if best.total_cost <= budget_per_leg or best.total_cost <= 0:
        # 不超预算 / 免单（cost=0）→ 直接给首选，与现状一致
        return best
    if scenario == "none":
        return best
    if scenario == "direct":
        # DirectStrategy 恒单候选（≤12h 直达到底）：超预算仅 warn 不回落。
        # 注意不能用 len(candidates)==1 区分——AirRail 单候选（如 type A
        # 直达铁路）超预算时现状走「全部候选超预算」文案，不是这条。
        logger.warning(
            "城际直达 %s→%s 人均 ¥%.0f 超单程预算 ¥%.0f（速度偏好保持，"
            "建议用户上调预算或改 cost 偏好）",
            origin, destination, best.total_cost, budget_per_leg,
        )
        return best
    # scenario == "candidates"（多候选联运回落）
    affordable = [r for r in candidates if r.total_cost <= budget_per_leg]
    if affordable:
        cheapest = min(affordable, key=lambda r: r.total_cost)
        logger.warning(
            "城际 %s→%s 首选（%dmin ¥%.0f）超单程预算 ¥%.0f，回落预算内"
            "最便宜候选（%dmin ¥%.0f）",
            origin, destination, best.total_minutes, best.total_cost,
            budget_per_leg, cheapest.total_minutes, cheapest.total_cost,
        )
        return cheapest
    logger.warning(
        "城际 %s→%s 全部候选超单程预算 ¥%.0f（最便宜 ¥%.0f），维持偏好首选并提示用户",
        origin, destination, budget_per_leg,
        min(r.total_cost for r in candidates),
    )
    return best


def resolve_intercity_chain(
    ctx: IntercityStrategyContext,
    strategies: List[IntercityStrategy],
) -> Optional[IntercityRoute]:
    """跑策略链：按顺序执行，首个产出非空候选的策略胜出，预算回落统一处理。

    - 策略异常 → 记 warning 继续下一策略（现状 try/except 回落语义：
      候选生成失败不阻断老链路；除 DirectStrategy 正常路径不抛异常外，
      通用保护只加更稳不改正常行为）；
    - 全部无解 → None（现状：direct 缺失且 BFS 无解）。
    """
    for strategy in strategies:
        try:
            candidates = strategy.resolve(ctx)
        except Exception as exc:  # noqa: BLE001  单策略失败不阻断链
            logger.warning(
                "城际策略 %s 异常，继续下一策略：%s",
                type(strategy).__name__, exc,
            )
            continue
        if not candidates:
            continue
        return apply_budget_fallback(
            candidates, ctx.budget_per_leg, ctx.origin, ctx.destination,
            scenario=getattr(strategy, "budget_scenario", "candidates"),
        )
    return None


# ---------------------------------------------------------------------------
# 策略实现（P2 子任务迁移：DirectStrategy / AirRailStrategy / GraphBfsStrategy
# / DirectFallbackStrategy）。迁移完成前由 travel._resolve_intercity_route 调用，
# 迁移后 travel.py 改走 resolve_intercity_chain。
# ---------------------------------------------------------------------------


class DirectStrategy:
    """直达策略（现状级联第 1 级）：provider 真源 + 本地 options 按偏好选方式。

    完整耗时（air 含值机缓冲）≤ 12h → 单段候选（超预算回落由链后处理器按
    ``direct`` 场景处理：仅 warn 不回落）；超 12h / 不存在 → 返回空列表
    （交下一策略），并把 ``direct`` / ``direct_minutes`` 写回 ctx 供共享。
    """

    budget_scenario = "direct"

    def resolve(self, ctx: IntercityStrategyContext) -> List[IntercityRoute]:
        direct = find_city_travel_preferred(
            ctx.origin, ctx.destination,
            options=ctx.options, provider=ctx.provider, priority=ctx.priority,
        )
        ctx.direct = direct
        ctx.direct_minutes = (
            direct.transport_minutes
            + (AIR_BUFFER_MIN if direct.mode == Mode.AIR.value else 0)
            if direct is not None
            else None
        )
        if direct is not None and ctx.direct_minutes <= DEFAULT_MAX_TOTAL_MINUTES:
            return [
                IntercityRoute((direct,), ctx.direct_minutes, direct.cost_per_person)
            ]
        return []


class DirectFallbackStrategy:
    """直达兜底策略（现状级联第 4 级）：BFS 无解 → 直达如实给出。

    即使超 12h 也返回（如驾驶 19h 的表外边——它是被迫选项不是优选，
    8.30 起不再参与软直达优先基准）。无 direct → 空列表。

    与 ``GraphBfsStrategy`` 的关系：``find_intercity_route``（BFS）内部自带
    direct 兜底，本策略是链尾双保险（现状第 4 级同样如此，行为等价）。
    """

    budget_scenario = "none"

    def resolve(self, ctx: IntercityStrategyContext) -> List[IntercityRoute]:
        if ctx.direct is None:
            return []
        return [
            IntercityRoute(
                (ctx.direct,),
                ctx.direct_minutes or 0,
                ctx.direct.cost_per_person,
            )
        ]


class AirRailStrategy:
    """空铁联运策略（现状级联第 2 级）：航空拓扑正反向邻居 + 免费铁路过滤。

    - 铁路走 provider 的 **unbudgeted 通道**（候选生成器有自带的总量纪律
      MAX_TRAIN_CALLS，与主链路共享 per-mode 预算会互相饿死——8.30 demo1
      返程实测）。旧 provider 无此通道 → 回退 ``mode='train'``（兼容纯 Mock
      测试）；
    - 候选生成后对 top 候选航段做 juhe 真价验证（``verify_flight_legs``，
      真价覆盖拓扑提示、无航班淘汰、故障保持 estimated）；
    - 候选优于「被迫直达」（超 12h 的 driving 之类才值得替换；直达缺失时
      任何候选都更好）→ 返回候选列表（预算回落由链后处理器按
      ``candidates`` 场景处理）；否则返回空列表交 BFS——与现状级联第 2 级
      ``if direct_minutes is None or best.total_minutes < direct_minutes``
      的 else 分支等价（不 return，落到老 BFS）。
    """

    budget_scenario = "candidates"

    def resolve(self, ctx: IntercityStrategyContext) -> List[IntercityRoute]:
        if ctx.provider is None:
            return []
        from data_transmission.intercity_candidates import (
            generate_intercity_candidates,
            verify_flight_legs,
        )

        try:
            unbudgeted = getattr(ctx.provider, "train_edge_unbudgeted", None)
            if callable(unbudgeted) and ctx.date_str:
                train_query = lambda a, b: unbudgeted(a, b, ctx.date_str)  # noqa: E731
            else:
                train_query = lambda a, b: ctx.provider(  # noqa: E731
                    a, b, mode=Mode.TRAIN.value
                )
            candidates = generate_intercity_candidates(
                ctx.origin, ctx.destination,
                date_str=ctx.date_str,
                train_provider=train_query,
                priority=ctx.priority,
            )
            if not candidates:
                return []
            # Day 3 提前（8.30）：top 候选航段 juhe 真价验证（额度 ≤4 城市对，
            # 复用 provider air 分支的 per-mode 预算 ≤6 双重保护）。
            try:
                candidates = verify_flight_legs(
                    candidates,
                    lambda a, b: ctx.provider(a, b, mode=Mode.AIR.value),
                )
            except Exception as exc:  # noqa: BLE001  验证失败不阻断候选
                logger.warning("航段真价验证异常，保持拓扑档：%s", exc)
            if not candidates:
                logger.info(
                    "联运候选全部被真源证伪，回落老 BFS：%s",
                    ctx.pair,
                )
                return []
            best = candidates[0]
            if ctx.direct_minutes is None or best.total_minutes < ctx.direct_minutes:
                return candidates  # 预算回落由链后处理器按 candidates 场景处理
            return []  # 候选不优于被迫直达 → 交老 BFS
        except Exception as exc:  # noqa: BLE001  候选生成失败不阻断老链路
            logger.warning("空铁候选生成失败，回落老 BFS：%s", exc)
            return []


class GraphBfsStrategy:
    """全图 BFS 策略（现状级联第 3 级）：估算表邻接 + 段级真源升级兜底。

    ``find_intercity_route`` 内部已含 direct 兜底（BFS 无解 → 直达如实给出），
    provider（live）场景的直达由 DirectStrategy 先行权衡、经 ctx 传入。
    无预算处理后处理（``budget_scenario="none"``，现状 BFS 段无预算约束）。
    """

    budget_scenario = "none"

    def resolve(self, ctx: IntercityStrategyContext) -> List[IntercityRoute]:
        route = find_intercity_route(
            ctx.origin, ctx.destination,
            options=ctx.options, priority=ctx.priority,
            provider=ctx.provider, direct=ctx.direct,
        )
        return [route] if route is not None else []