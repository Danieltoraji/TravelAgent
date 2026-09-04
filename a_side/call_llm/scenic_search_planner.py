"""ScenicSearchPlanner：候选池搜索计划的 LLM 定制（交接清单十二节 A）。

背景：层一（9.2 实证修正版，B 侧 ``scenic_tool``）默认用**固定词表**分层
搜索——市区桶「城市 景点」+ 远郊类别词（长城/寺/山/国家森林公园），对北京
这类「远郊名胜密集」的城市效果好，但**每个城市景点分布特点不同**（杭州
湖/园林、上海摩天楼/博物馆、西安古迹）：固定词表对非典型城市召回会偏。

本模块加两层 LLM 定制：让模型按目的地城市特点给出一组分桶搜索计划
``{"buckets": [...], "center_schedule": [...]}``，经 ``LiveSpotsSource``
透传给 B 侧 ``scenic`` 工具的 ``search_plan``（``_layered_search`` 按计划桶
覆盖固定词表；缺省 None → 固定表，零回归）；**``center_schedule``（P5.7，
2026-09-08 增补：按日簇中心计划）** 取代单一 ``trip_center``——长行程/点稀疏
城市不应全旅程钉死一个空间中心（张掖以丹霞为单中心 → 丹霞簇池浅每天凑低质、
市区高分点被全局锚误杀），改由 LLM 结合 days/偏好/必去决定**哪几天围绕哪个
中心、何时换中心**（换中心成本由排程层 daily_travel_time 硬约束兜底，此处
只出切片）。返回另附过渡字段 ``trip_center = 首簇中心``（保持现
``b_planner_hook`` 接线兼容，S3 后移除）。

设计（对齐 P5.2 PlannerAgent 先例但更轻——纯规划，不查真源工具）：
- 门控 ``USE_LLM_TOOLS`` 默认关 → ``plan_for`` 返回 None（调用方走固定词表，
  行为与既往完全一致，零额度零回归）；
- 一次 LLM 调用 + ``response_schema`` 结构化输出；LLM 失败 / 结构非法 /
  校验不过 → None + warning（LLM 乱出不影响候选池产出）；
- 护栏：第 1 桶必须是市区综合桶（keywords 含「景点」，质量已验证 4.7-4.9）；
  桶数 1~8、总词数 ≤24、quota_ratio 0~1（B 侧归一化兜底）；
  ``center_schedule`` **规则定界**：簇数 ≤ min(days,3)、区间不重叠、空白天
  继承前簇、poi 未命中由排程层回退质心、days≥4 防「一天一中心」碎片化、
  整段非法 → 降级默认 city_center（不拖垮 buckets）。
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from call_llm.client_factory import create_llm_client
from call_llm.decision_engine import _use_llm_tools

logger = logging.getLogger("call_llm.scenic_search_planner")

# ---------------------------------------------------------------------------
# 提示词与响应 Schema
# ---------------------------------------------------------------------------

SCENIC_SEARCH_PLAN_SYSTEM = (
    "你是旅行规划助手，负责为「某城市景点的候选池搜索」制定分层搜索计划。\n"
    "候选池会按你给的关键词逐词调用地图 POI 搜索（每个词自动拼成"
    "「城市 词」查询），再按评分排序选入候选池——所以关键词要选"
    "**地图能搜到、且能代表该城不同空间/类型分布的类别词**。\n"
    "规则：\n"
    "1. 第一个桶（buckets[0]）必须是**市区综合桶**：keywords 必须含「景点」"
    "（搜索「城市 景点」能稳定召回天安门/故宫级市中心高分景点），"
    "quota_ratio 建议 0.5~0.7；\n"
    "2. 其余桶按该城市远郊/特色分布给类别词（参考：长城、山、寺、湖、"
    "国家森林公园、古镇、博物馆、园林、遗址公园、丹霞、峡谷、草原、海洋馆、"
    "主题乐园、滑雪场等——**只选该城真实存在且成规模的类别**，每桶 1~3 个词，"
    "全计划总词数 3~8）；\n"
    "3. 参考用户的偏好标签（preferred_tags）与必去景点（must_visit）微调"
    "类别词——如偏好「历史文化」可加「博物馆」「遗址」类桶；\n"
    "4. quota_ratio 是该桶占候选池的比例（0~1），各桶相加应≈1，"
    "远郊桶比例别压过市区综合桶；\n"
    "5. 只输出 JSON：{buckets: [...], center_schedule: [...]}，不要解释。\n\n"
    "**center_schedule（按日簇中心计划）——决定行程哪些天围绕同一个空间中心、"
    "何时换中心**：\n"
    "- 中心 = 一批天的活动围绕的空间点：type=city_center（市区即核心）或 "
    "type=poi（城外核心景点，poi=完整景点名如「张掖七彩丹霞景区」）；\n"
    "- **切片原则（结合 destination、days、preferred_tags、must_visit）**："
    "核心景点在城外（如 张掖=丹霞 34km 外）且用户为其而来 → 该 poi 值得独占"
    "一到两天；其余天围绕市区/另一片区（城市/湿地/峡谷等各成一簇）——"
    "点稀疏目的地少切（全行程 ≤3 簇），长行程才多切；别一天一中心；\n"
    "- 每簇给 day_from/day_to（1..days 闭区间）与 reason（依据一句话）；"
    "拿不准时整段给一个 type=city_center；系统会对区间做规则定界（越界/"
    "重叠/空白自动纠正），poi 若不存在由排程回退，不会炸。"
)

SCENIC_SEARCH_PLAN_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "buckets": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                        "description": "类别词（自动拼「城市 词」查询；"
                        "市区综合桶含「景点」）",
                    },
                    "quota_ratio": {
                        "type": "number",
                        "description": "该桶占候选池比例（0~1，各桶相加≈1）",
                    },
                    "note": {
                        "type": "string",
                        "description": "桶的空间/类型说明（如 市区综合 / 远郊长城）",
                    },
                },
                "required": ["keywords", "quota_ratio"],
            },
        },
        "center_schedule": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "day_from": {"type": "integer", "minimum": 1,
                                 "description": "该中心簇起始天（1..days）"},
                    "day_to": {"type": "integer", "minimum": 1,
                               "description": "该中心簇结束天（1..days，≥day_from）"},
                    "center": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "enum": ["city_center", "poi"]},
                            "poi": {
                                "type": "string",
                                "description": "type=poi 时的核心景点完整名"
                                "（如 张掖七彩丹霞景区）",
                            },
                            "reason": {
                                "type": "string",
                                "description": "该簇判断依据一句话"
                                "（引用用户偏好/必去/城市特点）",
                            },
                        },
                        "required": ["type"],
                    },
                },
                "required": ["day_from", "day_to", "center"],
            },
        },
    },
    "required": ["buckets", "center_schedule"],
}

# 关键护栏词：第 1 桶必须含「景点」（市区综合桶质量已验证）
_URBAN_ANCHOR = "景点"
_MAX_BUCKETS = 8
_MAX_TOTAL_WORDS = 24
# center_schedule 规则定界（P5.7）：簇数上限（点稀疏/长行程防碎片化）与
# 缺省中心（保守——不引入错误锚点）
_MAX_CENTERS = 3
_DEFAULT_CENTER = {"type": "city_center", "reason": "默认：市区即旅行中心"}


def _user_plan_prompt(
    destination: str,
    days: int,
    preferred_tags: Optional[List[str]],
    must_visit: Optional[List[str]],
) -> str:
    tags_txt = "、".join(preferred_tags or []) or "无"
    must_txt = "、".join(must_visit or []) or "无"
    return (
        f"请为 {destination} 制定候选池分层搜索计划（行程 {days} 天）。\n"
        f"用户偏好标签：{tags_txt}\n必去景点：{must_txt}\n"
        "按 schema 输出 JSON。"
    )


def _validate_buckets(plan: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """结构校验 + 护栏，返回 ``{"buckets": [...]}`` 或 None（调用方回退固定词表）。

    - 顶层含非空 buckets 列表；
    - buckets[0].keywords 必须含「景点」（市区综合桶护栏）；
    - 每桶 keywords 非空字符串、quota_ratio 0~1；
    - 桶数 ≤ _MAX_BUCKETS、总词数 ≤ _MAX_TOTAL_WORDS（超限截断）。
    """
    try:
        buckets = plan.get("buckets")
    except AttributeError:
        return None
    if not isinstance(buckets, list) or not buckets:
        return None

    cleaned: List[Dict[str, Any]] = []
    for bucket in buckets[:_MAX_BUCKETS]:
        if not isinstance(bucket, dict):
            continue
        raw_keywords = bucket.get("keywords")
        if not isinstance(raw_keywords, list):
            continue
        words: List[str] = []
        for kw in raw_keywords:
            kw = str(kw).strip()
            if kw and kw not in words:
                words.append(kw)
        if not words:
            continue
        ratio_raw = bucket.get("quota_ratio")
        try:
            ratio = float(ratio_raw) if ratio_raw is not None else 0.0
        except (TypeError, ValueError):
            ratio = 0.0
        if not (0.0 < ratio <= 1.0):
            ratio = 0.0
        cleaned.append({"keywords": words, "quota_ratio": ratio,
                        "note": str(bucket.get("note") or "")})
    if not cleaned:
        return None

    # 护栏：市区综合桶必须是第 1 桶（含「景点」锚词）
    if _URBAN_ANCHOR not in cleaned[0]["keywords"]:
        logger.warning("search_plan 第 1 桶缺市区锚词「%s」，计划作废回退", _URBAN_ANCHOR)
        return None

    # 总词数截断
    total_words = sum(len(b["keywords"]) for b in cleaned)
    if total_words > _MAX_TOTAL_WORDS:
        logger.warning("search_plan 总词数 %d > %d，截断", total_words, _MAX_TOTAL_WORDS)
        kept: List[Dict[str, Any]] = []
        budget = _MAX_TOTAL_WORDS
        for bucket in cleaned:
            if budget <= 0:
                break
            words = bucket["keywords"][:budget]
            budget -= len(words)
            kept.append({"keywords": words, "quota_ratio": bucket["quota_ratio"],
                         "note": bucket["note"]})
        cleaned = kept
    return {"buckets": cleaned}


def _clean_center(center: Any) -> Optional[Dict[str, Any]]:
    """校验/归一化单个中心（city_center / poi）；非法 → None（保守丢弃）。"""
    if not isinstance(center, dict):
        return None
    tc_type = str(center.get("type") or "")
    if tc_type == "poi":
        poi = str(center.get("poi") or "").strip()
        if not poi:
            return None
        return {"type": "poi", "poi": poi, "reason": str(center.get("reason") or "")}
    if tc_type == "city_center":
        return {"type": "city_center", "reason": str(center.get("reason") or "")}
    return None


def _center_signature(center: Dict[str, Any]) -> tuple:
    """中心的等价签名（type + poi）——同签名相邻簇自动合并用。"""
    return (center.get("type"), center.get("poi") or "")


def _validate_center_schedule(content: Dict[str, Any], days: int) -> Dict[str, Any]:
    """LLM 的按日簇中心计划 → 规则定界的 center_schedule + 过渡 trip_center。

    规则（P5.7 拍板：LLM 切片、规则定上下界）：
    - 簇区间合法（1..days、day_from ≤ day_to、中心可识别），不合法整簇丢弃；
    - 簇数 ≤ min(days, _MAX_CENTERS)（保留最早 cap 条）；
    - days ≥ 4 防碎片化：单日且非「poi+reason」的弱簇并入前一簇；
    - 逐日赋中心（重叠先到先得、空白继承前簇/默认市中心）后按「连续同中心」
      重切——保证输出区间覆盖 1..days、无空洞；
    - 无任何合法簇 → 默认整段 city_center。
    返回 {"center_schedule": [...], "trip_center": 首簇中心}（过渡字段保持现
    b_planner_hook 接线兼容，S3 后移除）。
    """
    days = max(1, int(days or 2))
    default_center = dict(_DEFAULT_CENTER)
    try:
        raw = content.get("center_schedule")
    except AttributeError:
        raw = None

    clusters: List[Dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            try:
                day_from = int(item.get("day_from"))
                day_to = int(item.get("day_to"))
            except (TypeError, ValueError):
                continue
            day_from = max(1, min(days, day_from))
            day_to = max(1, min(days, day_to))
            if day_from > day_to:
                continue
            center = _clean_center(item.get("center"))
            if center is None:
                continue
            clusters.append({"day_from": day_from, "day_to": day_to,
                             "center": center})
    if not clusters:
        return {
            "center_schedule": [{"day_from": 1, "day_to": days,
                                 "center": dict(default_center)}],
            "trip_center": dict(default_center),
        }

    clusters.sort(key=lambda c: (c["day_from"], c["day_to"]))
    cap = min(days, _MAX_CENTERS)
    if len(clusters) > cap:
        clusters = clusters[:cap]

    # 防碎片化（days≥4）：单日弱簇并入前一簇（区间由后续逐日赋值兜底）
    if days >= 4:
        merged: List[Dict[str, Any]] = []
        for c in clusters:
            strong_single = (
                c["day_from"] == c["day_to"]
                and c["center"].get("type") == "poi"
                and c["center"].get("reason")
            )
            if c["day_from"] == c["day_to"] and not strong_single and merged:
                merged[-1]["day_to"] = max(merged[-1]["day_to"], c["day_to"])
                continue
            merged.append(dict(c))
        clusters = merged or [
            {"day_from": 1, "day_to": days, "center": dict(default_center)}
        ]

    # 逐日赋中心（重叠先到先得）→ 重切连续同中心簇（区间闭包 1..days 无空洞）
    assigned: List[Optional[Dict[str, Any]]] = [None] * days
    for c in clusters:
        for d in range(c["day_from"] - 1, min(c["day_to"], days)):
            if assigned[d] is None:
                assigned[d] = c["center"]
    last: Optional[Dict[str, Any]] = None
    for d in range(days):
        if assigned[d] is None:
            assigned[d] = last if last is not None else dict(default_center)
        last = assigned[d]
    schedule: List[Dict[str, Any]] = []
    for d, center in enumerate(assigned, start=1):
        if schedule and _center_signature(schedule[-1]["center"]) == \
                _center_signature(center):
            schedule[-1]["day_to"] = d
        else:
            schedule.append({"day_from": d, "day_to": d, "center": center})
    return {"center_schedule": schedule, "trip_center": schedule[0]["center"]}


def _validate_plan(content: Dict[str, Any], days: int) -> Optional[Dict[str, Any]]:
    """整体校验（buckets + center_schedule），返回完整计划或 None。

    buckets 校验不过 → None（回退固定词表）；center_schedule 独立规则定界、
    非法只降级默认 city_center（不拖垮 buckets）。
    """
    buckets_part = _validate_buckets(content)
    if buckets_part is None:
        return None
    schedule_part = _validate_center_schedule(content, days)
    return {"buckets": buckets_part["buckets"],
            "center_schedule": schedule_part["center_schedule"],
            "trip_center": schedule_part["trip_center"]}


class ScenicSearchPlanner:
    """LLM 定制候选池搜索计划执行器（门控默认关，返回 None = 走固定词表）。

    使用方法::

        planner = ScenicSearchPlanner()
        plan = planner.plan_for("北京", days=2,
                                preferred_tags=["历史文化"], must_visit=["故宫"])
        # -> {"buckets": [...]} 或 None（门控关 / 失败 / 校验不过）

    门控：``USE_LLM_TOOLS`` 未开启 → 直接返回 None（零额度零回归）；
    LLM 客户端经 ``create_llm_client`` 创建（模型配置走环境变量），测试可注入。
    """

    def __init__(
        self,
        model_name: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: int = 60,
    ):
        self.model_name = model_name
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout

    @property
    def enabled(self) -> bool:
        """门控开启才尝试调 LLM（默认关）。"""
        return _use_llm_tools()

    def plan_for(
        self,
        destination: str,
        days: int = 2,
        preferred_tags: Optional[List[str]] = None,
        must_visit: Optional[List[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """为目的地生成分层搜索计划；门控关 / 失败 / 校验不过 → None。

        一次 LLM 调用（无工具回路，纯规划）；返回已校验的
        ``{"buckets": [...]}`` 或 None。
        """
        if not self.enabled:
            return None
        if not destination:
            return None
        try:
            client = create_llm_client(
                model_name=self.model_name,
                api_key=self.api_key,
                base_url=self.base_url,
                timeout=self.timeout,
                ask_user_if_missing=False,
                system_instruction=SCENIC_SEARCH_PLAN_SYSTEM,
                max_tokens=1200,
            )
            messages = [{
                "role": "user",
                "content": _user_plan_prompt(
                    destination, int(days or 2), preferred_tags, must_visit,
                ),
            }]
            result = client.generate(
                messages=messages,
                response_schema=SCENIC_SEARCH_PLAN_SCHEMA,
            )
        except Exception as exc:  # noqa: BLE001  LLM 失败不阻断候选池
            logger.warning("ScenicSearchPlanner 调用失败（%s）：%s", destination, exc)
            return None

        content = result.get("content") or {}
        if not isinstance(content, dict):
            return None
        plan = _validate_plan(content, int(days or 2))
        if plan is None:
            logger.warning("ScenicSearchPlanner 输出校验不过（%s）", destination)
            return None
        logger.info(
            "ScenicSearchPlanner 计划（%s）：buckets=%s center_schedule=%s",
            destination,
            [b["keywords"] for b in plan["buckets"]],
            plan["center_schedule"],
        )
        return plan
