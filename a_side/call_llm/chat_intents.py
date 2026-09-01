"""chat 修改意图的翻译器：模型输出的「修改意图」→ A 侧的事件 / 约束（纯函数，可离线测试）。

P5.1（chat 线回 A）：B 侧 ``update_timeline`` 从「模型输出整份新时间轴整体替换」
改为「模型输出修改意图 → A 翻译成事件/约束 → RePlanner 增量修复 / A 规划器
全量重排」。本模块是翻译层（纯函数，不触发网络、不修改输入）：

- ``remove``      删景点  → ``closed`` 事件（RePlanner 现成的「从池移除 + 增量修复」）；
- ``add``         加景点  → requirement 约束修改（``constraints.must_visit`` 追加），
  由编排层用新需求全量重排；
- ``reschedule``  改时段  → 时段锚点替换（``apply_reschedule_ops`` 在 A 计划 dict
  上直接挪动，编排层再转回 ``TripTimeline``）；
- 其它动作 / 字段缺失 → 记入 ``unsupported``，由编排层回给 LLM 调整重试。

意图 Schema（``CHAT_INTENTS_SCHEMA``）即 B 侧 ``update_timeline`` 工具的**新参数契约**
（tools 面的变化）；C 端请求/响应契约零变化（仍是 ``{message, history}`` →
``{reply, elapsed_ms}``，见方案 §六.7）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence, Tuple

# 意图动作枚举：v1 支持的三种（演示切片；reschedule 因 RePlanner 无「时段约束
# 事件」翻译、增量错峰无法指定目标时段，由编排层直接做时段锚点替换）
CHAT_ACTIONS = ("remove", "add", "reschedule")

# 单个修改意图的 JSON Schema（B 侧 update_timeline 工具参数 = {intents: [...]}）
_CHAT_INTENT_ITEM_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {
            "type": "string",
            "enum": list(CHAT_ACTIONS),
            "title": "修改动作",
            "description": (
                "remove=删除景点 / add=新增景点 / reschedule=调整到达时段"
            ),
        },
        "spot": {"type": "string", "title": "景点名称"},
        "day": {"type": "integer", "minimum": 1, "title": "目标天序号（从 1 起）"},
        "time": {
            "type": "string",
            "title": "目标到达时段",
            "description": "HH:MM 24 小时制，如 15:00",
        },
    },
    "required": ["action", "spot"],
}

CHAT_INTENTS_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "intents": {
            "type": "array",
            "items": _CHAT_INTENT_ITEM_SCHEMA,
            "title": "修改意图列表",
            "description": (
                "用户要求调整行程时输出的一次性修改意图；一次最多 5 条。"
                "只表达用户明确提出的改动，不要臆测。"
            ),
        },
    },
    "required": ["intents"],
}

# 时段合法性区间（与 B 侧 timeline_validator 同口径的兜底值；深度闭馆/预算
# 校验仍由 B 侧 validator 在最终时间轴上执行，见方案 P5.1）
_RESCHEDULE_EARLIEST = "09:00"
_RESCHEDULE_LATEST = "20:00"


def _hhmm_to_minutes(value: Any) -> Optional[int]:
    """HH:MM → 当天分钟数；解析失败（含 24:xx / xx:60 越界）返回 None。"""
    try:
        hh, mm = (str(value).split(":") + ["00"])[:2]
        h, m = int(hh), int(mm)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            return None
        return h * 60 + m
    except (ValueError, AttributeError):
        return None


def _minutes_to_hhmm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _node_start_minutes(node: Dict[str, Any]) -> Optional[int]:
    """从 A 计划节点取到达分钟数（真实字段 start_minutes；兼容字符串字段）。"""
    value = node.get("start_minutes")
    if value is None and node.get("arrival") is not None:
        return _hhmm_to_minutes(node.get("arrival"))
    return _hhmm_to_minutes(value)


def translate_chat_intents(
    intents: Sequence[Dict[str, Any]],
    requirement: Dict[str, Any],
) -> Dict[str, Any]:
    """把模型输出的修改意图翻译成 A 侧可消费的结构（纯函数）。

    ``requirement`` 遵循 A 侧统一形态（``{"content": {...}}`` 包装，与
    ``select_spots`` / ``replan`` / ``BPlannerHook`` 一致）；缺 ``content``
    时按裸形态容错读取。

    返回 dict：:
        events                A 事件 dict 列表（remove → ``closed``；喂 ``replan``）
        requirement_override  携 ``content`` 的变更副本或 None（add → must_visit 追加）
        reschedule_ops        时段锚点替换操作列表（{spot, day?, time}）
        notes                 翻译说明（可回填给 LLM）
        unsupported           无法翻译的意图（动作非法 / 缺景点名）
        errors                字段级错误（如 reschedule 缺 time）
    """
    events: List[Dict[str, Any]] = []
    requirement_override: Optional[Dict[str, Any]] = None
    reschedule_ops: List[Dict[str, Any]] = []
    notes: List[str] = []
    unsupported: List[Dict[str, Any]] = []
    errors: List[str] = []

    content = requirement.get("content")
    bare = not isinstance(content, dict)   # 裸形态：content 就是 requirement 本身

    for intent in intents or []:
        if not isinstance(intent, dict):
            unsupported.append(intent)
            notes.append(f"忽略非法意图：{intent!r}")
            continue
        action = str(intent.get("action") or "")
        spot = str(intent.get("spot") or "").strip()
        if action not in CHAT_ACTIONS:
            unsupported.append(intent)
            notes.append(f"不支持的动作 {action!r}，已忽略")
            continue
        if not spot:
            errors.append(f"{action} 意图缺少景点名称")
            notes.append(f"{action} 意图缺少景点名称，已忽略")
            continue

        if action == "remove":
            events.append(
                {
                    "event_type": "closed",
                    "spot": spot,
                    "severity": "high",
                    "detail": f"用户对话要求移除景点「{spot}」",
                }
            )
            notes.append(f"移除 {spot}（按 closed 事件走增量修复）")
        elif action == "add":
            if requirement_override is None:
                if bare:
                    # 裸形态归一为 content 包装（A 侧统一形态），避免污染原对象
                    requirement_override = {"content": _deepish_copy(requirement)}
                else:
                    requirement_override = _deepish_copy(requirement)
            add_content = requirement_override["content"]
            if not isinstance(add_content, dict) or "constraints" not in add_content:
                errors.append("需求缺少 content.constraints，无法翻译 add 意图")
                notes.append("需求缺少 content.constraints，add 意图已忽略")
                continue
            must_visit = add_content.setdefault("constraints", {}).setdefault(
                "must_visit", []
            )
            if spot not in must_visit:
                must_visit.append(spot)
                notes.append(f"新增 {spot} 到 must_visit")
            else:
                notes.append(f"{spot} 已在 must_visit，无需重复添加")
        elif action == "reschedule":
            time_ = str(intent.get("time") or "").strip()
            if not time_:
                errors.append(f"reschedule {spot} 缺少 time")
                notes.append(f"reschedule {spot} 缺少 time，已忽略")
                continue
            reschedule_ops.append(
                {"spot": spot, "day": intent.get("day"), "time": time_}
            )
            notes.append(f"{spot} 调整到达时段到 {time_}")

    return {
        "events": events,
        "requirement_override": requirement_override,
        "reschedule_ops": reschedule_ops,
        "notes": notes,
        "unsupported": unsupported,
        "errors": errors,
    }


def _deepish_copy(requirement: Dict[str, Any]) -> Dict[str, Any]:
    """requirement 的浅层深拷贝（约束/偏好列表独立，避免污染原对象）。"""
    import copy

    return copy.deepcopy(requirement)


# ---------------------------------------------------------------------------
# reschedule（时段锚点替换）：在 A 计划 dict（select_spots/replan 形态）上操作
# ---------------------------------------------------------------------------


def _find_spot_nodes(plan: Dict[str, Any], spot: str) -> List[Tuple[int, Dict[str, Any]]]:
    """在计划里找名称/别名匹配 spot 的节点，返回 [(day_index, node), ...]。"""
    found: List[Tuple[int, Dict[str, Any]]] = []
    for day in plan.get("days") or []:
        nodes = day.get("route_details") or []
        for node in nodes:
            if node.get("type") != "spot":
                continue
            name = str(node.get("name") or "")
            details = node.get("details") or {}
            spot_id = details.get("spot_id") or ""
            if name == spot or spot_id == spot:
                found.append((int(day.get("day") or 0), node))
    return found


def _plan_day_by_index(plan: Dict[str, Any], index: int) -> Optional[Dict[str, Any]]:
    """按人的天序号（1 起）取 day dict；不存在返回 None。"""
    for day in plan.get("days") or []:
        if int(day.get("day") or 0) == index:
            return day
    return None


def apply_reschedule_ops(
    plan: Dict[str, Any],
    ops: Sequence[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[str]]:
    """把时段锚点替换应用到 A 计划 dict 的深拷贝上，返回 (新计划, 错误列表)。

    纯函数：不修改输入的 ``plan``。每条 op：:
        {"spot": str, "day": int|None, "time": "HH:MM"}

    语义：
    - 目标天缺省 = 保持原天，只改该天的到达时段；
    - 指定目标天 => 跨天搬移（从原天移除节点，插入目标天末尾，覆盖 time）；
    - 校验：time 合法且在 [09:00, 20:00]；目标天存在；计划中恰好一个匹配节点
      （多个匹配记录歧义错误，交由上层回给 LLM 澄清）。
    注意：本层只做时段合法性与存在性校验；闭馆/预算等深度可行性仍由 B 侧
    ``timeline_validator`` 在最终时间轴上执行。
    """
    import copy

    result = copy.deepcopy(plan)
    errors: List[str] = []

    for op in ops:
        spot = str(op.get("spot") or "").strip()
        time_ = str(op.get("time") or "").strip()
        if not spot or not time_:
            errors.append(f"reschedule 操作缺少 spot 或 time：{op!r}")
            continue
        minutes = _hhmm_to_minutes(time_)
        if minutes is None:
            errors.append(f"{spot} 的目标时段 {time_!r} 不是合法 HH:MM")
            continue
        earliest, latest = _hhmm_to_minutes(_RESCHEDULE_EARLIEST), _hhmm_to_minutes(
            _RESCHEDULE_LATEST
        )
        if minutes < earliest or minutes > latest:
            errors.append(
                f"{spot} 的目标时段 {time_} 超出允许区间 "
                f"{_RESCHEDULE_EARLIEST}-{_RESCHEDULE_LATEST}"
            )
            continue

        matches = _find_spot_nodes(result, spot)
        if not matches:
            errors.append(f"计划中未找到景点「{spot}」，无法调整时段")
            continue
        if len(matches) > 1:
            errors.append(
                f"计划中「{spot}」出现在 {len(matches)} 处，请指定更明确的景点"
            )
            continue
        day_index, node = matches[0]
        target_day = op.get("day")
        if target_day is not None:
            try:
                target_index = int(target_day)
            except (TypeError, ValueError):
                errors.append(f"{spot} 的目标天 {target_day!r} 不是整数")
                continue
            target = _plan_day_by_index(result, target_index)
            if target is None:
                errors.append(f"计划中没有第 {target_index} 天，无法搬移 {spot}")
                continue
            if target_index != day_index:
                # 跨天搬移：从原天移除，插到目标天末尾
                source_day = _plan_day_by_index(result, day_index)
                if source_day is not None:
                    source_day["route_details"] = [
                        n for n in source_day.get("route_details") or [] if n is not node
                    ]
                target.setdefault("route_details", []).append(node)
        _set_node_time(node, minutes)

    return result, errors


def _set_node_time(node: Dict[str, Any], start_minutes: int) -> None:
    """把节点到达时段写到真实字段（start_minutes/end_minutes），并同步
    兼容字段（arrival/end_time）——A 内部统一用分钟，外部字典可能带字符串。
    """
    default_duration = 90  # 与 timeline_validator.DEFAULT_SPOT_MIN 同口径兜底
    node["start_minutes"] = start_minutes
    node["end_minutes"] = start_minutes + default_duration
    node["duration_minutes"] = default_duration
    if "arrival" in node or "end_time" in node:
        node["arrival"] = _minutes_to_hhmm(start_minutes)
        node["end_time"] = _minutes_to_hhmm(start_minutes + default_duration)