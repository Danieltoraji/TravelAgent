"""机场表：城市/三字码/中文名互转（照 ``tools/train/stations.py`` 模式）。

数据为内置精简表（覆盖主要枢纽 + 本项目目标支线机场：张掖甘州 YZY、
常州奔牛 CZX、锦州 JNZ 等），随包内置避免运行时拉取；首次使用时懒加载。

用途
----
- ``resolve_city_airport(city)``：城市名（可含「市」后缀 / 机场名）→ 首选机场三字码；
- ``resolve_airport(value)``：三字码 / 中文名 / 城市名 → 三字码（无法识别抛 ValueError）；
- ``airport_name(code)``：三字码 → 中文名（含机场后缀，如「北京首都」→「北京首都国际机场」）。

数据注意：本表只为「城市 → 机场三字码」查询与展示服务，**不构成时刻/票价真源**；
航班时刻与票价由 FlightClient 调数据源（juhe / aviationstack）获取。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional

logger = logging.getLogger("tools.flight.airports")

# IATA 三字码 | 中文名 | 城市 | 简拼 | 类型（major=主枢纽/inter=国际支线/local=支线）
_AIRPORT_ROWS = [
    # -- 主要枢纽（major）--
    ("PEK", "北京首都", "北京", "beijing", "major"),
    ("PKX", "北京大兴", "北京", "daxing", "major"),
    ("SHA", "上海虹桥", "上海", "hongqiao", "major"),
    ("PVG", "上海浦东", "上海", "pudong", "major"),
    ("CAN", "广州白云", "广州", "guangzhou", "major"),
    ("SZX", "深圳宝安", "深圳", "shenzhen", "major"),
    ("CTU", "成都双流", "成都", "shuangliu", "major"),
    ("TFU", "成都天府", "成都", "tianfu", "major"),
    ("TSN", "天津滨海", "天津", "tianjin", "major"),
    ("XIY", "西安咸阳", "西安", "xian", "major"),
    ("HGH", "杭州萧山", "杭州", "hangzhou", "major"),
    ("NKG", "南京禄口", "南京", "nanjing", "major"),
    ("WUH", "武汉天河", "武汉", "wuhan", "major"),
    ("CKG", "重庆江北", "重庆", "chongqing", "major"),
    ("KMG", "昆明长水", "昆明", "kunming", "major"),
    ("LHW", "兰州中川", "兰州", "lanzhou", "major"),
    ("URC", "乌鲁木齐地窝堡", "乌鲁木齐", "wulumuqi", "major"),
    ("SHE", "沈阳桃仙", "沈阳", "shenyang", "major"),
    ("HRB", "哈尔滨太平", "哈尔滨", "haerbin", "major"),
    ("CSX", "长沙黄花", "长沙", "changsha", "major"),
    ("XMN", "厦门高崎", "厦门", "xiamen", "major"),
    ("FOC", "福州长乐", "福州", "fuzhou", "major"),
    ("NNG", "南宁吴圩", "南宁", "nanning", "major"),
    ("CGO", "郑州新郑", "郑州", "zhengzhou", "major"),
    ("TYN", "太原武宿", "太原", "taiyuan", "major"),
    ("SJW", "石家庄正定", "石家庄", "shijiazhuang", "major"),
    ("DLC", "大连周水子", "大连", "dalian", "major"),
    ("TAO", "青岛胶东", "青岛", "qingdao", "major"),
    ("TNA", "济南遥墙", "济南", "jinan", "major"),
    ("HET", "呼和浩特白塔", "呼和浩特", "huhehaote", "major"),
    ("INC", "银川河东", "银川", "yinchuan", "major"),
    ("WNZ", "温州龙湾", "温州", "wenzhou", "inter"),
    ("NGB", "宁波栎社", "宁波", "ningbo", "inter"),
    ("WUX", "苏南硕放", "无锡", "wuxi", "inter"),
    ("KWE", "贵阳龙洞堡", "贵阳", "guiyang", "inter"),
    ("LXA", "拉萨贡嘎", "拉萨", "lasa", "inter"),
    ("XNN", "西宁曹家堡", "西宁", "xining", "inter"),
    ("XUZ", "徐州观音", "徐州", "xuzhou", "inter"),
    ("NTG", "南通兴东", "南通", "nantong", "inter"),
    ("LYG", "连云港花果山", "连云港", "lianyungang", "inter"),
    # -- 目标支线（本项目验证对象）--
    ("YZY", "张掖甘州", "张掖", "zhangye", "local"),
    ("CZX", "常州奔牛", "常州", "changzhou", "local"),
    ("JNZ", "锦州湾", "锦州", "jinzhou", "local"),
    ("YZJ", "扬州泰州", "扬州", "yangzhou", "local"),
    ("HYN", "台州路桥", "台州", "taizhou", "local"),
    ("AQG", "安庆天柱山", "安庆", "anqing", "local"),
    ("JJN", "泉州晋江", "泉州", "quanzhou", "local"),
    ("ZYI", "遵义新舟", "遵义", "zunyi", "local"),
    ("MIG", "绵阳南郊", "绵阳", "mianyang", "local"),
    ("NGQ", "阿里昆莎", "阿里", "ali", "local"),
]


@dataclass(frozen=True)
class Airport:
    iata: str       # 三字码，如 PEK
    name: str       # 中文名（不含机场后缀），如 北京首都
    city: str       # 所属城市，如 北京
    pinyin: str     # 简拼，如 beijing
    kind: str       # major / inter / local


def parse():  # noqa: ANN201  保持与 stations.py 一致的懒加载风格
    """解析内置机场行。"""
    return [Airport(*row) for row in _AIRPORT_ROWS]


class _AirportTable:
    """懒加载机场索引：城市名/中文名/三字码 → Airport。"""

    def __init__(self) -> None:
        self._loaded = False
        self._by_iata: Dict[str, Airport] = {}
        self._by_city: Dict[str, List[Airport]] = {}
        self._by_name: Dict[str, Airport] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        for ap in parse():
            self._by_iata[ap.iata] = ap
            self._by_city.setdefault(ap.city, []).append(ap)
            self._by_name.setdefault(ap.name, ap)
            self._by_name.setdefault(ap.name + "机场", ap)
            self._by_name.setdefault(ap.city + ap.name, ap)
        self._loaded = True
        logger.info("Flight: 机场表已加载，%d 个机场", len(self._by_iata))

    def iata(self, code: str) -> Optional[Airport]:
        self._ensure_loaded()
        return self._by_iata.get((code or "").strip().upper())

    def city(self, city: str) -> List[Airport]:
        """城市名 → 机场列表（major 优先排序）；未收录返回 []。"""
        self._ensure_loaded()
        name = (city or "").strip()
        if not name:
            return []
        # 去「市」后缀再查；再兜底城市+机场名
        candidates = self._by_city.get(name) or self._by_city.get(
            name[:-1] if name.endswith("市") else name
        ) or []
        if candidates:
            return sorted(
                candidates,
                key=lambda a: 0 if a.kind == "major" else 1 if a.kind == "inter" else 2,
            )
        for key in (name, name + "机场", name + "国际机场"):
            ap = self._by_name.get(key)
            if ap is not None:
                return [ap]
        return []


_table = _AirportTable()


def resolve_city_airport(city: str) -> str:
    """城市名 → 首选机场三字码（major > inter > local）；无法识别抛 ValueError。

    「北京」→ PEK（major 优先，而非大兴 PKX）。
    """
    airports = _table.city(city)
    if not airports:
        raise ValueError(
            f"无法识别城市的机场: {city}（机场表未收录该城市）"
        )
    return airports[0].iata


def resolve_airport(value: str) -> str:
    """三字码 / 中文名 / 城市名 → 三字码；无法识别抛 ValueError。

    "北京" → PEK；"PEK" / "pek" → PEK；"张掖" → YZY；"张掖甘州" → YZY。
    """
    q = (value or "").strip()
    if not q:
        raise ValueError("机场不能为空")
    if len(q) == 3 and q.isalpha():
        ap = _table.iata(q)
        if ap is not None:
            return ap.iata
    city_aps = _table.city(q)
    if city_aps:
        return city_aps[0].iata
    raise ValueError(f"无法识别机场: {value}（可输入三字码、中文名或城市名）")


def airport_name(code: str) -> str:
    """三字码 → 「中文名+机场」（如 PEK → 北京首都机场）；未收录原样返回。"""
    ap = _table.iata(code)
    if ap is None:
        return code
    return f"{ap.name}机场"


def all_airports() -> List[Airport]:
    """全部机场（懒加载触发）。"""
    _table._ensure_loaded()
    return list(_table._by_iata.values())