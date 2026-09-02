"""景点 Tool：开放状态、排队、预约、营业时间（对应 Scenic Agent 的 API 封装）。

Mock 版（ScenicTool）：从 MockWorld 读取模拟数据，Demo 剧情用。
Live 版（ScenicToolLive）：调高德 POI 搜索 API，返回真实评分/地址/电话。

两种 ``action``：
- ``status``（默认）：单景点实时状态 dict（既有契约，Monitor / Execution 用）；
- ``search``：**城市景点候选池**——返回 A 侧 spot dict 列表（B5 字段对齐：
  id / name / alias / location / suggest_duration / opening_time / closing_time /
  price / tags / rating），供 A 侧规划层 ``LiveSpotsSource`` 直接消费。

切换方式：build_registry() 按 settings.use_real_map_api 自动选择。
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from tools.base_tool import BaseTool
from tools.mock_data import MockWorld, PLACES

logger = logging.getLogger("tools.scenic")

# 默认营业时间
_DEFAULT_OPEN = "09:00"
_DEFAULT_CLOSE = "17:00"
# 无 API 数据时的建议停留时长（分钟）
_DEFAULT_DURATION = 120

# 层一（9.2 十一节）：多锚点分层搜索——市区 text「城市 景点」+ 远郊通用类别词 text。
#
# **为什么弃用 around 三桶（2026-09-02 真源实证）**：`search_poi_around` 按
# `sortrule=distance` 距离升序返回，市中心 POI 密度极大（东交民巷一带 0.2-0.6km
# 就有几十个旧址/遗址/打卡点），15/40/80km 三桶的配额全被市中心占满 → 剔除
# 逻辑整桶清空 → 远郊 0 命中（实测最终池 6 家全是 0.2-0.3km 低质 POI）。
# 免费 key 下 around 无法实现空间分层。
#
# **text 关键词按名称相关度匹配（不按距离）**：实测「北京 长城」直击八达岭
# 长城(60km, 4.8)/慕田峪长城(60km, 4.8)；「北京 寺」直击潭柘寺(32km, 4.8)/
# 红螺寺(56km, 4.8)；「北京 山」直击西山森林公园(20km, 4.7)/百望山(19km, 4.7)；
# 「北京 国家森林公园」直击北宫(25km, 4.7)/鹫峰(32km, 4.5)。故层一改为：
# 市区桶 `search_poi(f"{city} 景点")`（质量已验证 4.7-4.9）+ 远郊类别词
# `search_poi(f"{city} {word}")` 定向召回，合并去重 → rating 降序截断。
#
# 远郊类别词表（通用，跨城市有效；无对应景点的城市该词返回空自动跳过）：
# 长城（八达岭/慕田峪级）/ 寺（潭柘寺/红螺寺级）/ 山（西山/百望山级）/
# 国家森林公园（北宫/鹫峰级）——实证「湖」「古镇」「遗址公园」召回弱/噪音多，不入选。
_FAR_KEYWORDS = ("长城", "寺", "山", "国家森林公园")
# 市区桶配额比例（剩余给远郊类别词桶，随 limit=days×5 联动）
_URBAN_RATIO = 0.6
# 词间节律（免费 key QPS≈2-3/s，照抄 _PAGE_DELAY）
_BUCKET_DELAY = 0.3

# 真实高德 opentime_today 格式繁杂（"08:30-17:00"、"09:00-22:00;18:30-22:00"、
# "14:00 18:30-22:00"…）——取首个 "HH:MM-HH:MM" 区间，解析失败走默认值。
_OPEN_RANGE_RE = re.compile(r"(\d{1,2}:\d{2})\s*[-—~至]\s*(\d{1,2}:\d{2})")


def _split_open_range(open_range: str) -> Tuple[str, str]:
    """``"08:30-17:00"`` → ``("08:30", "17:00")``；多段/杂乱文本取首个区间；无法解析 → 默认 09:00-17:00。"""
    text = (open_range or "").strip()
    match = _OPEN_RANGE_RE.search(text)
    if match:
        return match.group(1), match.group(2)
    return _DEFAULT_OPEN, _DEFAULT_CLOSE


def _split_tags(tag: str, type_text: str = "") -> List[str]:
    """``tag``（逗号分隔）+ ``type``（分号分隔）→ 标签列表（大类置首）。"""
    tags = [item.strip() for item in (tag or "").split(",") if item.strip()]
    first_type = (type_text or "").split(";")[0].strip()
    if first_type and first_type not in tags:
        tags.insert(0, first_type)
    return tags


def _split_alias(alias: str) -> List[str]:
    return [item.strip() for item in (alias or "").split(",") if item.strip()]


def _open_range_to_fields(open_range: str) -> Dict[str, str]:
    opening, closing = _split_open_range(open_range)
    return {"opening_time": opening, "closing_time": closing}


class ScenicTool(BaseTool):
    name = "scenic"
    domain = "scenic"
    internal_actions = ["search"]         # 内部管道：A 侧候选池
    description = (
        "景点实时状态：是否开放、预计排队分钟数、是否需要预约、营业时间、票价；"
        "或按城市搜索景点候选池（action=search）。"
    )
    source = "mock"
    input_schema = {
        "type": "object",
        "properties": {
            "action": {
                "enum": ["status", "search"],
                "description": "status 单景点实时状态（默认）；search 返回城市景点候选池",
            },
            "place": {"type": "string", "description": "景点名称，或 search 时为目标城市"},
            "limit": {"type": "integer", "description": "search 返回数量上限（默认 10）"},
            # 12（9.2 十二节 A）：LLM 定制的候选池搜索计划（可选）——
            # {buckets: [{keywords: [类别词], quota_ratio: 0~1}]} 覆盖固定词表；
            # 缺省 None → 用内置固定词表（_FAR_KEYWORDS/_URBAN_RATIO），零回归。
            "search_plan": {
                "type": "object",
                "description": "可选。A 侧 ScenicSearchPlanner 产出的分层搜索计划："
                "{buckets: [{keywords: [...], quota_ratio: 0~1, note: \"\"}]}。"
                "每个 bucket 是一组关键词（自动拼「城市 词」查询）+ 占池宽配额比例；"
                "校验失败/缺省 → 回退内置固定词表。",
            },
            # 8.30 必去强拉：搜索结果未覆盖的必去景点逐个精确查找入库
            "ensure_spots": {"type": "array", "items": {"type": "string"},
                             "description": "必去景点名列表（search 时强拉保障，远郊景点不依赖搜索排名）"},
            # C6：schema 与实现同步（Live _status 用 city 做地理编码限定）
            "city": {"type": "string", "description": "地理编码限定城市（status 时用，默认北京）"},
        },
        "required": ["place"],
    }

    def __init__(self, world: Optional[MockWorld] = None) -> None:
        super().__init__()
        self._world = world or MockWorld()

    def _run(self, place: str = "", action: str = "status",
             limit: int = 10, city: str = "") -> Any:
        if action == "search":
            return self._search_spots(place, limit=limit)
        return self._status(place)
    def _status(self, place: str) -> Dict[str, Any]:
        info = self._world.get_place(place)
        if info is None:
            raise ValueError(f"Unknown place: {place}")
        return {
            "place": place,
            "open": True,
            "queue_min": info["queue_min"],
            "ticket_required": info["ticket"],
            "open_hours": info["open"],
            "price": info["price"],
            "rating": 0,
            "address": "",
            "tel": "",
            "open_hours_week": "",
        }

    def _search_spots(self, place: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Mock 城市候选池：从 MockWorld / PLACES 取全部点位，对齐 A 侧 spot dict。"""
        rows: List[Dict[str, Any]] = []
        for index, (name, info) in enumerate(PLACES.items()):
            if limit and len(rows) >= int(limit):
                break
            rows.append(
                {
                    "id": f"mock_{index}",
                    "name": name,
                    "alias": [],
                    "location": {"lat": info["lat"], "lng": info["lng"]},
                    "suggest_duration": _DEFAULT_DURATION,
                    **_open_range_to_fields(info["open"]),
                    "price": info["price"],
                    "tags": ["景点"],
                    "rating": 0.0,
                }
            )
        return rows


class ScenicToolLive(ScenicTool):
    """高德 POI 搜索 API 实现版。

    调用链路：
      1. search_poi(place, city="北京") → 获取景点 POI 信息
      2. 从 POI 提取 rating（评分）、address（地址）、tel（电话）

    局限：
      - 排队时间无公开 API，从 MockWorld 取（Demo 剧情关键变量）
      - 营业时间从 v5 API opentime_today 获取，API 无数据时 fallback MockWorld
      - 票价高德无此字段，从 MockWorld 取或默认 0

    返回与 Mock 版完全相同的 dict 结构，调用方零改动。
    """

    source = "live"

    def __init__(self, client: Any, world: Optional[MockWorld] = None) -> None:
        """初始化 Live 版景点 Tool。

        Args:
            client: AmapClient 实例（共享 API Key + 地理编码缓存）
            world: MockWorld 实例（用于排队/票价等无 API 字段的 fallback）
        """
        super().__init__(world)
        self._client = client

    def _run(self, place: str = "", action: str = "status",
             limit: int = 10, city: str = "北京",
             ensure_spots: Optional[List[str]] = None,
             search_plan: Optional[Dict[str, Any]] = None) -> Any:
        if action == "search":
            return self._search_spots(
                place, limit=limit, ensure_spots=ensure_spots,
                search_plan=search_plan,
            )
        return self._status(place, city=city)

    def _status(self, place: str, city: str = "北京") -> Dict[str, Any]:
        pois = self._client.search_poi(place, city=city, limit=1)
        if not pois:
            raise ValueError(f"未找到景点: {place}")
        poi = pois[0]

        # 从 MockWorld 获取排队/票价（无公开 API）
        info = self._world.get_place(place)
        queue_min = info["queue_min"] if info else 20
        ticket_required = info["ticket"] if info else True
        price = info["price"] if info else 0.0

        # 营业时间：优先用 v5 API 返回的 opentime_today，空时 fallback MockWorld
        open_hours = poi.get("opentime_today", "") or (info["open"] if info else "")

        logger.info(
            "Scenic: %s → rating=%s, opentime=%s, address=%s",
            place, poi.get("rating", 0), open_hours, poi.get("address", ""),
        )

        return {
            "place": place,
            "open": True,               # 高德基础 API 无法判断是否开放
            "queue_min": queue_min,      # 无公开 API，从 MockWorld 取
            "ticket_required": ticket_required,
            "open_hours": open_hours,    # v5 API opentime_today，fallback MockWorld
            "price": price,
            "rating": poi.get("rating", 0),
            "address": poi.get("address", ""),
            "tel": poi.get("tel", ""),
            "open_hours_week": poi.get("opentime_week", ""),
        }

    def _search_spots(
        self, place: str, limit: int = 10, ensure_spots: Optional[List[str]] = None,
        search_plan: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """Live 城市候选池（B5）：多锚点分层搜索 → 返回 A 侧 spot dict 列表。

        ``search_plan``（9.2 十二节 A）：可选 LLM 定制搜索计划——缺省 None 走
        内置固定词表（零回归）；结构见 ``_layered_search``。

        **层一（9.2 十一节：候选池空间覆盖，真源实证后修正版）**：
        - ``_layered_search`` = 市区桶 text「城市 景点」（质量 4.7-4.9，实测天安门/
          故宫/天坛/景山）+ 远郊类别词 text「城市 长城/寺/山/国家森林公园」——
          远郊优质景点（八达岭/慕田峪/潭柘寺/红螺寺级）不再被「市中心相关度排序」
          饿死；**弃用 around 三桶**（实证：around 按距离升序，市中心 POI 密度
          把 15/40/80km 桶配额占满，剔除逻辑整桶清空，远郊 0 命中）；
        - 桶配额随 limit（= ``days×5``）联动：市区 60% / 远郊类别词 40% 摊分、
          每词至少 2 个候选（如 2 天 10 家 = 6 市区 + 4 词×2 远郊候选，合并后
          rating 截断收束池宽到 limit），池宽总量不变只是空间分层；
        - 市区桶失败 / 全空 → 回退老 text 路径（``search_poi`` ``f"{city} 景点"``
          → 城市名），保持既有行为不炸；远郊单词失败/无结果跳过不阻断；
        - 合并后按 **rating 降序**截断到 limit（质量优先 + 池宽上限）；
        - 词间 0.3s 节律（``_BUCKET_DELAY``，防免费 key QPS 10021）。

        **既有语义保留**：
        - ``limit`` 直透（>25 时 amap_client 自动翻页）——候选池宽度随天数联动；
        - **同名去重**（8.30）：翻页/跨桶同名 POI（不同入口/别名）按名称去重，
          ``scenic_N`` 的 N 取去重后的全局序号；
        - **must_visit 强拉**（8.30）：``ensure_spots`` 里的必去景点若未被搜索
          结果覆盖（远郊排名靠后/关键词召回死角），逐个按名字精确搜索拉回——
          必去是硬约束，**在 rating 截断之后追加**（硬约束不被池宽截断挤掉）；
        - 字段映射 / 默认时长 / 兜底逻辑不变。
        """
        city = place or ""
        limit = max(int(limit or 10), 1)

        pois = self._layered_search(city, limit, search_plan=search_plan)
        if not pois:
            # geocode 失败 / 分层桶全空 → 回退老 text 路径（行为不变）
            pois = self._client.search_poi(f"{city} 景点", city=city, limit=limit)
            if not pois:
                pois = self._client.search_poi(city, city=city, limit=limit)

        spots: List[Dict[str, Any]] = []
        seen_keys: set = set()
        index = 0

        def _append(poi: Dict[str, Any]) -> None:
            nonlocal index
            name = (poi.get("name") or "").strip() or city
            base_key = name.split("-")[0].strip()
            if name in seen_keys or (len(base_key) >= 2 and base_key in seen_keys):
                return
            seen_keys.add(name)
            if len(base_key) >= 2:
                seen_keys.add(base_key)
            open_hours = poi.get("opentime_today", "")
            spots.append(
                {
                    "id": f"scenic_{index}",
                    "name": name,
                    "alias": _split_alias(poi.get("alias", "")),
                    "location": {
                        "lat": poi.get("lat", 0.0),
                        "lng": poi.get("lng", 0.0),
                    },
                    "suggest_duration": _DEFAULT_DURATION,
                    **_open_range_to_fields(open_hours),
                    "price": float(poi.get("cost", 0) or 0),
                    "tags": _split_tags(poi.get("tag", ""), poi.get("type", "")),
                    "rating": float(poi.get("rating", 0) or 0),
                    "address": poi.get("address", ""),
                    "open_hours_week": poi.get("opentime_week", ""),
                }
            )
            index += 1

        for poi in pois:
            _append(poi)

        # 层一：rating 降序截断到 limit（质量优先，池宽上限）
        if len(spots) > limit:
            spots.sort(key=lambda s: s.get("rating", 0.0), reverse=True)
            del spots[limit:]

        # must_visit 强拉：搜索结果没覆盖的必去景点逐个精确查找（QPS 节流）。
        # 在 rating 截断之后追加——必去是硬约束，不能被池宽截断挤掉。
        if ensure_spots:
            import time as _time

            for i, must_name in enumerate(ensure_spots):
                must_name = (must_name or "").strip()
                if not must_name:
                    continue
                covered = any(
                    must_name in key or key in must_name for key in seen_keys
                )
                if covered:
                    continue
                if i:
                    _time.sleep(0.3)  # 免费key QPS≈2-3/s
                try:
                    exact = self._client.search_poi(
                        must_name, city=city, limit=1
                    )
                except Exception as exc:  # noqa: BLE001  单个必去找不到不阻断
                    logger.warning("must_visit 强拉失败 %s: %s", must_name, exc)
                    continue
                if exact:
                    _append(exact[0])
                    logger.info(
                        "must_visit 强拉入库: %s → %s（搜索排名未覆盖）",
                        must_name, exact[0].get("name"),
                    )
                else:
                    logger.warning("must_visit %s 精确搜索无结果，跳过", must_name)
        return spots

    def _layered_search(
        self, city: str, limit: int,
        search_plan: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        """层一（9.2 十一节）：text 多关键词扩展搜索核心。

        内置固定词表路径（``search_plan`` 缺省 / 无效时，行为与 9.2 实证修正版
        完全一致）：市区桶 ``search_poi(f"{city} 景点")`` + 远郊类别词桶
        ``search_poi(f"{city} {word}")``（``_FAR_KEYWORDS``：长城/寺/山/
        国家森林公园）：
        - 市区桶配额 ``limit × _URBAN_RATIO``（60%），远郊每词配额
          ``limit × (1-ratio) / 词数``——池宽总量不变，空间再分配；
        - 远郊词间 ``_BUCKET_DELAY`` 秒节律；单词失败（限流/网络）跳过不阻断；
        - **市区桶失败 → 返回 ``[]``**（调用方回退老 text 路径），远郊词全空
          不阻断（有市区即可）。

        ``search_plan``（9.2 十二节 A：LLM 定制计划）：结构
        ``{"buckets": [{"keywords": ["长城", "寺"], "quota_ratio": 0.3}]}``——
        - 每个 bucket 是一组类别词（自动拼 ``f"{city} {word}"`` 查询），
          ``quota_ratio`` 是该组占池宽配额比例（0~1，允许缺省等分、允许总和≠1
          按比例归一）；
        - 组内每词独立搜索，词间 ``_BUCKET_DELAY`` 节律；单词/组失败跳过不阻断；
        - 计划结构非法（非 dict / 无有效 bucket / 词全空）→ **回退内置固定词表**
          并 warning（LLM 乱出也不影响候选池产出）；
        - 返回仍为原始 POI 列表（未去重，去重/截断由调用方统一处理）。

        Returns:
            合并后的原始 POI 列表（**未去重**，去重/截断由调用方统一处理）。
        """
        if search_plan:
            buckets = self._normalize_search_plan(search_plan)
            if buckets:
                return self._search_with_plan(city, limit, buckets)
            logger.warning(
                "search_plan 校验失败，回退内置固定词表（%s）", city,
            )

        # ---- 内置固定词表路径（缺省 / 计划无效，9.2 实证行为）----
        pois: List[Dict[str, Any]] = []
        urban_quota = max(1, round(limit * _URBAN_RATIO))
        try:
            urban = self._client.search_poi(
                f"{city} 景点", city=city, limit=urban_quota,
            )
            pois.extend(urban or [])
        except Exception as exc:  # noqa: BLE001  市区桶失败回退老 text 路径
            logger.warning("市区桶搜索失败（%s），回退 text 搜索：%s", city, exc)
            return []

        far_quota = max(
            2, round(limit * (1 - _URBAN_RATIO) / len(_FAR_KEYWORDS))
        )
        for word_index, word in enumerate(_FAR_KEYWORDS):
            if word_index:
                import time as _time
                _time.sleep(_BUCKET_DELAY)
            try:
                bucket = self._client.search_poi(
                    f"{city} {word}", city=city, limit=far_quota,
                )
                pois.extend(bucket or [])
            except Exception as exc:  # noqa: BLE001  单词失败不阻断整体
                logger.warning("远郊词「%s」搜索失败（%s）：%s", word, city, exc)
                continue
        return pois

    @staticmethod
    def _normalize_search_plan(
        search_plan: Dict[str, Any],
    ) -> Optional[List[Tuple[List[str], float]]]:
        """校验/归一化 LLM 搜索计划 → ``[(keywords, quota_ratio)]`` 或 None。

        规则：
        - 顶层必须是 dict 且含非空 ``buckets`` 列表；
        - 每个 bucket：``keywords`` 非空字符串列表（去重、剥空白、剔除含城市
          名的整串——LLM 若给出「北京 长城」也接受，保留原样查询），
          ``quota_ratio`` 数值 0~1（缺省/非法 → 与其他桶等分）；
        - bucket 数 1~8、总词数 ≤ 24（超限截断），全部无效 → None。
        """
        try:
            buckets = search_plan.get("buckets")
        except AttributeError:
            return None
        if not isinstance(buckets, list) or not buckets:
            return None

        cleaned: List[Tuple[List[str], float]] = []
        for bucket in buckets[:8]:
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
                ratio = 0.0  # 缺省/非法 → 等分
            cleaned.append((words, ratio))
        if not cleaned:
            return None

        # 总词数截断（防 LLM 超发请求击穿 QPS 节律预算）
        total_words = sum(len(words) for words, _ in cleaned)
        if total_words > 24:
            logger.warning(
                "search_plan 总词数 %d > 24，截断保留前 24 词", total_words,
            )
            kept: List[Tuple[List[str], float]] = []
            budget = 24
            for words, ratio in cleaned:
                if budget <= 0:
                    break
                take = words[:budget]
                budget -= len(take)
                kept.append((take, ratio))
            cleaned = kept
        return cleaned

    def _search_with_plan(
        self, city: str, limit: int,
        buckets: List[Tuple[List[str], float]],
    ) -> List[Dict[str, Any]]:
        """按 LLM 计划桶搜索：组配额 ``limit×ratio`` 归一 → 组内每词分额查询。"""
        pois: List[Dict[str, Any]] = []
        total_ratio = sum(ratio for _, ratio in buckets) or 1.0
        request_index = 0
        for words, ratio in buckets:
            group_quota = max(
                1, round(limit * (ratio / total_ratio) / len(words))
            )
            for word in words:
                if request_index:
                    import time as _time
                    _time.sleep(_BUCKET_DELAY)
                request_index += 1
                try:
                    bucket = self._client.search_poi(
                        f"{city} {word}", city=city, limit=group_quota,
                    )
                    pois.extend(bucket or [])
                except Exception as exc:  # noqa: BLE001  单词失败不阻断整体
                    logger.warning(
                        "计划词「%s」搜索失败（%s）：%s", word, city, exc,
                    )
                    continue
        return pois