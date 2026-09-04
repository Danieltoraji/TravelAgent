"""高德地图 API 客户端：统一认证 + HTTP 请求封装。

所有 Live 版地图相关 Tool 共用同一个 AmapClient 实例，
认证方式为 URL query string 中传 ``key`` 参数（不同于和风天气的 header 认证）。

高德 API 基础域名：https://restapi.amap.com
坐标系：GCJ-02（国测局坐标，国内通用）
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("tools.amap")

_AMAP_BASE_URL = "https://restapi.amap.com"

# 批量距离请求的瞬时失败标记（免费 key QPS 超限：10021 CUQPS_HAS_EXCEEDED_THE_LIMIT）
_DISTANCE_TRANSIENT_MARKERS = ("10021", "CUQPS", "QPS", "EXCEEDED", "LIMIT", "429", "TIMEOUT")


def _distance_error_transient(text: str) -> bool:
    """判断 /v3/distance 单次请求错误是否瞬时（QPS 限流等，可退避重试）。"""
    if not text:
        return False
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in _DISTANCE_TRANSIENT_MARKERS)


def _distance_origin_index(raw: Any) -> int:
    """高德 /v3/distance 响应的 ``origin_id`` 是 **1 基**序号（实测 10 起点返回 1..10，
    第 1 个起点为 1）——转成 0 基索引；非法值或 0 返回 -1（由上层跳过，不拖垮矩阵）。"""
    try:
        idx = int(raw) - 1  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return -1
    return idx if idx >= 0 else -1


_DISTANCE_ATTEMPTS = 3            # 单终点最大尝试次数（首次 + 2 次重试）
_DISTANCE_RETRY_BACKOFF = 0.4     # 重试退避秒数
_DISTANCE_INTER_REQUEST_DELAY = 0.3  # 终点间间隔秒数（防批量 QPS 突刺）

# T2（0829）：QPS 类瞬时 infocode——geocode/get_route 命中时退避重试
# （此前仅批量 distance 端点有重试，单点查询被限流即立即失败）
_TRANSIENT_INFOCODES = ("10019", "10020", "10021")
_TRANSIENT_ATTEMPTS = 3           # 首次 + 2 次重试
_TRANSIENT_RETRY_BACKOFF = 0.4    # 退避秒数


class AmapClient:
    """高德地图 API 客户端（API Key 认证）。

    用法::

        client = AmapClient(api_key="your-key")
        lat, lng = client.geocode("故宫")                    # 地理编码
        pois = client.search_poi("故宫")                      # 关键词搜索
        route = client.get_route((39.91, 116.39), (39.92, 116.40), "transit")
    """

    def __init__(self, api_key: str, timeout: float | None = None) -> None:
        from config.settings import settings
        self._api_key = api_key
        self._timeout = timeout if timeout is not None else settings.api_timeout
        self._geocode_cache: Dict[str, Tuple[float, float]] = {}  # 地址 → (lat, lng)

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def _get_with_transient_retry(self, path: str,
                                  params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """T2（0829）：带 QPS 类瞬时错误退避重试的 GET。

        infocode 10019/10020/10021（QPS 限流类）→ 退避重试；非瞬时错误
        （如 10001 key 无效、10003 超限）原样抛出，不改变异常类型。
        """
        last_error: Optional[ValueError] = None
        for attempt in range(_TRANSIENT_ATTEMPTS):
            try:
                return self._get(path, params)
            except ValueError as exc:
                last_error = exc
                message = str(exc)
                if any(code in message for code in _TRANSIENT_INFOCODES) \
                        and attempt < _TRANSIENT_ATTEMPTS - 1:
                    delay = _TRANSIENT_RETRY_BACKOFF * (attempt + 1)
                    logger.warning(
                        "amap 瞬时限流（%s），%.1fs 后重试 %d/%d: %s",
                        path, delay, attempt + 1, _TRANSIENT_ATTEMPTS - 1, message,
                    )
                    time.sleep(delay)
                    continue
                raise
        raise last_error  # pragma: no cover（循环内必 return/raise）

    def geocode(self, address: str, city: str = "") -> Tuple[float, float]:
        """地理编码：地址 → 坐标 (lat, lng)。委托 ``geocode_detail``。"""
        lat, lng, _ = self.geocode_detail(address, city=city)
        return lat, lng

    def geocode_detail(self, address: str, city: str = "") -> Tuple[float, float, str]:
        """地理编码：地址 → (lat, lng, 命中行政区城市名)。

        第三个返回值取自 ``geocodes[0].city``（直辖市可能为空串）——供
        市内路线的**全国搜索兜底城市归属校验**使用（十一节：全国兜底命中
        外省同名 POI → 跨市路线漂移，霸州 87km 实测）。带独立缓存键。
        """
        cache_key = f"detail|{address}|{city}"
        if cache_key in self._geocode_cache:
            return self._geocode_cache[cache_key]

        params: Dict[str, str] = {"address": address}
        if city:
            params["city"] = city
        resp = self._get_with_transient_retry("/v3/geocode/geo", params)

        geocodes = resp.get("geocodes", [])
        if not geocodes:
            raise ValueError(f"高德地理编码未找到地址: {address}")

        g0 = geocodes[0]
        location = g0.get("location", "")  # "116.397428,39.90923"
        if not location:
            raise ValueError(f"高德地理编码返回无坐标: {address}")

        lng_str, lat_str = location.split(",")
        lat, lng = float(lat_str), float(lng_str)
        city_field = g0.get("city")
        if isinstance(city_field, list):
            matched_city = str(city_field[0]) if city_field else ""
        else:
            matched_city = str(city_field or "")
        self._geocode_cache[cache_key] = (lat, lng, matched_city)
        logger.info(
            "Geocode: %s → (%.6f, %.6f) city=%s", address, lat, lng, matched_city
        )
        return lat, lng, matched_city

    # v5 show_fields 固定参数：请求营业时间、评分、人均消费、特色菜等深度信息
    _SHOW_FIELDS = "business,opentime_today,opentime_week,rating,cost,tag,alias"

    # 翻页页间隔（秒）：免费 key QPS≈2-3/s，连续翻页必触发 10021 限流
    _PAGE_DELAY = 0.3

    def search_poi(
        self,
        query: str,
        city: str = "",
        types: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """关键词搜索 POI（支持翻页，突破单页 25 上限）。

        调用 ``/v5/place/text``（v5 API），返回标准化 POI 列表。
        v5 通过 ``show_fields`` 参数返回营业时间、评分等深度信息。

        翻页规则（8.30 候选池扩容）：``limit > 25`` 时按 ``page_size=25``
        逐页拉取（``page_num`` 1 基），页间 ``_PAGE_DELAY`` 秒防 QPS；
        某页结果不足一页（尾页）即停。全程经 ``_get_with_transient_retry``
        （QPS 类瞬时错误退避重试），非瞬时错误原样抛出。
        """
        collected: List[Dict[str, Any]] = []
        remaining = int(limit)
        page_num = 1
        while remaining > 0:
            page_size = min(remaining, 25)
            params: Dict[str, str] = {
                "keywords": query,
                "page_num": str(page_num),
                "page_size": str(page_size),
                "show_fields": self._SHOW_FIELDS,
            }
            if city:
                params["region"] = city
                params["city_limit"] = "true"
            if types:
                params["types"] = types

            if page_num > 1:
                time.sleep(self._PAGE_DELAY)
            resp = self._get_with_transient_retry("/v5/place/text", params)
            pois = resp.get("pois", []) or []
            collected.extend(self._normalize_poi(p) for p in pois[:remaining])
            if len(pois) < page_size:
                break  # 尾页：再翻也是空，避免多余请求
            remaining -= len(pois)
            page_num += 1
        return collected[:limit]

    def search_poi_around(
        self,
        location: Tuple[float, float],
        radius: int = 1000,
        keywords: str = "",
        types: str = "",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """周边搜索 POI。

        调用 ``/v5/place/around``（v5 API），返回标准化 POI 列表。
        v5 通过 ``show_fields`` 参数返回营业时间、评分等深度信息。

        Args:
            location: 中心点坐标 (lat, lng)
            radius: 搜索半径（米）
            keywords: 搜索关键词（可选）
            types: POI 类型编码（可选）
            limit: 返回数量上限
        """
        params: Dict[str, str] = {
            "location": f"{location[1]},{location[0]}",  # 高德格式: lng,lat
            "radius": str(radius),
            "page_size": str(min(limit, 25)),
            "sortrule": "distance",
            "show_fields": self._SHOW_FIELDS,
        }
        if keywords:
            params["keywords"] = keywords
        if types:
            params["types"] = types

        resp = self._get("/v5/place/around", params)
        pois = resp.get("pois", [])
        return [self._normalize_poi(p) for p in pois[:limit]]

    def get_route(
        self,
        origin: Tuple[float, float],
        destination: Tuple[float, float],
        mode: str = "transit",
        city: str = "北京",
    ) -> Dict[str, Any]:
        """路线规划：返回距离（米）和耗时（秒）。

        根据 ``mode`` 调用不同端点：
        - ``transit``  → /v3/direction/transit/integrated（公交）
        - ``driving``  → /v3/direction/driving（驾车）
        - ``riding``   → /v4/direction/bicycling（骑行）
        - ``walk``     → /v3/direction/walking（步行）

        Returns:
            ``{"distance": int, "duration": int}`` — 距离（米）、耗时（秒）
        """
        origin_str = f"{origin[1]},{origin[0]}"          # lng,lat
        dest_str = f"{destination[1]},{destination[0]}"

        if mode == "transit":
            params = {
                "origin": origin_str,
                "destination": dest_str,
                "city": city,
            }
            resp = self._get_with_transient_retry("/v3/direction/transit/integrated", params)
            return self._extract_transit_route(resp)

        if mode == "driving":
            params = {"origin": origin_str, "destination": dest_str}
            resp = self._get_with_transient_retry("/v3/direction/driving", params)
            return self._extract_driving_route(resp)

        if mode == "riding":
            params = {"origin": origin_str, "destination": dest_str}
            resp = self._get_with_transient_retry("/v4/direction/bicycling", params)
            return self._extract_bicycling_route(resp)

        if mode == "walk":
            params = {"origin": origin_str, "destination": dest_str}
            resp = self._get_with_transient_retry("/v3/direction/walking", params)
            return self._extract_walking_route(resp)

        raise ValueError(f"不支持的路线模式: {mode}")

    def get_distances(
        self,
        origins: List[Tuple[float, float]],
        destinations: List[Tuple[float, float]],
        mode: str = "driving",
    ) -> List[Dict[str, Any]]:
        """批量距离测量（行程矩阵用）：多起点 × 多终点的距离/时长。

        调用 ``/v3/distance``（``type=1`` 驾车 / ``type=2`` 步行）：
        一次请求最多 **100 个起点 + 1 个终点**（高德限制），因此对每个终点各发
        一次请求，共 ``len(destinations)`` 次，拿到 ``N×M`` 对结果——
        相比逐对 ``get_route`` 大幅减少请求数（矩阵 O(n²) 场景的关键）。

        Args:
            origins: 起点坐标列表 `[(lat, lng), ...]`
            destinations: 终点坐标列表 `[(lat, lng), ...]`
            mode: "driving"（驾车，默认）或 "walk"（步行）——批量用直线距离度量
                API，公交模式无对应批量端点，需逐对走 ``get_route``

        Returns:
            ``[{"origin": (lat,lng), "destination": (lat,lng),
                "distance_m": int, "duration_s": int}, ...]``
        """
        origin_list = [tuple(o) for o in origins]
        dest_list = [tuple(d) for d in destinations]
        if not origin_list or not dest_list:
            return []
        if mode not in {"driving", "walk"}:
            raise ValueError(
                f"批量距离测量仅支持 driving / walk，当前 mode={mode!r}；"
                "公交等其它模式请逐对调用 get_route"
            )
        measure_type = "2" if mode == "walk" else "1"

        rows: List[Dict[str, Any]] = []
        for destination in dest_list:
            # /v3/distance 一次最多 100 个起点，超出则分批
            for chunk_start in range(0, len(origin_list), 100):
                chunk = origin_list[chunk_start : chunk_start + 100]
                origins_str = "|".join(f"{o[1]},{o[0]}" for o in chunk)  # lng,lat
                dest_str = f"{destination[1]},{destination[0]}"
                resp = self._distance_request(
                    origins_str, dest_str, measure_type
                )
                if resp is None:
                    continue  # 该终点瞬时失败已跳过 → 矩阵缺行由上层单边降级
                for item in self._extract_distance_rows(resp):
                    origin_index = chunk_start + item["origin_id"]
                    if origin_index < 0 or origin_index >= len(origin_list):
                        # 异常响应（origin_id 越界/非法）不应拖垮整个矩阵，跳过并告警
                        logger.warning(
                            "distance 响应 origin_id 越界，跳过: %s", item,
                        )
                        continue
                    rows.append(
                        {
                            "origin": origin_list[origin_index],
                            "destination": destination,
                            "distance_m": item["distance_m"],
                            "duration_s": item["duration_s"],
                        }
                    )
            # 8.25：免费 key 批量接口 QPS 低，终点间加间隔防突刺 10021
            time.sleep(_DISTANCE_INTER_REQUEST_DELAY)
        return rows

    def _distance_request(
        self, origins_str: str, dest_str: str, measure_type: str
    ) -> Optional[Dict[str, Any]]:
        """单次 ``/v3/distance`` 请求：瞬时 10021 退避重试；仍失败跳过（不拖垮整矩阵）。"""
        last_error: Optional[Exception] = None
        for attempt in range(_DISTANCE_ATTEMPTS):
            try:
                return self._get(
                    "/v3/distance",
                    {"origins": origins_str, "destination": dest_str, "type": measure_type},
                )
            except ValueError as exc:
                last_error = exc
                if _distance_error_transient(str(exc)) and attempt < _DISTANCE_ATTEMPTS - 1:
                    time.sleep(_DISTANCE_RETRY_BACKOFF)
                    continue
                # 非瞬时错误 → 直接跳过该终点（单终点失败不应拖垮整矩阵）
                break
        logger.warning(
            "distance 请求失败后跳过该终点：%s（%s）",
            dest_str, last_error,
        )
        return None

    @staticmethod
    def _extract_distance_rows(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
        """从 ``/v3/distance`` 响应中提取每一对的距离/时长。

        响应 ``results`` 每项含 ``origin_id``（0 起索引）/ ``distance``（米）/
        ``duration``（秒）；缺字段或非法数值的项跳过。
        """
        rows: List[Dict[str, Any]] = []
        for item in resp.get("results", []):
            raw_distance = item.get("distance")
            raw_duration = item.get("duration")
            if raw_distance in (None, "") or raw_duration in (None, ""):
                continue  # 空值项视为非法，跳过（同点合法值 0 保留）
            try:
                rows.append(
                    {
                        # 高德 origin_id 是 1 基序号 → 转 0 基索引（_distance_origin_index）
                        "origin_id": _distance_origin_index(item.get("origin_id")),
                        "distance_m": int(float(raw_distance)),
                        "duration_s": int(float(raw_duration)),
                    }
                )
            except (TypeError, ValueError):
                continue
        return rows

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _get(self, path: str, params: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
        """发送 GET 请求，自动附加 ``key`` 参数，返回解析后的 JSON dict。

        v3 API 的错误通过 ``status != "1"`` 判断；
        v4 API（如骑行）的返回结构不同，数据在 ``data`` 字段中，
        错误通过 ``errcode`` 判断。
        """
        all_params = {"key": self._api_key}
        if params:
            all_params.update(params)

        url = f"{_AMAP_BASE_URL}{path}?{urlencode(all_params)}"
        req = Request(url)
        req.add_header("Accept-Encoding", "gzip")
        logger.debug("GET %s", url)

        try:
            with urlopen(req, timeout=self._timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                data = json.loads(raw)
        except URLError as exc:
            raise ConnectionError(f"高德 API 请求失败 [{url}]: {exc}") from exc

        # v4 API（如 /v4/direction/bicycling）返回结构不同
        if path.startswith("/v4/"):
            # v4 错误检查：errcode != 0 表示失败
            errcode = data.get("errcode", 0)
            if errcode != 0:
                err_msg = data.get("errmsg", "unknown")
                raise ValueError(f"高德 v4 API 错误 [{errcode}]: {err_msg}")
            # v4 数据在 data 字段中
            return data.get("data", {})

        # v3 API 错误检查：status != "1" 表示失败
        if data.get("status") != "1":
            err_code = data.get("infocode", "unknown")
            err_msg = data.get("info", "unknown")
            raise ValueError(f"高德 API 错误 [{err_code}]: {err_msg}")

        return data

    @staticmethod
    def _normalize_poi(poi: Dict[str, Any]) -> Dict[str, Any]:
        """将高德 POI 原始返回标准化为项目内部结构。

        v5 API（``/v5/place/*``）通过 ``show_fields=business,...`` 返回深度信息，
        深度字段嵌套在 ``business`` 对象内：
        - rating: 评分（餐饮/酒店/景点/影院类 POI）
        - cost: 人均消费（餐饮/酒店/景点/影院类 POI）
        - tag: 特色内容（美食类 POI，如"烤鱼,麻辣香锅"）
        - opentime_today: 今日营业时间（如 "08:30-17:30"）
        - opentime_week: 周营业时间描述（如 "周一至周五:08:30-17:30..."）

        v5 结构（show_fields=business,... 时）：
        - business.rating: 评分（餐饮/酒店/景点/影院类 POI）
        - business.cost: 人均消费（餐饮/酒店/景点/影院类 POI）
        - business.tag: 特色内容（美食类 POI，如"烤鱼,麻辣香锅"）
        - business.opentime_today: 今日营业时间（如 "08:30-17:30"）
        - business.opentime_week: 周营业时间描述
        - business.tel: 联系电话（v5 移入 business 内）

        兼容 v3 API：当 ``biz_ext`` 存在时作为 fallback。
        注意：高德返回值有时是字符串有时是空数组 []，需安全转换。
        """
        location = poi.get("location", "")  # "lng,lat"
        lat, lng = 0.0, 0.0
        if location and "," in location:
            parts = location.split(",")
            lng = float(parts[0])
            lat = float(parts[1])

        # v5: 深度信息嵌套在 business 对象内
        business = poi.get("business", {})
        if not isinstance(business, dict):
            business = {}

        # v3 fallback: biz_ext 嵌套结构
        biz_ext = poi.get("biz_ext", {})
        if not isinstance(biz_ext, dict):
            biz_ext = {}

        def _safe_float(val: Any) -> float:
            """安全转换为 float，处理空数组/空字符串/None。"""
            if isinstance(val, (list, tuple)):
                return 0.0
            if val is None or val == "":
                return 0.0
            try:
                return float(val)
            except (ValueError, TypeError):
                return 0.0

        # v5 business 优先，fallback 到 v3 biz_ext
        rating = business.get("rating")
        if rating is None:
            rating = biz_ext.get("rating", 0)

        cost = business.get("cost")
        if cost is None:
            cost = biz_ext.get("cost", 0)

        # tel: v5 在 business 内，v3 在顶层
        tel = business.get("tel") or poi.get("tel", "") or ""

        # tag: v5 在 business 内，v3 在顶层
        tag = business.get("tag") or poi.get("tag", "") or ""

        # opentime: v5 在 business 内
        opentime_today = business.get("opentime_today", "") or ""
        opentime_week = business.get("opentime_week", "") or ""

        # alias: v5 在 business 内（POI 别名，如 "紫禁城"）
        alias = business.get("alias", "") or ""

        # business_area: v5 在 business 内（POI 所属商圈）
        business_area = business.get("business_area", "") or ""

        return {
            "name": poi.get("name", ""),
            "lat": lat,
            "lng": lng,
            "address": poi.get("address", "") or "",
            "tel": tel,
            "type": poi.get("type", "") or "",
            "rating": _safe_float(rating),
            "cost": _safe_float(cost),
            "tag": tag,
            "distance": _safe_float(poi.get("distance", 0)),
            "opentime_today": opentime_today,
            "opentime_week": opentime_week,
            "alias": alias,
            "business_area": business_area,
        }

    @staticmethod
    def _extract_transit_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从公交路线规划响应中提取距离/耗时/票价 + 具体线路导航。

        2026-09-01：新增 ``transit_text``（如「步行858m → 124路 2站 → 步行398m」，
        逐段拼接公交线路与步行距离）与 ``walking_m``（步行总距离），
        供 C 端展示公共交通具体信息与路程。
        """
        route = resp.get("route", {})
        transit = route.get("transits", [])
        if not transit:
            raise ValueError("高德公交路线规划返回为空")
        first = transit[0]
        segments = first.get("segments") or []
        text_parts: List[str] = []
        walking_total = 0
        for seg in segments:
            walking = seg.get("walking") or {}
            walk_m = int(float(walking.get("distance", 0) or 0))
            if walk_m > 0:
                walking_total += walk_m
                text_parts.append(f"步行{walk_m}m")
            bus = seg.get("bus") or {}
            for bl in bus.get("buslines") or []:
                name = str(bl.get("name") or "").strip()
                if not name:
                    continue
                part = name
                try:
                    via = int(bl.get("via_num", 0) or 0)
                    if via > 0:
                        part += f" {via + 1}站"
                except (TypeError, ValueError):
                    pass  # via_num 异常时不显示站数
                text_parts.append(part)
        transit_text = " → ".join(text_parts) if text_parts else ""
        try:
            walking_m = int(float(first.get("walking_distance", 0) or 0))
        except (TypeError, ValueError):
            walking_m = walking_total
        if walking_m <= 0:
            walking_m = walking_total
        return {
            "distance": int(float(first.get("distance", 0) or 0)),
            "duration": int(float(first.get("duration", 0) or 0)),
            "cost": int(float(first.get("cost", 0) or 0)),
            "transit_text": transit_text,
            "walking_m": walking_m,
        }

    @staticmethod
    def _extract_driving_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从驾车路线规划响应中提取距离和耗时。"""
        route = resp.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            raise ValueError("高德驾车路线规划返回为空")
        first = paths[0]
        return {
            "distance": int(float(first.get("distance", 0) or 0)),
            "duration": int(float(first.get("duration", 0) or 0)),
            "tolls": int(float(first.get("tolls", 0) or 0)),
        }

    @staticmethod
    def _extract_bicycling_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从骑行路线规划响应中提取距离和耗时。

        v4 API 的数据已在 ``_get()`` 中解包到顶层，
        paths 直接在返回的 dict 中。
        """
        paths = resp.get("paths", [])
        if not paths:
            raise ValueError("高德骑行路线规划返回为空")
        first = paths[0]
        return {
            "distance": int(float(first.get("distance", 0) or 0)),
            "duration": int(float(first.get("duration", 0) or 0)),
        }

    @staticmethod
    def _extract_walking_route(resp: Dict[str, Any]) -> Dict[str, Any]:
        """从步行路线规划响应中提取距离和耗时。"""
        route = resp.get("route", {})
        paths = route.get("paths", [])
        if not paths:
            raise ValueError("高德步行路线规划返回为空")
        first = paths[0]
        return {
            "distance": int(float(first.get("distance", 0) or 0)),
            "duration": int(float(first.get("duration", 0) or 0)),
        }

    @property
    def api_key(self) -> str:
        return self._api_key
