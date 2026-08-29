"""train_trip 技能：城际火车出行查询（P2b，设计文档 §3 第一刀）。

输入城市对 + 日期 + 偏好，输出**单一推荐班次**，字段与 A 侧城际交通契约
（CityTravelEdge：transport_minutes / cost_per_person / from_station /
to_station）直接对齐——消费方零换算。

站名解析顺序（v1 城市对覆盖范围 = 根级 ``fake_spots/city_travel.json`` 估算表
收录的城市对）：
1. 输入本身已是 12306 站名/电报码 → 直接使用；
2. 估算表按城市对（train mode）查历史站名；
3. 失败 → ValueError（提示传入站名）。

选班次：可预订（status=预订）车次中，``earliest`` 取历时最短、``cheapest``
取二等座价最低（价查不到时回落 earliest）。票价经 train_price 接口换算为元。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from tools.skill import Skill
from tools.train.client import TrainClient, parse_price_row, parse_ticket_row, validate_depart_date
from tools.train.stations import resolve_station, station_name

logger = logging.getLogger("tools.train.trip")

_ESTIMATE_JSON = (
    Path(__file__).resolve().parent.parent.parent / "fake_spots" / "city_travel.json"
)

_PREFERENCES = ("earliest", "cheapest")


def _duration_minutes(train: Dict[str, Any]) -> int:
    """``"HH:MM"`` → 分钟；解析失败返回极大值（排序沉底）。"""
    try:
        hours, minutes = str(train.get("duration", "")).split(":")
        return int(hours) * 60 + int(minutes)
    except ValueError:
        return 10 ** 9


def _stations_for_city_pair(origin_city: str, dest_city: str,
                            mode: str = "train") -> Optional[Tuple[str, str]]:
    """估算表城市对 → (from_station, to_station)；未收录返回 None。"""
    try:
        data = json.loads(_ESTIMATE_JSON.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for edge in data.get("edges", []):
        if edge.get("origin") == origin_city and edge.get("destination") == dest_city:
            for opt in edge.get("options", []):
                if opt.get("mode") == mode:
                    from_station = opt.get("from_station")
                    to_station = opt.get("to_station")
                    if from_station and to_station:
                        return from_station, to_station
    return None


class TrainTripSkill(Skill):
    name = "train_trip"
    description = (
        "城际火车出行查询：出发/到达城市（或站名）+ 日期 + 偏好，返回单一推荐"
        "班次的时刻、历时与二等座票价（与城际交通契约对齐）。"
    )
    source = "mock"
    domain = "train"
    input_schema = {
        "type": "object",
        "properties": {
            "from_city": {"type": "string", "description": "出发城市或车站（如 北京 / 北京南）"},
            "to_city": {"type": "string", "description": "到达城市或车站"},
            "date": {"type": "string", "description": "出发日期 YYYY-MM-DD（今天起 14 天内）"},
            "preference": {
                "enum": ["earliest", "cheapest"],
                "description": "earliest=历时最短（默认）；cheapest=二等座最低价",
            },
        },
        "required": ["from_city", "to_city", "date"],
    }
    output_schema = {
        "type": "object",
        "properties": {
            "origin": {"type": "string"},
            "destination": {"type": "string"},
            "from_station": {"type": "string"},
            "to_station": {"type": "string"},
            "from_station_code": {"type": "string"},
            "to_station_code": {"type": "string"},
            "code": {"type": "string", "description": "车次号"},
            "train_no": {"type": "string", "description": "12306 官方编号"},
            "depart_time": {"type": "string"},
            "arrive_time": {"type": "string"},
            "duration": {"type": "string"},
            "transport_minutes": {"type": "integer", "description": "历时（分钟）"},
            "cost_per_person": {"type": "number", "description": "二等座票价（元）"},
            "seats": {"type": "object"},
            "source": {"type": "string"},
        },
        "required": ["origin", "destination", "code", "transport_minutes"],
    }

    def _run(self, from_city: str = "", to_city: str = "", date: str = "",
             preference: str = "earliest") -> Dict[str, Any]:
        # Mock：固定班次（北京南→上海虹桥），与 Live 输出同构
        if not from_city or not to_city:
            raise ValueError("from_city / to_city 不能为空")
        return {
            "origin": from_city, "destination": to_city,
            "from_station": "北京南", "to_station": "上海虹桥",
            "from_station_code": "VNP", "to_station_code": "AOH",
            "code": "G39", "train_no": "24000000G390I",
            "depart_time": "08:00", "arrive_time": "13:24", "duration": "05:24",
            "transport_minutes": 324, "cost_per_person": 662.0,
            "seats": {"second_class": "23"},
            "preference": preference, "source": "mock",
        }


class TrainTripSkillLive(TrainTripSkill):
    """12306 真源实现：组合余票查询（选班次）+ 票价查询（二等座价）。"""

    source = "live"

    def __init__(self, client: TrainClient) -> None:
        super().__init__()
        self._client = client

    def _run(self, from_city: str = "", to_city: str = "", date: str = "",
             preference: str = "earliest") -> Dict[str, Any]:
        if not from_city or not to_city:
            raise ValueError("from_city / to_city 不能为空")
        if preference not in _PREFERENCES:
            raise ValueError(f"未知 preference: {preference}（可选 {'/'.join(_PREFERENCES)}）")
        validate_depart_date(date)

        from_code, to_code, _, _ = self._resolve_stations(from_city, to_city)
        trains = self._bookable_trains(from_code, to_code, date)
        best, price = self._select(trains, from_code, to_code, date, preference)

        logger.info("train_trip: %s(%s)→%s(%s) %s [%s] → %s %s",
                    from_city, from_code, to_city, to_code, date,
                    preference, best["code"], best["duration"])
        # 12306 会把同城其他车站的车次一并返回（查北京南可能命中北京站发车
        # 的 D 字头过路车）——展示以实际班次的到发站为准，请求城市对仅作入参回显
        return {
            "origin": from_city, "destination": to_city,
            "from_station": station_name(best["from_station_code"]),
            "to_station": station_name(best["to_station_code"]),
            "from_station_code": best["from_station_code"],
            "to_station_code": best["to_station_code"],
            "code": best["code"], "train_no": best["train_no"],
            "depart_time": best["depart_time"], "arrive_time": best["arrive_time"],
            "duration": best["duration"],
            "transport_minutes": _duration_minutes(best),
            "cost_per_person": price if price is not None else 0.0,
            "seats": best["seats"],
            "preference": preference,
            "source": "live",
        }

    # -- 内部 --------------------------------------------------------------

    @staticmethod
    def _resolve_stations(from_city: str, to_city: str) -> Tuple[str, str, str, str]:
        """城市/站名 → (出发电报码, 到达电报码, 出发站名, 到达站名)。

        **城市对表优先**："北京"既是城市（估算表收录）也是站名（北京站）——
        若按站名直查只会返回北京站的车次、漏掉同城其他车站（北京南等），
        故先查城市对表，未收录再按站名直查。
        """
        pair = _stations_for_city_pair(from_city, to_city)
        if pair:
            from_code, to_code = resolve_station(pair[0]), resolve_station(pair[1])
            return (from_code, to_code,
                    station_name(from_code), station_name(to_code))
        try:
            return (resolve_station(from_city), resolve_station(to_city),
                    from_city, to_city)
        except ValueError as exc:
            raise ValueError(
                f"无法确定 {from_city}→{to_city} 的车站：请传入 12306 站名，"
                "或使用估算表已收录的城市对（fake_spots/city_travel.json）"
            ) from exc

    def _bookable_trains(self, from_code: str, to_code: str, date: str) -> List[Dict[str, Any]]:
        rows = self._client.query_tickets(from_code, to_code, date)
        trains: List[Dict[str, Any]] = []
        for row in rows:
            parsed = parse_ticket_row(row)
            if parsed and parsed.get("status") == "预订" and parsed.get("train_no"):
                trains.append(parsed)
        if not trains:
            raise ValueError(f"未查到可预订车次（{from_code}→{to_code} {date}）")
        return trains

    def _second_class_prices(self, from_code: str, to_code: str, date: str) -> Dict[str, float]:
        prices: Dict[str, float] = {}
        for dto in self._client.query_price(from_code, to_code, date):
            row = parse_price_row(dto)
            second = row["prices"].get("second_class")
            if second is not None:
                prices[row["code"]] = float(second)
        return prices

    def _select(self, trains: List[Dict[str, Any]], from_code: str, to_code: str,
                date: str, preference: str) -> Tuple[Dict[str, Any], Optional[float]]:
        """按偏好选班次并取二等座价（cheapest 按价选，earliest 按历时选）。"""
        price_map = self._second_class_prices(from_code, to_code, date)
        if preference == "cheapest":
            priced = [t for t in trains if t["code"] in price_map]
            if priced:
                best = min(priced, key=lambda t: price_map[t["code"]])
                return best, price_map[best["code"]]
            logger.info("train_trip: 票价接口无可用价格，回落 earliest")
        best = min(trains, key=_duration_minutes)
        return best, price_map.get(best["code"])
