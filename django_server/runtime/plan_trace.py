"""规划轨迹采集器（plan trace v1，2026-09-16）。

v1 双通道交付（协议见 docs/sync_notes_plan_trace_20260916.md）：
- POST /api/plan/（及 /api/chat/）响应顶层新增 ``trace`` 字段（完成后回放）；
- GET /api/plan-trace/（无锁）等待期准实时轮询（running → done → idle）。

设计铁律：**观测旁路，绝不影响规划主链路**——Recorder 全部公开方法内部
兜底 try/except，任何采集失败只记 warning，规划照常。

步骤 schema（version=1）::

    {seq, t(ms 相对起点), phase, kind(llm|tool|milestone), title,
     tool?|model?, args(脱敏截断), result_digest(≤200字符),
     elapsed_ms, status(ok|error|no_data), source?, error?}

phase 枚举（对齐 C 端图标协议）：
parse🔍需求解析 / data🔎真源查询 / llm🤖模型调用 / review⚖️审查 /
plan⚙️算法编排 / enrich🚄交通增强 / final🔨落锤
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Dict, List, Optional

logger = logging.getLogger("runtime.plan_trace")

TRACE_VERSION = 1
MAX_STEPS = 200          # 单次轨迹步骤封顶（防异常调用打爆内存/响应体）
DIGEST_LIMIT = 200       # result_digest / 摘要截断长度
ARGS_LIMIT = 200         # 参数概要截断长度（与 _brief_args 同口径）

# 参数脱敏黑名单（规范源，2026-09-16 从 agent_runtime 迁入）：这些键的值是
# 用户数据（如 booking 工具的 tel 联系电话），不进日志也不进 trace；其余
# 键值（city/place 等排障关键值）保留。agent_runtime._brief_args 反向引用本清单。
SENSITIVE_ARG_KEYS = {
    "tel", "phone", "mobile", "password", "passwd", "token",
    "secret", "id_card", "email",
}

_ALLOWED_STATUS = {"ok", "error", "no_data"}


def sanitize_args(kwargs: Any) -> str:
    """工具参数概要：敏感键脱敏 + JSON 序列化 + 截断（语义同旧 _brief_args）。"""
    if not isinstance(kwargs, dict):
        return str(kwargs)[:ARGS_LIMIT]
    safe = {
        k: ("***" if str(k).lower() in SENSITIVE_ARG_KEYS else v)
        for k, v in kwargs.items()
    }
    try:
        s = json.dumps(safe, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        s = str(safe)
    return s if len(s) <= ARGS_LIMIT else s[:ARGS_LIMIT] + "…"


def _brief(text: Any, limit: int = DIGEST_LIMIT) -> str:
    s = str(text or "").strip()
    return s if len(s) <= limit else s[:limit] + "…"


# ── result_digest：按工具分派的关键事实摘要（结构化摘要而非原始回声）─────

_LIST_KEYS = (
    "trips", "trains", "tickets", "flights", "records", "list",
    "items", "results", "routes", "hotels", "spots", "days",
)
_PRICE_KEYS = (
    "price", "min_price", "lowest_price", "second_class_price",
    "price_economy", "average_cost", "guide_price",
)


def _records_of(data: Any) -> Optional[list]:
    """从工具返回里找记录列表（list 本身或常见包裹键）。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in _LIST_KEYS:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


def _price_of(rec: Any) -> Optional[float]:
    if not isinstance(rec, dict):
        return None
    for key in _PRICE_KEYS:
        value = rec.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return float(value)
    return None


def _range_digest(recs: list, unit: str = "元") -> str:
    prices = [p for rec in recs[:50] if (p := _price_of(rec)) is not None]
    if not prices:
        return ""
    lo, hi = min(prices), max(prices)
    if lo == hi:
        return f"价格 {lo:g}{unit}"
    return f"价格 {lo:g}~{hi:g}{unit}"


def _count_digest(recs: list, noun: str) -> str:
    out = f"共 {len(recs)} {noun}"
    extra = _range_digest(recs)
    if extra:
        out += f"，{extra}"
    return out


def _generic_digest(data: Any) -> str:
    recs = _records_of(data)
    if recs is not None:
        return _count_digest(recs, "条记录")
    if isinstance(data, dict):
        keys = [str(k) for k in list(data.keys())[:6]]
        suffix = f" 等 {len(data)} 个字段" if len(data) > 6 else ""
        return f"返回字段：{'、'.join(keys)}{suffix}" if keys else "空对象"
    return _brief(data, 100)


def _train_digest(data: Any) -> str:
    recs = _records_of(data)
    if recs:
        return _count_digest(recs, "个班次")
    return _generic_digest(data)


def _flight_digest(data: Any) -> str:
    recs = _records_of(data)
    if recs:
        return _count_digest(recs, "个航班")
    return _generic_digest(data)


def _hotel_digest(data: Any) -> str:
    recs = _records_of(data)
    if recs:
        return _count_digest(recs, "家酒店")
    return _generic_digest(data)


def _weather_digest(data: Any) -> str:
    if isinstance(data, dict):
        bits = []
        for key in ("weather", "text", "desc", "description", "condition"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                bits.append(value.strip())
                break
        temps = [
            float(data[k]) for k in ("temp_min", "temp_max", "temperature")
            if isinstance(data.get(k), (int, float))
        ]
        if len(temps) >= 2:
            bits.append(f"{min(temps):g}~{max(temps):g}℃")
        if bits:
            return " ".join(bits)
    return _generic_digest(data)


def _map_digest(data: Any) -> str:
    if isinstance(data, dict):
        bits = []
        for key, unit in (("distance", "km"), ("duration", "min")):
            value = data.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                bits.append(f"{value:g}{unit}")
        if bits:
            return " / ".join(bits)
    return _generic_digest(data)


_DIGESTERS = {
    "train_trip": _train_digest,
    "train_ticket": _train_digest,
    "flight_search": _flight_digest,
    "hotel": _hotel_digest,
    "weather": _weather_digest,
    "weather_brief": _weather_digest,
    "weather_forecast": _weather_digest,
    "map": _map_digest,
}


def result_digest_for(tool: str, data: Any) -> str:
    """工具返回 → ≤200 字符关键事实（未命中分派走通用兜底）。"""
    try:
        digester = _DIGESTERS.get(tool, _generic_digest)
        return _brief(digester(data))
    except Exception:  # noqa: BLE001  摘要失败不影响主链路
        return ""


# ── Recorder ─────────────────────────────────────────────────────────────


class PlanTraceRecorder:
    """单次规划/对话请求的轨迹缓冲。

    生命周期 = 一次 POST 请求：视图入口创建；持 ``runtime.lock`` 期间挂在
    ``rt.current_trace_recorder`` 供 logged_call（含 enrich 线程池并发）写入；
    结束时 ``assemble()`` 装配 trace dict → 写响应 + 存 ``rt.last_plan_trace``。
    全部写入方法内部兜底，绝不向调用方抛异常。
    """

    def __init__(self, request_id: str = "") -> None:
        self._lock = threading.Lock()
        self._t0 = time.monotonic()
        self._request_id = request_id
        self._phase = "parse"
        self._steps: List[Dict[str, Any]] = []

    # -- 写入 ----------------------------------------------------------

    def set_phase(self, phase: str) -> None:
        """切换后续工具步骤的 phase 归类（如进 enrich 前置为 enrich）。"""
        try:
            with self._lock:
                self._phase = phase
        except Exception:  # noqa: BLE001
            pass

    def add_tool(
        self,
        name: str,
        arguments: Any,
        status: str,
        source: Optional[str] = None,
        elapsed_ms: Optional[int] = None,
        error: Optional[str] = None,
        data: Any = None,
        phase: Optional[str] = None,
    ) -> None:
        """工具调用步骤（logged_call 接线；data 仅只读工具携带，用于 digest）。

        phase 缺省取 recorder 当前 phase；LLM 片段展开（extend_from_llm_meta）
        显式传 "data"（模型轮内的真源查询语义）。
        """
        try:
            step: Dict[str, Any] = {
                "kind": "tool",
                "phase": phase or self._current_phase(),
                "title": f"调用 {name}",
                "tool": name,
                "args": sanitize_args(arguments),
                "result_digest": result_digest_for(name, data) if data is not None else "",
                "status": status if status in _ALLOWED_STATUS else "ok",
                "elapsed_ms": elapsed_ms,
            }
            if source:
                step["source"] = source
            if error:
                step["error"] = _brief(error)
            self._append(step)
        except Exception:  # noqa: BLE001
            logger.warning("plan trace: add_tool failed", exc_info=True)

    def add_llm_step(
        self,
        title: str,
        phase: str = "llm",
        summary: str = "",
        model: Optional[str] = None,
        elapsed_ms: Optional[int] = None,
        status: str = "ok",
    ) -> None:
        try:
            step: Dict[str, Any] = {
                "kind": "llm",
                "phase": phase,
                "title": title,
                "result_digest": _brief(summary),
                "status": status if status in _ALLOWED_STATUS else "ok",
                "elapsed_ms": elapsed_ms,
            }
            if model:
                step["model"] = model
            self._append(step)
        except Exception:  # noqa: BLE001
            logger.warning("plan trace: add_llm_step failed", exc_info=True)

    def add_milestone(
        self,
        title: str,
        phase: str = "plan",
        detail: str = "",
        elapsed_ms: Optional[int] = None,
    ) -> None:
        try:
            step: Dict[str, Any] = {
                "kind": "milestone",
                "phase": phase,
                "title": title,
                "status": "ok",
                "elapsed_ms": elapsed_ms,
            }
            if detail:
                step["result_digest"] = _brief(detail)
            self._append(step)
        except Exception:  # noqa: BLE001
            logger.warning("plan trace: add_milestone failed", exc_info=True)

    def extend_from_steps(
        self, steps: Sequence[Dict[str, Any]], phase: str = "plan"
    ) -> None:
        """外部步骤流并入（2026-09-16 agent_trace 归一：编排器轨迹单通道接入）。

        ``steps`` 由 A 侧 ``call_llm.agent_trace.to_plan_trace_steps`` 产出
        （字段与本 recorder 步骤 schema 对齐：seq/t 由 _append 统一分配）。
        异常只降级不影响规划。
        """
        try:
            for step in steps or []:
                if not isinstance(step, dict):
                    continue
                # _json_safe 防御性归一：外部步骤可能携带非 JSON-safe 对象
                # （如 ToolResult dataclass），在此兜底序列化
                merged = _json_safe({**step, "phase": step.get("phase") or phase})
                self._append(merged if isinstance(merged, dict)
                             else {"title": "编排步", "phase": phase})
        except Exception:  # noqa: BLE001
            logger.warning("plan trace: extend_from_steps failed", exc_info=True)

    def extend_from_llm_meta(
        self,
        llm_meta: Any,
        phase: str = "llm",
        title: str = "模型调用",
        main_step: bool = True,
    ) -> None:
        """把 generate 结果片段展开为步骤序列。

        片段来源（schema 见同步文档「A 侧透传约定」）：
        - A 侧透传 ``BPlannerHook.last_llm_trace``（候选池 LLM）；
        - B 侧自留的 parse / chat 片段（``_parse_free_text_requirement`` /
          ``views.chat`` 从 generate 返回 dict 提取）。

        main_step=False 用于调用方已手工记过主 LLM 步骤（如 chat）时只补
        工具轮与审查步。
        """
        if not isinstance(llm_meta, dict):
            return
        try:
            if main_step:
                self.add_llm_step(
                    title,
                    phase=phase,
                    summary=str(llm_meta.get("content_summary")
                                or llm_meta.get("content") or ""),
                    model=llm_meta.get("model"),
                    elapsed_ms=llm_meta.get("elapsed_ms"),
                )
            for round_entry in llm_meta.get("tool_trace") or []:
                if not isinstance(round_entry, dict):
                    continue
                for call in round_entry.get("calls") or []:
                    if not isinstance(call, dict):
                        continue
                    name = str(call.get("name") or "unknown")
                    result = call.get("result")
                    status = "ok"
                    if isinstance(result, dict):
                        raw_status = str(result.get("status") or "")
                        if raw_status in _ALLOWED_STATUS:
                            status = raw_status
                    self.add_tool(
                        name,
                        call.get("arguments"),
                        status,
                        source="llm_round",
                        data=result,
                        phase="data",
                    )
            for review in llm_meta.get("reviews") or []:
                if not isinstance(review, dict):
                    continue
                verdict = str(review.get("review") or "unknown")
                self.add_milestone(
                    f"审查 · 第{review.get('round')}轮（{verdict}）",
                    phase="review",
                    detail=str(review.get("reason") or ""),
                )
        except Exception:  # noqa: BLE001
            logger.warning("plan trace: extend_from_llm_meta failed", exc_info=True)

    # -- 读出 ----------------------------------------------------------

    def progress(self) -> Dict[str, Any]:
        """等待期轮询快照（GET /api/plan-trace/ 的 running 分支）。"""
        with self._lock:
            steps = [dict(step) for step in self._steps]
            phase = self._phase
        return {
            "steps": steps,
            "phase": phase,
            "elapsed_ms": self._elapsed_ms(),
        }

    def assemble(self, plan_meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """装配最终 trace dict（写响应 + 存 rt.last_plan_trace 的形态）。"""
        with self._lock:
            steps = [dict(step) for step in self._steps]
        trace = {
            "version": TRACE_VERSION,
            "request_id": self._request_id,
            "total_elapsed_ms": self._elapsed_ms(),
            "plan_meta": plan_meta or {},
            "steps": steps,
        }
        return _json_safe(trace)

    # -- 内部 ----------------------------------------------------------

    def _current_phase(self) -> str:
        with self._lock:
            return self._phase

    def _elapsed_ms(self) -> int:
        return int((time.monotonic() - self._t0) * 1000)

    def _append(self, step: Dict[str, Any]) -> None:
        with self._lock:
            if len(self._steps) >= MAX_STEPS:
                return
            step["seq"] = len(self._steps) + 1
            step.setdefault("t", self._elapsed_ms())
            self._steps.append(step)


def _json_safe(obj: Any) -> Any:
    """整体 JSON 归一化（对齐 manager.persist 的 _json_default 思路）。"""
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:  # noqa: BLE001
        return {"version": TRACE_VERSION, "steps": [], "plan_meta": {}}
