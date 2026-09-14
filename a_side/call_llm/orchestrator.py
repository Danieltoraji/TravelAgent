"""PlanOrchestrator：LLM 主导的全流程规划编排器（智能体编排方案 阶段 a）。

用户拍板（2026-09-07）：整条规划流程改成 **LLM 主导的 agentic 编排**——LLM
作为指挥官分解任务、按依赖顺序调 tool（城际真源 → 定中心/选景点 → 酒店 →
餐饮 → 排程器），审查每步结果，不满意就定向修复并重调，迭代到达标或预算
耗尽。铁律（方案 §4 红线）：**LLM 是指挥官，确定性工具是执行部队**——
排程/背包/预算由 ``schedule_plan``（``call_llm/schedule_tool.py``）落锤，
班次/票价/坐标来自 B 真源工具，LLM 只选参数、挑候选、判质量。

工作台状态 S（方案 §1）：需求 / 候选池 / 中心计划 / 时刻窗（城际骨架落锤）/
草案计划 / 质量报告历史（每轮追加，全程可解释）。持有在本实例上，
``tool_executor`` 闭包按名字分派读写：

- ``schedule_plan`` → 本地排程器（``schedule_tool.run_schedule_plan``）；
  落锤后立刻算确定性质量信号（``quality_signals.compute_quality_signals``）
  随结果回填——LLM「读信号 → 决定下一轮改什么」的依据；
- B 真源工具（白名单城际三件套 + hotel/food）→ ``QuotaManager.cached_call``
  （per-mode 预算 + 缓存 + 节律，与 PlannerAgent 同款）；``LiveDataError``
  （含 ``QuotaExceeded``）接成结构化 error 回填（错误被回路消费）；
- 白名单之外一律拒绝（``unknown_tool`` 结构化 error，方案红线 4 工具白名单）。

门控：``env USE_LLM_ORCHESTRATOR in (1, true, yes)`` 且注入候选池 ``spots``
才真正起循环；**默认关**——``run()`` 直接返回回落结果（``tools_enabled=False``
+ ``fallback_reason``），调用方（阶段 b 的 BPlannerHook 接线）回退固定管线。
编排循环任何异常（轮数超限 / LLM 失败 / JSON 不合法）→ 返回
``fallback_reason`` 的回落结果，绝不向上抛——「回落路径必须真回落」。

阶段 a 边界：本模块**只做编排循环与工作台**，不接 ``BPlannerHook``（阶段 b
门控接线）、不做城际站对中心化选择（阶段 b）、不做 M1 提名（阶段 c）。
返回的 ``plan`` 是 A 侧排程 dict（``plan_multi_day`` 形状）；TripTimeline
转换仍由调用方走 ``plan_to_trip_timeline``（阶段 b 收口，口径与现管线一致）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional

from call_llm.client_factory import create_llm_client
from call_llm.quality_signals import compute_quality_signals
from call_llm.schedule_tool import (
    SCHEDULE_TOOL_NAME,
    SCHEDULE_TOOL_SCHEMA,
    run_schedule_plan,
    schedule_tool_ctx,
)
from data_transmission.live_errors import LiveDataError
from data_transmission.tool_specs import intercity_mode_budget, to_openai_tools

# 编排默认暴露的 B 真源工具白名单（方案 §2；scenic 不入内——候选池由上游
# ScenicSearchPlanner+LiveSpotsSource 构建）。map 入白名单（阶段 b）：LLM 用它
# 实测「站↔中心/酒店」驾车分钟后再 prefer_station 提名站对（LLM 提名 →
# 真源验证红线）。
ORCHESTRATOR_B_TOOLS = ("train_trip", "train_ticket", "flight_search", "hotel", "food", "map")

ORCHESTRATOR_SYSTEM = (
    "你是旅行规划的总指挥（编排器）。候选池、时刻窗（城际骨架落锤）已经就绪，"
    "你的工作是按依赖顺序调工具迭代出一份达标行程：\n"
    "1. 需要核验城际班次/酒店/餐饮事实时，调用对应真源工具（结果真实可信，"
    "不要编造）；\n"
    "2. 主循环是调 schedule_plan（确定性排程器）出草案：改参数（min_spots / "
    "allocator / center_plan 按日中心）→ 读返回的质量信号 → 决定下一轮改什么。"
    "多日游可先按空间片区给 center_plan（如远郊景区簇几天、市区簇几天），"
    "信号差就调整中心或参数重排；\n"
    "3. 红线：你绝不自己生成任何时刻表或金额——时间与费用只来自工具返回；"
    "同一参数不要原样重复调用（同参结果相同）；\n"
    "4. 质量信号（feasible/预算/单景点天/必去缺口等）由规则计算，你负责权衡："
    "可行性硬伤必须修，权衡类信号（利用率/费用占比）按需求取舍；"
    "注意 schedule_plan 返回的费用是编排期下界（住宿/城际/餐饮尚未挂载，"
    "0 为预期，收尾阶段实价 true-up），勿据此判「不自洽」；\n"
    "5. 达标或工具预算告罄时收尾：accepted=true 表示接受当前草案（或如实"
    "accepted=false 说明无法达标）。只输出符合给定 JSON Schema 的 JSON。"
)

ORCHESTRATOR_RESPONSE_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "accepted": {
            "type": "boolean",
            "description": "是否接受当前草案（schedule_plan 最近一次的 plan）",
        },
        "summary": {
            "type": "string",
            "description": "一段话总结最终行程（或无法达标的原因）",
        },
        "reasons": {
            "type": "array",
            "items": {"type": "string"},
            "description": "接受/拒绝的依据（引用质量信号与工具真实返回）",
        },
    },
    "required": ["accepted", "summary", "reasons"],
}

# 审查轮（P5.5 横切机制复用）：每批工具结果后强制常识审查，可疑即重调。
ORCHESTRATOR_REVIEW_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "review": {"type": "string", "enum": ["ok", "suspicious"]},
        "reason": {"type": "string", "description": "一句审查理由"},
    },
    "required": ["review", "reason"],
}

# 本地工具：偏好站对提名（阶段 b，中心先行拍板落地——「站对按距中心/酒店近
# 优选」的选择口径）。LLM 用 map 实测后提名；确定性选择（返程组合选择/到达
# 站精修）在可行集合内带 90min 容忍度尊重该偏好，硬约束不变（红线：时刻/
# 接续仍由确定性工具落锤，偏好只重排可行解）。
PREFER_STATION_TOOL_NAME = "prefer_station"
_PREFER_STATION_DIRECTIONS = ("outbound_arrival", "return_departure")

PREFER_STATION_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": PREFER_STATION_TOOL_NAME,
        "description": (
            "提名偏好站对（先调 map 实测「站↔中心/酒店」驾车分钟再提名）。"
            "确定性选择会在可行班次集合内尊重该偏好（90 分钟容忍度）："
            "非偏好站的班次要早 90 分钟以上才会被选中。不提名 = 按时刻/费用默认口径。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "direction": {
                    "type": "string",
                    "enum": list(_PREFER_STATION_DIRECTIONS),
                    "description": "outbound_arrival=去程到达站（靠近首日中心/酒店）"
                    "；return_departure=返程出发站（靠近末日中心/酒店）",
                },
                "station": {
                    "type": "string",
                    "description": "站名/机场名（来自真源候选里真实存在的站）",
                },
                "reason": {
                    "type": "string",
                    "description": "一句依据（须引用 map 实测分钟或班次真实返回）",
                },
            },
            "required": ["direction", "station", "reason"],
            "additionalProperties": False,
        },
    },
}


# 零工具调用退回重问（阶段 b 在线实测 2026-09-14：响应 schema 可被模型直接
# 满足，flash 档模型会跳过工具直接收尾 JSON——红线「LLM 只能经工具行事」要求
# 至少落锤一次排程；退回重问一次，再不调则按回落处理）。
NO_TOOL_REPROMPT = (
    "你还没有调用过任何工具就给出了结论，这不符合编排纪律——时刻/费用/可行性"
    "必须由 schedule_plan 落锤，不允许凭空收尾。请立即调用 schedule_plan（参数"
    "按候选池与必去给出），拿到草案与质量信号后再决定接受或继续调整，最后按"
    " schema 输出。"
)

def use_llm_orchestrator() -> bool:
    """编排门控（默认关，与 ``decision_engine._use_llm_tools`` 同款 env 语义）。"""
    return os.environ.get("USE_LLM_ORCHESTRATOR", "").strip().lower() in (
        "1", "true", "yes",
    )


def _fallback_result(reason: str, **extra: Any) -> Dict[str, Any]:
    """回落结果（方案 §1 退出兜底：调用方据此回退固定管线，绝不向上抛）。"""
    base = {
        "plan": None,
        "accepted": False,
        "summary": "",
        "reasons": [],
        "quality_history": [],
        "tool_rounds": 0,
        "schedule_calls": 0,
        "tools_enabled": False,
        "tools_degraded": False,
        "reviews": [],
        "uncertain": False,
        "preferred_stations": {},
        "fallback_reason": reason,
    }
    base.update(extra)
    return base


class PlanOrchestrator:
    """LLM 编排循环执行器（工作台状态 S 持有者）。

    使用方法::

        orch = PlanOrchestrator(
            requirement=req,
            spots=(must, conflict, scored),          # A 侧三组候选池
            planner_ctx={"center_schedule": ...,     # 规则定界后的按日中心
                         "first_day_start_time": ..., # 城际骨架落锤的时刻窗
                         "last_day_end_minutes": ...},
            tool_provider=tool_provider,             # B 侧工具门面（可选）
        )
        result = orch.run()
        # -> {plan, accepted, summary, reasons, quality_history, schedule_calls,
        #     tool_rounds, tools_enabled, fallback_reason, ...}

    ``planner_ctx`` 可选键：``planner_fn``（测试注入假排程器）/
    ``travel_time_provider`` / ``restaurants`` / ``center_schedule`` /
    ``first_day_start_time`` / ``last_day_end_minutes``——后两者是城际骨架
    落锤的产物，**不对 LLM 开放**（红线 1）。
    """

    def __init__(
        self,
        requirement: Dict[str, Any],
        *,
        spots: Any = None,
        planner_ctx: Optional[Dict[str, Any]] = None,
        tool_provider: Any = None,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
        max_tool_rounds: int = 8,
        max_schedule_calls: int = 4,
        review_enabled: bool = True,
        b_tools: Optional[tuple] = None,
    ):
        self.requirement = requirement if isinstance(requirement, dict) else {}
        self.spots = spots
        self._planner_ctx = dict(planner_ctx or {})
        self.tool_provider = tool_provider
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_tool_rounds = max_tool_rounds
        self.max_schedule_calls = max_schedule_calls
        self.review_enabled = review_enabled
        self.b_tools = tuple(b_tools) if b_tools is not None else ORCHESTRATOR_B_TOOLS

        # 工作台状态 S（跨轮演化：LLM 改 center_plan 会更新中心计划）
        self._center_schedule: Optional[list] = (
            self._planner_ctx.get("center_schedule") or None
        )
        self._draft_plan: Optional[Dict[str, Any]] = None
        self._quality_history: List[Dict[str, Any]] = []
        self._schedule_counter: Dict[str, int] = {}
        # 偏好站对（阶段 b 中心先行）：LLM prefer_station 提名 → 收尾链确定性
        # 选择在可行集合内尊重（90min 容忍度）
        self._preferred_stations: Dict[str, str] = {}

        # B 真源走 QuotaManager（per-mode 预算 + 同参缓存 + 节律）；无 provider
        # 时 B 工具调用返回结构化 error（schedule_plan 是本地的，不受影响）。
        self._quota: Optional[Any] = None
        if tool_provider is not None:
            from data_transmission.quota_manager import make_quota_manager

            self._tool_cache: Dict[Any, Any] = {}
            # per-mode 预算取城际三真源默认（各 6，ToolSpec 注册表单一来源）；
            # hotel/food 未配预算 = 计数不限流（stats 仍是全量真源账本）。
            self._quota = make_quota_manager(
                tool_provider,
                mode_budget=intercity_mode_budget(),
                cache=self._tool_cache,
            )

    # -- 门控与工作台摘要 ---------------------------------------------------

    @property
    def tools_enabled(self) -> bool:
        """编排循环可用：env 门控开（默认关）且候选池就绪。"""
        return use_llm_orchestrator() and self.spots is not None

    def _pool_digest(self) -> str:
        """候选池摘要（名字分组；坐标/评分细节不进 prompt，token 可控）。"""
        if not self.spots:
            return "（空）"
        group_names = ("必去", "冲突待定", "评分候选")
        lines = []
        for i, group in enumerate(self.spots):
            label = group_names[i] if i < len(group_names) else f"组{i}"
            names = [
                spot.get("name") or ""
                for spot in (group or [])
                if isinstance(spot, dict)
            ]
            lines.append(f"- {label}：{'、'.join(n for n in names if n) or '（空）'}")
        return "\n".join(lines)

    def _workbench_digest(self) -> str:
        """工作台状态 S → user 提示词（需求/池/中心/时刻窗）。"""
        content = self.requirement.get("content") or {}
        constraints = content.get("constraints") or {}
        windows = []
        if self._planner_ctx.get("first_day_start_time"):
            windows.append(
                f"Day1 起点 {self._planner_ctx['first_day_start_time']}"
                "（城际到达+接驳，已定不可改）"
            )
        if self._planner_ctx.get("last_day_end_minutes") is not None:
            windows.append(
                f"末日游玩截止 {self._planner_ctx['last_day_end_minutes']} 分钟"
                "（返程出发-缓冲，已定不可改）"
            )
        center_desc = (
            "、".join(
                f"D{c.get('day_from')}-{c.get('day_to')}:{(c.get('center') or {}).get('poi') or '市中心'}"
                for c in self._center_schedule
            )
            if self._center_schedule
            else "未定（可用 center_plan 指定）"
        )
        return (
            f"目的地：{content.get('destination') or '未指定'}；"
            f"天数：{content.get('days') or '?'}；"
            f"预算：{constraints.get('budget') or '不限'} 元；"
            f"必去：{constraints.get('must_visit') or []}\n"
            f"候选池：\n{self._pool_digest()}\n"
            f"当前按日中心计划：{center_desc}\n"
            f"时刻窗：{'；'.join(windows) or '无城际窗口（纯目的地游）'}\n"
            "请按系统指令编排：需要事实先查真源，主循环调 schedule_plan 迭代，"
            "达标后按 schema 收尾。"
        )

    # -- 工具分派 -----------------------------------------------------------

    def _tool_executor(self, name: str, arguments: Dict[str, Any]) -> Any:
        """统一工具门面：本地排程器 / 偏好站对 / B 真源白名单 / 拒绝名单外工具。"""
        if name == SCHEDULE_TOOL_NAME:
            return self._run_schedule(arguments)
        if name == PREFER_STATION_TOOL_NAME:
            return self._run_prefer_station(arguments)
        if name in self.b_tools:
            if self._quota is None:
                return {
                    "status": "error",
                    "error": "no_provider",
                    "detail": f"{name}: 未注入 B 侧工具门面，无法查真源",
                }
            try:
                return self._quota.cached_call(name, **(arguments or {}))
            except LiveDataError as exc:
                return {
                    "status": "error",
                    "error": type(exc).__name__,  # quota_exceeded 等机器可读
                    "detail": f"{name}: {exc}",
                }
        return {
            "status": "error",
            "error": "unknown_tool",
            "detail": f"{name} 不在编排白名单内（可用：{SCHEDULE_TOOL_NAME} + {sorted(self.b_tools)}）",
        }

    def _run_schedule(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """schedule_plan 分派：中心计划规则定界 → 排程落锤 → 质量信号回填。"""
        arguments = arguments if isinstance(arguments, dict) else {}
        note = None
        center_plan = arguments.get("center_plan")
        if isinstance(center_plan, list) and center_plan:
            # 规则定界（P5.7 拍板：LLM 切片、规则定上下界）复用
            # scenic_search_planner 的校验器；LLM 显式给了 center_plan 但全部
            # 簇非法时保底默认市中心并标注（不静默吞掉 LLM 意图）。
            from call_llm.scenic_search_planner import (
                _DEFAULT_CENTER,
                _validate_center_schedule,
            )

            content = self.requirement.get("content") or {}
            try:
                days = int(content.get("days") or 0)
            except (TypeError, ValueError):
                days = 0
            validated = _validate_center_schedule(
                {"center_schedule": center_plan}, days or 1
            )
            self._center_schedule = validated["center_schedule"]
            collapsed = (
                len(self._center_schedule) == 1
                and self._center_schedule[0]["center"] == dict(_DEFAULT_CENTER)
            )
            if collapsed:
                note = "center_plan 全部簇非法，已按规则回退默认市中心"
            # 更新工作台中心计划（下一轮 digest 可见，中心迭代闭环）

        ctx = schedule_tool_ctx(
            self.requirement,
            self.spots,
            planner_fn=self._planner_ctx.get("planner_fn"),
            travel_time_provider=self._planner_ctx.get("travel_time_provider"),
            restaurants=self._planner_ctx.get("restaurants"),
            first_day_start_time=self._planner_ctx.get("first_day_start_time"),
            last_day_end_minutes=self._planner_ctx.get("last_day_end_minutes"),
            center_schedule=self._center_schedule,
            max_calls=self.max_schedule_calls,
            calls=self._schedule_counter,
        )
        out = run_schedule_plan(ctx, arguments)
        if out.get("status") != "ok":
            return out

        # 落锤即入工作台：草案 + 确定性质量信号（每轮追加，全程可解释）
        self._draft_plan = out["plan"]
        signals = compute_quality_signals(out["plan"], self.requirement)
        self._quality_history.append({
            "round": len(self._quality_history) + 1,
            "params": {
                "min_spots": arguments.get("min_spots") or 0,
                "allocator": arguments.get("allocator") or "balanced",
                "center_plan": center_plan or None,
            },
            "signals": signals,
        })
        result = {"status": "ok", "summary": out["summary"], "quality_signals": signals}
        if note:
            result["note"] = note
        return result

    def _run_prefer_station(self, arguments: Dict[str, Any]) -> Dict[str, Any]:
        """prefer_station 分派：站对提名入工作台（编排器/收尾链消费）。

        校验 direction 枚举 + 站名非空 + reason 必填（「LLM 提名 → 真源验证」
        红线：reason 须引用 map 实测/班次真实返回；站名本身的可行性仍由确定性
        选择在候选集合内裁决——提一个候选里不存在的站不会生效，只会按默认
        口径选班）。
        """
        arguments = arguments if isinstance(arguments, dict) else {}
        direction = str(arguments.get("direction") or "")
        station = str(arguments.get("station") or "").strip()
        reason = str(arguments.get("reason") or "").strip()
        if direction not in _PREFER_STATION_DIRECTIONS:
            return {
                "status": "error",
                "error": "invalid_params",
                "detail": f"direction 必须是 {' / '.join(_PREFER_STATION_DIRECTIONS)}，收到 {direction!r}",
            }
        if not station:
            return {
                "status": "error",
                "error": "invalid_params",
                "detail": "station 不能为空（须为真源候选中真实存在的站名）",
            }
        if not reason:
            return {
                "status": "error",
                "error": "invalid_params",
                "detail": "reason 必填（引用 map 实测分钟或班次真实返回）",
            }
        self._preferred_stations[direction] = station
        return {
            "status": "ok",
            "preferred_stations": dict(self._preferred_stations),
            "note": (
                f"{direction} 偏好站 {station} 已记录：确定性选择将在可行班次集合内"
                "尊重该偏好（90 分钟容忍度），时刻/接续硬约束不变"
            ),
        }

    # -- 编排主循环 ---------------------------------------------------------

    def run(self) -> Dict[str, Any]:
        """起编排循环；门控关 / 异常一律返回回落结果（不抛异常）。"""
        if not self.tools_enabled:
            reason = (
                "USE_LLM_ORCHESTRATOR 未开启（默认关，走固定管线）"
                if not use_llm_orchestrator()
                else "候选池（spots）未就绪"
            )
            return _fallback_result(reason)

        client = create_llm_client(
            model_name=self.model_name,
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
            ask_user_if_missing=False,
            system_instruction=ORCHESTRATOR_SYSTEM,
            max_tokens=2000,
        )
        tools = [dict(SCHEDULE_TOOL_SCHEMA), dict(PREFER_STATION_TOOL_SCHEMA)]
        tools.extend(to_openai_tools(names=sorted(self.b_tools)))
        messages = [{"role": "user", "content": self._workbench_digest()}]
        try:
            # 零工具调用退回重问（红线：LLM 只能经工具行事）——响应 schema 可
            # 被模型直接满足，flash 档模型会跳过工具直接收尾；重问一次，仍不调
            # 工具则按该结果走回落（accepted 必为 False → 调用方回固定管线）。
            for attempt in range(2):
                result = client.generate(
                    messages=messages,
                    response_schema=ORCHESTRATOR_RESPONSE_SCHEMA,
                    tools=tools,
                    tool_executor=self._tool_executor,
                    max_tool_rounds=self.max_tool_rounds,
                    review_schema=ORCHESTRATOR_REVIEW_SCHEMA if self.review_enabled else None,
                )
                if (
                    self._schedule_counter.get(SCHEDULE_TOOL_NAME, 0) > 0
                    or result.get("tools_degraded")
                    or attempt == 1
                ):
                    break
                content = result.get("content") or {}
                messages = messages + [
                    {
                        "role": "assistant",
                        "content": json.dumps(content, ensure_ascii=False),
                    },
                    {"role": "user", "content": NO_TOOL_REPROMPT},
                ]
        except Exception as exc:  # noqa: BLE001  编排失败 → 回落固定管线（红线 4）
            return _fallback_result(
                f"编排循环失败，回落固定管线：{type(exc).__name__}: {exc}",
                quality_history=self._quality_history,
                schedule_calls=self._schedule_counter.get(SCHEDULE_TOOL_NAME, 0),
                tools_enabled=True,  # 循环确实启用过（失败≠未启用）
            )

        content = result.get("content") or {}
        return {
            "plan": self._draft_plan,
            "accepted": bool(content.get("accepted")),
            "summary": str(content.get("summary") or ""),
            "reasons": list(content.get("reasons") or []),
            "quality_history": self._quality_history,
            "tool_rounds": int(result.get("tool_rounds") or 0),
            "schedule_calls": self._schedule_counter.get(SCHEDULE_TOOL_NAME, 0),
            "tools_enabled": True,
            "tools_degraded": bool(result.get("tools_degraded")),
            "reviews": list(result.get("reviews") or []),
            "uncertain": bool(result.get("uncertain")),
            "preferred_stations": dict(self._preferred_stations),
            "fallback_reason": None,
        }
