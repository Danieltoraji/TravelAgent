"""12306 车站表：内置 station_name.js 的解析 + 站名/拼音/电报码互转。

数据文件 tools/train/data/station_name.js 取自 12306 官方
（https://kyfw.12306.cn/otn/resources/js/framework/station_name.js），
随包内置避免运行时拉取；首次使用时懒加载进内存 dict。

每条记录格式：@简拼|站名|三字码|全拼|简拼|序号|区域码|城市|...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict

logger = logging.getLogger("tools.train.stations")

_DATA_PATH = Path(__file__).parent / "data" / "station_name.js"


@dataclass(frozen=True)
class Station:
    name: str        # 站名，如 北京南
    code: str        # 三字码（电报码），如 VNP
    pinyin: str      # 全拼，如 beijingnan
    py_short: str    # 简拼，如 bjn
    city: str        # 所属城市，如 北京


def parse_station_js(content: str) -> list[Station]:
    """解析 station_name.js 文本为车站列表。"""
    m = re.search(r"var station_names ?= ?'(.*?)';", content)
    if not m:
        m = re.search(r"'(@[^']+)';", content)
    if not m:
        raise ValueError("station_name.js 解析失败：未找到站点数据")
    stations: list[Station] = []
    for entry in m.group(1).split("@"):
        if not entry:
            continue
        parts = entry.split("|")
        if len(parts) < 8:
            logger.warning("station_name.js 字段数异常，跳过: %s", entry)
            continue
        stations.append(Station(
            name=parts[1].strip(),
            code=parts[2].strip(),
            pinyin=parts[3].strip(),
            py_short=parts[4].strip(),
            city=parts[7].strip(),
        ))
    return stations


class _StationTable:
    """懒加载车站索引：站名/全拼/城市 → 电报码，电报码 → 站名。"""

    def __init__(self) -> None:
        self._loaded = False
        self._code_by_name: Dict[str, str] = {}
        self._code_by_pinyin: Dict[str, str] = {}
        self._name_by_code: Dict[str, str] = {}
        # 城市 → 主站电报码（8.31 修复新增：LLM 备注解析常产出
        # 「广西贵港」这类带省级前缀的 origin，站表里只有站名「贵港」；
        # city 字段来自 12306 官方 station_name.js，长期闲置，这里用起来）
        self._code_by_city: Dict[str, str] = {}

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        stations = parse_station_js(_DATA_PATH.read_text(encoding="utf-8"))
        for st in stations:
            self._code_by_name.setdefault(st.name, st.code)
            self._code_by_pinyin.setdefault(st.pinyin, st.code)
            self._name_by_code[st.code] = st.name
            self._code_by_city.setdefault(st.city, st.code)
        # 同名主站优先：城市下「站名==城市名」的站（贵港站 GGZ / 上海站
        # SHH）覆盖先出现的非同名站（贵港市的桂平 GAZ / 上海市的练塘
        # LTU）——保证「按城市名查询=主站」的语义。
        for st in stations:
            if st.name == st.city:
                self._code_by_city[st.city] = st.code
        self._loaded = True
        logger.info("Train: 车站表已加载，%d 个车站，%d 个城市",
                    len(self._name_by_code), len(self._code_by_city))


_table = _StationTable()


def resolve_station(value: str) -> str:
    """站名 / 全拼 / 电报码 / 城市名 → 12306 电报码；无法识别抛 ValueError。

    匹配顺序：
    1. 三字码（电报码，大小写均可）；
    2. 中文站名（"北京南站" 这类带"站"后缀的写法先去后缀）；
    3. 全拼；
    4. **城市名后缀匹配（8.31）**：剥掉尾部"市"后，若输入以某个
       ``Station.city`` 结尾（裸城市名「贵港」/ 带省级前缀「广西贵港」/
       带"市"「贵港市」「广西贵港市」），取该城市主站电报码——
       LLM 备注解析出的 origin 常带省级前缀，而站表只有城市名本身。
    """
    q = (value or "").strip()
    if not q:
        raise ValueError("车站不能为空")
    _table._ensure_loaded()
    if len(q) == 3 and q.isalpha():
        upper = q.upper()
        if upper in _table._name_by_code:
            return upper
    if q.endswith("站") and len(q) > 2:
        q = q[:-1]
    code = _table._code_by_name.get(q)
    if code:
        return code
    code = _table._code_by_pinyin.get(q.lower())
    if code:
        return code
    # 城市级兜底：剥"市"，再查 q 是否以某城市名结尾（省前缀+城市名）。
    q_city = q[:-1] if q.endswith("市") and len(q) > 2 else q
    for city, city_code in _table._code_by_city.items():
        if q_city.endswith(city):
            return city_code
    raise ValueError(f"无法识别车站: {value}（可输入中文站名、全拼、三字码或城市名）")


def station_name(code: str) -> str:
    """电报码 → 中文站名；未收录时原样返回电报码。"""
    _table._ensure_loaded()
    return _table._name_by_code.get(code, code)