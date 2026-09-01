"""地名归一化层 PlaceNormalizer（架构整理方案 P3.1，8.30 贵港事故驱动）。

**事故**：LLM 备注解析把出发地解析成「广西贵港」（带省份前缀）→ 12306
站名解析只认「贵港」→ 城际真源全 error → 候选 0 条 → 静默 driving 1360min
兜底。名称不对齐问题预估大量存在（用户输入「长三角」同理），换成 LLM 调
tool 后只会更高频（模型自由生成参数，地名写法更放飞），必须有专门一层。

**定位**：归一化是工具层职责，不是调用者职责。任何来源的地名（LLM 生成、
用户输入、上游字段）一律视为不可信自由文本，进工具层前先过本层。

四个真源规范形不同（12306 站名 / juhe IATA 码 / 高德坐标 / 估算表城市），
本层输出**规范地名 + 所属城市 + 结构化结果**（``NormalizeResult``）。
数据源分两层（保持 A 侧离线可测）：

- **城市级默认内置**：估算表城市（17）∪ 航路城市（41）——数据都在 A 侧
  ``data_transmission``；
- **站级/全量注入式**：12306 站表是 B 侧资产（``tools/train/``，含
  ``Station.city`` 字段——方案 §P3.1 明说的「现成资产」）。B 侧接线时注入
  ``extra_cities``（12306 city 集）与 ``station_resolver``（城市 → 车站
  列表）；A 侧单测注入小集合验证剥离/反查逻辑，不依赖 B 侧文件。

匹配顺序（方案 §P3.1）：
1. 精确匹配（城市集 / 别名表）；
2. 前后缀剥离：省/市/区/站/机场（「广西贵港」→「贵港」；「张掖西站」→
   剥「站」→「张掖西」——保留站名，``city`` 回填前缀城市「张掖」）；
3. 别名表（「帝都」→北京，随用随补）；
4. rapidfuzz 模糊匹配 → top-3 候选（**不直接采信**，进结构化错误回传，
   供 agent loop 让模型下一轮修正）。

区域名（一名多地）单独处理，不混入归一化：「长三角」是**展开**不是对齐 →
显示能力 ``expand_region``（词典起步，LLM 架构下天然是一个工具）。

结构化错误（LLM 自愈前提）：未知地名返回 ``{"error": "place_not_found",
"input", "candidates", "hint"}``——agent loop 据此让模型下一轮自行修正重试，
错误从「往下级联成静默兜底」变成「被回路消费掉」。

验收用例（方案 §P3.1）：「广西贵港」→ 贵港（前后缀剥离）；「贵港」→
该市全部车站（city 反查）；「长三角」→ 展开列表；「张掖西站」→ 张掖西
（站后缀剥离，保留站名）；未知地名 → 结构化错误含候选。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Set, Tuple

from data_transmission.air_routes import load_air_routes
from data_transmission.city_travel import load_city_travel_options

logger = logging.getLogger("data_transmission.place_normalizer")

# 行政区划后缀（「贵港市」→「贵港」；「广西壮族自治区」→ 先剥后缀再剥省前缀）
_SUFFIX_PATTERNS = (
    re.compile(r"^(.*?)(?:省|市|自治区|特别行政区|自治州|自治县|区|县|镇|乡|盟|旗)$"),
)
# 设施后缀（「张掖西站」→「张掖西」；剥后站名保留，城市回填前缀）
_STATION_SUFFIX = re.compile(r"^(.*?)(?:火车站|高铁站|汽车站|站)$")
# 省级前缀（剥「广西贵港」的「广西」；注意「广西」是省级名不能剥成「西」）
_PROVINCE_PREFIXES = (
    "北京", "上海", "天津", "重庆",
    "河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江", "安徽", "福建",
    "江西", "山东", "河南", "湖北", "湖南", "广东", "海南", "四川", "贵州",
    "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆",
    "香港", "澳门",
)

# 别名表（随用随补；LLM/用户自由文本 → 标准城市名）
ALIASES: Dict[str, str] = {
    "帝都": "北京",
    "魔都": "上海",
    "羊城": "广州",
    "鹏城": "深圳",
    "蓉城": "成都",
    "锦官城": "成都",
    "春城": "昆明",
    "山城": "重庆",
    "泉城": "济南",
    "榕城": "福州",
    "申城": "上海",
    "杭城": "杭州",
}

# 区域展开词典（一名多地：展开 ≠ 对齐，独立能力）
_REGIONS: Dict[str, Tuple[str, ...]] = {
    "长三角": ("上海", "苏州", "杭州", "南京", "无锡", "宁波", "合肥", "嘉兴", "绍兴", "南通"),
    "珠三角": ("广州", "深圳", "珠海", "佛山", "东莞", "中山", "惠州", "江门"),
    "京津冀": ("北京", "天津", "石家庄", "唐山", "保定", "廊坊", "秦皇岛", "承德", "张家口"),
    "大湾区": ("香港", "澳门", "广州", "深圳", "珠海", "佛山", "惠州", "东莞", "中山", "江门", "肇庆"),
}


@dataclass(frozen=True)
class NormalizeResult:
    """归一化结果：规范地名 + 所属城市 + 命中路径。

    ``canonical`` 规范地名：城市级命中 = 标准城市名（如「贵港」）；站名剥离后
    命中 = 站名本身（如「张掖西」——它是「张掖」的站，不是城市名）。
    ``city`` 所属城市（站名结果回填前缀城市；城市结果 = 自身）。
    ``method``：exact / strip / alias / fuzzy / station。
    ``stations`` 城市 → 车站列表（``station_resolver`` 注入时非空）。
    ``fuzzy_candidates`` 模糊 top-3（不直接采信）。
    """

    canonical: str
    matched: bool
    method: str = "exact"
    city: str = ""
    raw: str = ""
    stations: Tuple[str, ...] = ()
    fuzzy_candidates: Tuple[str, ...] = ()

    def to_dict(self) -> Dict[str, object]:
        return {
            "canonical": self.canonical,
            "matched": self.matched,
            "method": self.method,
            "city": self.city,
            "raw": self.raw,
            "stations": list(self.stations),
            "fuzzy_candidates": list(self.fuzzy_candidates),
        }


def _place_not_found(
    raw: str,
    candidates: Tuple[str, ...] = (),
    hint: str = "",
) -> Dict[str, object]:
    """结构化错误（机器可消费，LLM 自愈前提）——不是给人看的 ValueError。"""
    return {
        "error": "place_not_found",
        "input": raw,
        "candidates": list(candidates[:3]),
        "hint": hint or "无法识别地名，请检查拼写后重试（城市名，不含省级行政区前缀）",
    }


class PlaceNormalizer:
    """地名归一化器：自由文本 → 规范地名（城市级内置 + 站级注入）。

    城市集合 = 估算表城市 ∪ 航路城市 ∪ ``extra_cities``（B 侧接线注入
    12306 city 集）。``station_resolver`` / ``extra_stations`` 可选注入；
    无注入时站级能力降级（城市级归一照常，A 侧单测用小集合覆盖）。
    """

    def __init__(
        self,
        station_resolver: Optional[Callable[[str], List[str]]] = None,
        extra_cities: Optional[Set[str]] = None,
        extra_stations: Optional[Set[str]] = None,
        aliases: Optional[Dict[str, str]] = None,
        regions: Optional[Dict[str, Tuple[str, ...]]] = None,
    ) -> None:
        self._cities: Set[str] = self._load_city_set() | set(extra_cities or ())
        self._stations: Set[str] = set(extra_stations or ())
        self._aliases = dict(aliases or ALIASES)
        self._regions = dict(regions or _REGIONS)
        self._station_resolver = station_resolver

    # ------------------------------------------------------------------
    # 数据源
    # ------------------------------------------------------------------

    @staticmethod
    def _load_city_set() -> Set[str]:
        cities = set()
        cities.update(
            c for pair in load_city_travel_options().keys() for c in pair
        )
        try:
            routes = load_air_routes()
            for hint in routes.hints:
                if hint.origin_city:
                    cities.add(hint.origin_city)
                if hint.destination_city:
                    cities.add(hint.destination_city)
        except Exception as exc:  # noqa: BLE001  航路表缺失不阻断城市级归一
            logger.warning("加载航路城市失败，仅用估算表城市：%s", exc)
        return cities

    # ------------------------------------------------------------------
    # 归一化主入口
    # ------------------------------------------------------------------

    def normalize(self, text: str) -> NormalizeResult:
        """自由文本地名 → 规范地名（精确 → 剥离 → 别名 → 模糊）。"""
        raw = (text or "").strip()
        if not raw:
            return NormalizeResult(
                canonical="", matched=False, method="empty", raw=raw,
            )
        # 1. 精确城市匹配
        if raw in self._cities:
            return NormalizeResult(
                canonical=raw, matched=True, method="exact", city=raw, raw=raw,
                stations=self._stations_of(raw),
            )
        # 2a. 站名剥离（「张掖西站」→「张掖西」：保留站名，城市回填前缀）
        station_result = self._match_station(raw)
        if station_result is not None:
            return station_result
        # 2b. 行政区划前后缀剥离（「广西贵港」→「贵港」；「贵港市」→「贵港」）
        stripped = self._strip_affixes(raw)
        if stripped and stripped != raw:
            if stripped in self._cities:
                return NormalizeResult(
                    canonical=stripped, matched=True, method="strip",
                    city=stripped, raw=raw,
                    stations=self._stations_of(stripped),
                )
            parent = self._match_city_prefix(stripped)
            if parent is not None:
                return NormalizeResult(
                    canonical=stripped, matched=True, method="strip",
                    city=parent, raw=raw,
                    stations=self._stations_of(parent),
                )
        # 3. 别名表
        alias = self._aliases.get(raw)
        if alias is not None and alias in self._cities:
            return NormalizeResult(
                canonical=alias, matched=True, method="alias", city=alias, raw=raw,
                stations=self._stations_of(alias),
            )
        # 4. 模糊匹配 → top-3 候选（不直接采信，进结构化错误回传）
        fuzzy = self._fuzzy_top3(raw)
        if fuzzy:
            return NormalizeResult(
                canonical="", matched=False, method="fuzzy", raw=raw,
                fuzzy_candidates=fuzzy,
            )
        return NormalizeResult(
            canonical="", matched=False, method="none", raw=raw,
        )

    # ------------------------------------------------------------------
    # 站级能力（注入式）
    # ------------------------------------------------------------------

    def _match_station(self, raw: str) -> Optional[NormalizeResult]:
        """「张掖西站」→ 剥「站」→「张掖西」：若剥后文本命中站名集 / 城市集 /
        前缀城市（「张掖火车站」→「张掖」是城市本身），返回站名结果
        （canonical=站名或城市，city=城市），否则 None。"""
        m = _STATION_SUFFIX.match(raw)
        if not m or not m.group(1) or m.group(1) == raw:
            return None
        station = m.group(1)
        if station in self._stations:
            return NormalizeResult(
                canonical=station, matched=True, method="station",
                city=self._match_city_prefix(station) or "",
                raw=raw,
            )
        if station in self._cities:
            # 「张掖火车站」→「张掖」：剥设施后缀即得城市本身
            return NormalizeResult(
                canonical=station, matched=True, method="station",
                city=station, raw=raw,
                stations=self._stations_of(station),
            )
        parent = self._match_city_prefix(station)
        if parent is not None:
            return NormalizeResult(
                canonical=station, matched=True, method="station", city=parent,
                raw=raw,
            )
        return None

    def _stations_of(self, city: str) -> Tuple[str, ...]:
        if self._station_resolver is None:
            return ()
        try:
            stations = self._station_resolver(city) or []
            return tuple(dict.fromkeys(stations))  # 去重保序
        except Exception as exc:  # noqa: BLE001  站表异常不阻断归一化
            logger.warning("站级反查失败（%s）：%s", city, exc)
            return ()

    # ------------------------------------------------------------------
    # 子步骤
    # ------------------------------------------------------------------

    def _strip_affixes(self, raw: str) -> str:
        """剥行政区划后缀 + 省级前缀（「广西贵港市」→「贵港」）。"""
        out = raw
        m = _SUFFIX_PATTERNS[0].match(out)
        if m and m.group(1):
            out = m.group(1)
        for province in _PROVINCE_PREFIXES:
            if out.startswith(province) and len(out) > len(province):
                rest = out[len(province):]
                if rest:
                    out = rest
                    break
        if out.endswith("市") and len(out) > 2 and out[:-1] in self._cities:
            out = out[:-1]
        return out.strip()

    def _match_city_prefix(self, text: str) -> Optional[str]:
        """「张掖西」→ 城市前缀命中「张掖」（站名 → 所属城市；最长前缀防歧义）。"""
        best: Optional[str] = None
        for city in self._cities:
            if text.startswith(city) and len(text) > len(city):
                if best is None or len(city) > len(best):
                    best = city
        return best

    def _fuzzy_top3(self, raw: str) -> Tuple[str, ...]:
        """rapidfuzz 模糊匹配 → top-3 候选（不直接采信）。"""
        try:
            from rapidfuzz import fuzz
        except ImportError:
            return ()
        scored = sorted(
            ((fuzz.ratio(raw, c), c) for c in self._cities),
            key=lambda pair: pair[0],
            reverse=True,
        )
        top = [c for score, c in scored if score >= 60][:3]
        return tuple(top)

    # ------------------------------------------------------------------
    # 结构化错误 + 区域展开
    # ------------------------------------------------------------------

    def error(self, raw: str) -> Dict[str, object]:
        """未识别地名 → 结构化错误（机器可消费，供 agent loop 自愈）。"""
        result = self.normalize(raw)
        return _place_not_found(
            raw,
            candidates=result.fuzzy_candidates,
            hint=(
                f"无法识别地名，最近候选：{'、'.join(result.fuzzy_candidates)}；"
                "请用标准城市名（不含省级行政区前缀）重试"
                if result.fuzzy_candidates else
                "无法识别地名，请检查拼写后重试（城市名，不含省级行政区前缀）"
            ),
        )

    def expand_region(self, text: str) -> Tuple[str, ...]:
        """区域名 → 城市列表（一名多地：展开 ≠ 对齐，独立能力）。"""
        key = (text or "").strip().replace("地区", "").replace("区域", "")
        return self._regions.get(key, ())


def build_place_normalizer(
    station_resolver: Optional[Callable[[str], List[str]]] = None,
    extra_cities: Optional[Set[str]] = None,
    extra_stations: Optional[Set[str]] = None,
) -> PlaceNormalizer:
    """工厂：构建带默认数据源的 PlaceNormalizer（B 侧接线时注入 12306 站表能力）。"""
    return PlaceNormalizer(
        station_resolver=station_resolver,
        extra_cities=extra_cities,
        extra_stations=extra_stations,
    )