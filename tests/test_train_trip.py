"""train_trip 技能测试：站名解析、班次选择、A 侧 provider 适配（P2b）。"""

import os
import sys
import unittest
from unittest.mock import MagicMock

from tools.train.trip import (
    TrainTripSkill,
    TrainTripSkillLive,
    _stations_for_city_pair,
)

_B_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
for _p in (os.path.join(_B_ROOT, "django_server"), os.path.join(_B_ROOT, "a_side"), _B_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def make_ticket_row(code="G39", duration="05:24", depart="08:00", arrive="13:24"):
    """最小可预订余票行（40 列，与 test_train_tool.make_ticket_row 同构）。"""
    parts = [""] * 40
    parts[1] = "预订"
    parts[2] = f"24000000{code}0I"
    parts[3] = code
    parts[6] = "VNP"
    parts[7] = "AOH"
    parts[8] = depart
    parts[9] = arrive
    parts[10] = duration
    parts[30] = "23"
    return "|".join(parts)


def make_price_dto(code, second):
    return {"station_train_code": code, "ze_price": str(int(second * 10))}


class TestTrainTripMock(unittest.TestCase):
    def test_mock_output_contract(self) -> None:
        r = TrainTripSkill().execute(from_city="北京", to_city="上海",
                                     date="2026-09-05")
        self.assertEqual(r.status.value, "ok")
        data = r.data
        self.assertIsInstance(data["transport_minutes"], int)
        self.assertIsInstance(data["cost_per_person"], float)
        self.assertEqual(data["code"], "G39")
        self.assertEqual(data["source"], "mock")

    def test_mock_missing_city_errors(self) -> None:
        r = TrainTripSkill().execute(to_city="上海", date="2026-09-05")
        self.assertEqual(r.status.value, "error")


class TestTrainTripLive(unittest.TestCase):
    def _client(self, rows=None, prices=None):
        client = MagicMock()
        client.query_tickets.return_value = rows or [
            make_ticket_row(code="G39", duration="05:24"),
            make_ticket_row(code="G1", duration="04:54"),
        ]
        client.query_price.return_value = prices or [
            make_price_dto("G39", 662.0), make_price_dto("G1", 795.0),
        ]
        return client

    def test_earliest_picks_shortest_duration(self) -> None:
        skill = TrainTripSkillLive(self._client())
        r = skill.execute(from_city="北京南", to_city="上海虹桥", date="2026-09-05")
        self.assertEqual(r.status.value, "ok")
        data = r.data
        self.assertEqual(data["code"], "G1")            # 04:54 < 05:24
        self.assertEqual(data["transport_minutes"], 294)
        self.assertEqual(data["cost_per_person"], 795.0)
        self.assertEqual(data["from_station"], "北京南")  # 站名直查原样透出
        self.assertEqual(data["source"], "live")

    def test_cheapest_picks_lowest_second_class(self) -> None:
        skill = TrainTripSkillLive(self._client())
        r = skill.execute(from_city="北京南", to_city="上海虹桥",
                          date="2026-09-05", preference="cheapest")
        self.assertEqual(r.data["code"], "G39")         # 662 < 795
        self.assertEqual(r.data["cost_per_person"], 662.0)

    def test_city_pair_via_estimate_table(self) -> None:
        # v1 城市对覆盖优先于站名直查："北京"按城市解析为北京南（而非北京站），
        # 避免漏掉同城其他车站
        skill = TrainTripSkillLive(self._client())
        r = skill.execute(from_city="北京", to_city="上海", date="2026-09-05")
        self.assertEqual(r.status.value, "ok")
        self.assertEqual(r.data["from_station"], "北京南")
        self.assertEqual(r.data["to_station"], "上海虹桥")
        self.assertEqual(r.data["from_station_code"], "VNP")

    def test_direct_station_names_still_work(self) -> None:
        skill = TrainTripSkillLive(self._client())
        r = skill.execute(from_city="北京南", to_city="上海虹桥", date="2026-09-05")
        self.assertEqual(r.status.value, "ok")
        self.assertEqual(r.data["from_station"], "北京南")

    def test_unknown_pair_raises(self) -> None:
        skill = TrainTripSkillLive(self._client())
        r = skill.execute(from_city="火星", to_city="月球", date="2026-09-05")
        self.assertEqual(r.status.value, "error")
        self.assertIn("无法确定", r.error)

    def test_same_city_station_substitution(self) -> None:
        """12306 会返回同城其他车站的车次：展示以实际班次到发站为准。

        查北京南→上海虹桥，命中一趟实际从北京站（BJP）出发的 D 字头车——
        输出应是 北京（BJP）而非请求的 北京南（VNP），且仍被选为最快。
        """
        rows = [
            make_ticket_row(code="G39", duration="05:24"),   # VNP 出发
            make_ticket_row(code="D7", duration="04:00"),    # 北京站出发（更快）
        ]
        rows[1] = rows[1].replace("VNP", "BJP")
        skill = TrainTripSkillLive(self._client(rows=rows))
        r = skill.execute(from_city="北京南", to_city="上海虹桥", date="2026-09-05")
        data = r.data
        self.assertEqual(data["code"], "D7")
        self.assertEqual(data["from_station_code"], "BJP")
        self.assertEqual(data["from_station"], "北京")
        self.assertEqual(data["to_station_code"], "AOH")

    def test_no_bookable_trains_raises(self) -> None:
        stopped = make_ticket_row(code="G39").replace("|预订|", "|停运|")
        skill = TrainTripSkillLive(self._client(rows=[stopped]))
        r = skill.execute(from_city="北京南", to_city="上海虹桥", date="2026-09-05")
        self.assertEqual(r.status.value, "error")
        self.assertIn("未查到可预订车次", r.error)


class TestStationPairLookup(unittest.TestCase):
    def test_estimate_table_pair(self) -> None:
        pair = _stations_for_city_pair("北京", "上海")
        self.assertIsNotNone(pair)
        from tools.train.stations import resolve_station
        self.assertEqual(resolve_station(pair[0]), "VNP")
        self.assertEqual(resolve_station(pair[1]), "AOH")

    def test_unknown_pair_returns_none(self) -> None:
        self.assertIsNone(_stations_for_city_pair("火星", "月球"))


class TestLiveTrainTripProvider(unittest.TestCase):
    def _provider(self, tool_result):
        from data_transmission.live_data import make_live_train_trip_provider
        tp = MagicMock()
        tp.call.return_value = tool_result
        return make_live_train_trip_provider(tp, date="2026-09-05"), tp

    def _tool_result(self, payload):
        from core.schemas import ToolResult, ToolStatus
        return ToolResult(tool="train_trip", status=ToolStatus.OK, data=payload,
                          source="live")

    def test_provider_builds_city_travel_edge(self) -> None:
        from data_transmission.city_travel import CityTravelEdge
        provider, tp = self._provider(self._tool_result({
            "transport_minutes": 294, "cost_per_person": 795.0,
            "from_station": "北京南", "to_station": "上海虹桥", "source": "live",
        }))
        edge = provider("北京", "上海", mode="train")
        self.assertIsInstance(edge, CityTravelEdge)
        self.assertEqual(edge.transport_minutes, 294)
        self.assertEqual(edge.cost_per_person, 795.0)
        self.assertEqual(edge.source, "live")
        tp.call.assert_called_once_with("train_trip", from_city="北京",
                                        to_city="上海", date="2026-09-05")

    def test_provider_air_mode_returns_none(self) -> None:
        provider, _ = self._provider(self._tool_result({"transport_minutes": 1}))
        self.assertIsNone(provider("北京", "上海", mode="air"))

    def test_provider_tool_failure_returns_none(self) -> None:
        from core.schemas import ToolResult, ToolStatus
        provider, _ = self._provider(ToolResult(tool="train_trip",
                                                status=ToolStatus.ERROR,
                                                data=None, error="x"))
        self.assertIsNone(provider("北京", "上海"))


if __name__ == "__main__":
    unittest.main()
