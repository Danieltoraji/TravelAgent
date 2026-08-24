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
             limit: int = 10, city: str = "北京") -> Any:
        if action == "search":
            return self._search_spots(place, limit=limit)
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

    def _search_spots(self, place: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Live 城市候选池（B5）：搜索「城市+景点」→ 返回 A 侧 spot dict 列表。

        - 首次按 ``f"{place} 景点"`` 搜索；空结果回落按城市名搜索；
        - 字段映射：name / location / suggest_duration / opening_time /
          closing_time / price（cost，人均近似）/ tags（type 大类 + tag）/
          rating / alias / address；
        - 高德无建议停留时长，统一给 ``_DEFAULT_DURATION``（A 侧亦有兜底）。
        """
        city = place or ""
        pois = self._client.search_poi(f"{city} 景点", city=city, limit=limit)
        if not pois:
            pois = self._client.search_poi(city, city=city, limit=limit)

        spots: List[Dict[str, Any]] = []
        for index, poi in enumerate(pois):
            open_hours = poi.get("opentime_today", "")
            spots.append(
                {
                    "id": f"scenic_{index}",
                    "name": poi.get("name", city),
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
        return spots