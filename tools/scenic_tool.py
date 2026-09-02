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

# 层一（9.2 十一节）：多锚点分层搜索——城市中心 → 三桶 search_poi_around。
# 桶定义：(radius 米, 配额比例)。市区 60% / 近郊 30% / 远郊 10%，随 limit（days×5）
# 联动再分配空间：如 2 天 10 家 = 6 市区 + 3 近郊 + 1 远郊；7 天 35 家 ≈ 21/10/4。
_BUCKETS = (
    (15000, 0.6),   # 市区桶：radius=15km
    (40000, 0.3),   # 近郊桶：radius=40km（剔除 15km 内已收）
    (80000, 0.1),   # 远郊桶：radius=80km（剔除 40km 内已收）
)
# 各桶剔除半径（米）——前序桶已覆盖的市区/近郊不重复收（around 返回 distance 字段）
_BUCKET_EXCLUDE_RADIUS_M = (0, 15000, 40000)
# 桶间节律（免费 key QPS≈2-3/s，照抄 _PAGE_DELAY）
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
             ensure_spots: Optional[List[str]] = None) -> Any:
        if action == "search":
            return self._search_spots(place, limit=limit, ensure_spots=ensure_spots)
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
        self, place: str, limit: int = 10, ensure_spots: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """Live 城市候选池（B5）：多锚点分层搜索 → 返回 A 侧 spot dict 列表。

        **层一（9.2 十一节：候选池空间覆盖）**：
        - 先 ``geocode`` 城市中心 → 三桶 ``search_poi_around``：市区桶（15km）+
          近郊桶（40km，剔除 15km 内已收）+ 远郊桶（80km，剔除 40km 内已收）——
          远郊优质景点（颐和园/长城/环球影城类）不再被「市中心相关度排序」饿死；
        - 桶配额随 limit（= ``days×5``）联动再分配：市区 60% / 近郊 30% / 远郊 10%
          （如 2 天 10 家 = 6 市区 + 3 近郊 + 1 远郊），池宽总量不变只是空间分层；
        - geocode 失败 / 三桶全空 / 单桶限流 → 回退老 text 路径（``search_poi``
          ``f"{city} 景点"`` → 城市名），保持既有行为不炸；
        - 合并后按 **rating 降序**截断到 limit（质量优先 + 池宽上限）；
        - 桶间 0.3s 节律（``_BUCKET_DELAY``，防免费 key QPS 10021）。

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

        pois = self._layered_search(city, limit)
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

    def _layered_search(self, city: str, limit: int) -> List[Dict[str, Any]]:
        """层一（9.2 十一节）：多锚点分层搜索核心。

        geocode 城市中心 → 按 ``_BUCKETS`` 三桶 ``search_poi_around``：
        - 市区桶 radius=15km → 近郊桶 radius=40km（剔除 15km 内已收）→
          远郊桶 radius=80km（剔除 40km 内已收）；
        - 桶配额随 limit 比例（60%/30%/10%，最小 1）——池宽总量不变，空间再分配；
        - 桶间 ``_BUCKET_DELAY`` 秒节律；单桶失败（限流/网络）跳过不阻断；
        - 任一步失败 / 全空 → 返回 ``[]``（调用方回退老 text 路径）。

        Returns:
            合并后的原始 POI 列表（**未去重**，去重/截断由调用方统一处理）。
        """
        try:
            center = self._client.geocode(city, city=city)
        except Exception as exc:  # noqa: BLE001  geocode 失败回退 text
            logger.warning("分层搜索 geocode 失败（%s），回退 text 搜索：%s", city, exc)
            return []
        if not center:
            return []

        pois: List[Dict[str, Any]] = []
        for bucket_index, (radius_m, ratio) in enumerate(_BUCKETS):
            if bucket_index:
                import time as _time
                _time.sleep(_BUCKET_DELAY)
            quota = max(1, round(limit * ratio))
            try:
                bucket = self._client.search_poi_around(
                    center, radius=radius_m, keywords="景点", limit=quota,
                )
            except Exception as exc:  # noqa: BLE001  单桶限流/失败不阻断整体
                logger.warning(
                    "分层搜索桶 %d 失败（radius=%dm）：%s", bucket_index, radius_m, exc,
                )
                continue
            exclude_m = _BUCKET_EXCLUDE_RADIUS_M[bucket_index]
            for poi in bucket or []:
                # 剔除前序桶已覆盖半径内的 POI（around 返回 distance 字段，单位米）
                dist_m = float(poi.get("distance", 0) or 0)
                if exclude_m and dist_m and dist_m < exclude_m:
                    continue
                pois.append(poi)
        return pois