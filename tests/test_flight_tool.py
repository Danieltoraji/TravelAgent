"""航班 Tool 组测试：机场表解析、日期校验、juhe 响应解析（真实采样）、客户端 URL、Mock/Live 工具。

响应解析全部用 2026-08-29 探针实测到的 juhe 真实返回结构（不调真接口、不耗额度）：
- 北京→上海：``result.flightInfo[]`` 含 airline/airlineName/flightNo/isCodeShare/
  equipment/departure/departureName/departureDate/departureTime/arrivalDate/
  arrivalTime/arrival/arrivalName/duration("1h45m")/transferNum/ticketPrice/segments
- 错误码：``error_code`` 非 0（如 281801 格式错 / 10012 次数不足）
"""

import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from core.schemas import ToolStatus
from tools.flight.airports import (
    all_airports,
    airport_name,
    resolve_airport,
    resolve_city_airport,
)
from tools.flight.client import (
    FlightClient,
    _duration_to_min,
    parse_juhe_row,
    validate_flight_date,
)
from tools.flight.tools import FlightSearchTool, FlightSearchToolLive


# ---------------------------------------------------------------------------
# 真实采样（2026-08-29 探针实测，北京→上海 / 北京→张掖 / 空结果 / 错误码）
# ---------------------------------------------------------------------------

_SAMPLE_BJSHA = {
    "reason": "成功",
    "result": {
        "orderid": "JH8182608291315056S5wX",
        "flightInfo": [
            {"airline": "CA", "airlineName": "中国国际航空公司", "flightNo": "CA8341",
             "isCodeShare": False, "equipment": "320",
             "departure": "PKX", "departureName": "大兴国际机场",
             "departureDate": "2026-09-05", "departureTime": "22:00",
             "arrivalDate": "2026-09-05", "arrivalTime": "23:45",
             "arrival": "PVG", "arrivalName": "浦东国际机场",
             "duration": "1h45m", "transferNum": 1, "ticketPrice": 468, "segments": []},
            {"airline": "MU", "airlineName": "中国东方航空公司", "flightNo": "MU5231",
             "isCodeShare": False, "equipment": "325",
             "departure": "PKX", "departureName": "大兴国际机场",
             "departureDate": "2026-09-05", "departureTime": "22:00",
             "arrivalDate": "2026-09-06", "arrivalTime": "00:05",
             "arrival": "PVG", "arrivalName": "浦东国际机场",
             "duration": "2h5m", "transferNum": 1, "ticketPrice": 470, "segments": []},
        ],
    },
    "error_code": 0,
}

_SAMPLE_BJYZY = {
    "reason": "成功",
    "result": {
        "orderid": "JH8182608291315193NR7L",
        "flightInfo": [
            {"airline": "KN", "airlineName": "中国联合航空公司", "flightNo": "KN5601",
             "isCodeShare": False, "equipment": "73V",
             "departure": "PKX", "departureName": "大兴国际机场",
             "departureDate": "2026-09-05", "departureTime": "16:50",
             "arrivalDate": "2026-09-05", "arrivalTime": "19:35",
             "arrival": "YZY", "arrivalName": "甘州机场",
             "duration": "2h45m", "transferNum": 1, "ticketPrice": 654, "segments": []},
            {"airline": "MU", "airlineName": "中国东方航空公司", "flightNo": "MU8143(KN5601)",
             "isCodeShare": True, "equipment": "73V",
             "departure": "PKX", "departureName": "大兴国际机场",
             "departureDate": "2026-09-05", "departureTime": "16:50",
             "arrivalDate": "2026-09-05", "arrivalTime": "19:35",
             "arrival": "YZY", "arrivalName": "甘州机场",
             "duration": "2h45m", "transferNum": 1, "ticketPrice": 861, "segments": []},
        ],
    },
    "error_code": 0,
}

_SAMPLE_EMPTY = {
    "reason": "成功",
    "result": {"orderid": "JH...", "flightInfo": []},
    "error_code": 0,
}

_SAMPLE_ERROR = {
    "resultcode": "112", "reason": "当前可请求的次数不足",
    "result": None, "error_code": 10012,
}


# ---------------------------------------------------------------------------
# 机场表
# ---------------------------------------------------------------------------


class TestAirports(unittest.TestCase):
    def test_resolve_city_major_first(self) -> None:
        self.assertEqual(resolve_city_airport("北京"), "PEK")   # major 优先于 PKX
        self.assertEqual(resolve_city_airport("上海"), "SHA")
        self.assertEqual(resolve_city_airport("天津"), "TSN")

    def test_resolve_city_strip_suffix(self) -> None:
        self.assertEqual(resolve_city_airport("张掖市"), "YZY")
        self.assertEqual(resolve_city_airport("常州市"), "CZX")

    def test_resolve_city_local(self) -> None:
        self.assertEqual(resolve_city_airport("张掖"), "YZY")
        self.assertEqual(resolve_city_airport("常州"), "CZX")
        self.assertEqual(resolve_city_airport("锦州"), "JNZ")

    def test_resolve_iata_passthrough(self) -> None:
        self.assertEqual(resolve_airport("PEK"), "PEK")
        self.assertEqual(resolve_airport("pek"), "PEK")
        self.assertEqual(resolve_airport("YZY"), "YZY")

    def test_resolve_city_name(self) -> None:
        self.assertEqual(resolve_airport("北京"), "PEK")
        self.assertEqual(resolve_airport("张掖"), "YZY")

    def test_resolve_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_airport("火星")

    def test_airport_name(self) -> None:
        self.assertEqual(airport_name("YZY"), "张掖甘州机场")
        self.assertEqual(airport_name("ZZZ"), "ZZZ")  # 未收录原样

    def test_all_airports_loaded(self) -> None:
        self.assertGreaterEqual(len(all_airports()), 40)
        iatas = {a.iata for a in all_airports()}
        self.assertTrue({"PEK", "YZY", "CZX", "JNZ"} <= iatas)


# ---------------------------------------------------------------------------
# 日期校验
# ---------------------------------------------------------------------------


class TestValidateFlightDate(unittest.TestCase):
    def _date(self, offset_days: int) -> str:
        return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")

    def test_today_ok(self) -> None:
        validate_flight_date(self._date(0))

    def test_within_60d_ok(self) -> None:
        validate_flight_date(self._date(60))

    def test_bad_format_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_flight_date("2026/09/01")

    def test_past_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_flight_date(self._date(-1))

    def test_beyond_60d_raises(self) -> None:
        with self.assertRaises(ValueError):
            validate_flight_date(self._date(61))


# ---------------------------------------------------------------------------
# juhe 响应解析（真实采样）
# ---------------------------------------------------------------------------


class TestParseJuheRow(unittest.TestCase):
    def test_full_row(self) -> None:
        row = _SAMPLE_BJSHA["result"]["flightInfo"][0]
        parsed = parse_juhe_row(row)
        self.assertEqual(parsed["flight_no"], "CA8341")
        self.assertEqual(parsed["airline"], "中国国际航空公司")
        self.assertEqual(parsed["from_airport"], "PKX")
        self.assertEqual(parsed["to_airport"], "PVG")
        self.assertEqual(parsed["from_airport_name"], "大兴国际机场")
        self.assertEqual(parsed["depart_time"], "22:00")
        self.assertEqual(parsed["arrive_time"], "23:45")
        self.assertEqual(parsed["duration_min"], 105)      # 1h45m
        self.assertEqual(parsed["price"], 468.0)
        self.assertEqual(parsed["date"], "2026-09-05")
        self.assertEqual(parsed["transfer_num"], 1)

    def test_codeshare_row(self) -> None:
        row = _SAMPLE_BJYZY["result"]["flightInfo"][1]
        parsed = parse_juhe_row(row)
        self.assertEqual(parsed["flight_no"], "MU8143(KN5601)")
        self.assertEqual(parsed["price"], 861.0)

    def test_missing_flight_no_returns_none(self) -> None:
        self.assertIsNone(parse_juhe_row({"airline": "CA"}))

    def test_duration_to_min(self) -> None:
        self.assertEqual(_duration_to_min("1h45m"), 105)
        self.assertEqual(_duration_to_min("2h5m"), 125)
        self.assertEqual(_duration_to_min(""), 0)
        self.assertEqual(_duration_to_min("abc"), 0)


# ---------------------------------------------------------------------------
# FlightClient（patch urlopen，用真实采样 JSON）
# ---------------------------------------------------------------------------


def _fake_http_response(payload: dict, encoding: str = "utf-8"):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload, ensure_ascii=False).encode(encoding)
    resp.headers = {"Content-Encoding": ""}
    resp.__enter__.return_value = resp
    return resp


class TestFlightClient(unittest.TestCase):
    @patch("urllib.request.urlopen")
    def test_juhe_query_url_and_parse(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _fake_http_response(_SAMPLE_BJSHA)
        client = FlightClient(backend="juhe", api_key="testkey", timeout=10)
        rows = client.query_flights("北京", "上海", "2026-09-05")

        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["flight_no"], "CA8341")
        self.assertEqual(rows[0]["price"], 468.0)
        req = mock_urlopen.call_args.args[0]
        url = req.get_full_url()
        self.assertIn("https://apis.juhe.cn/flight/query", url)
        self.assertIn("key=testkey", url)
        self.assertIn("departure=PEK", url)
        self.assertIn("arrival=SHA", url)
        self.assertIn("departureDate=2026-09-05", url)

    @patch("urllib.request.urlopen")
    def test_juhe_empty_list(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _fake_http_response(_SAMPLE_EMPTY)
        client = FlightClient(backend="juhe", api_key="testkey", timeout=10)
        self.assertEqual(client.query_flights("锦州", "常州", "2026-09-05"), [])

    @patch("urllib.request.urlopen")
    def test_juhe_error_code_raises(self, mock_urlopen) -> None:
        mock_urlopen.return_value = _fake_http_response(_SAMPLE_ERROR)
        client = FlightClient(backend="juhe", api_key="testkey", timeout=10)
        with self.assertRaises(ValueError):
            client.query_flights("锦州", "常州", "2026-09-05")

    def test_no_key_raises(self) -> None:
        client = FlightClient(backend="aviationstack", api_key="", timeout=10)
        with self.assertRaises(ValueError):
            client.query_flights("北京", "上海", "2026-09-05")

    def test_unknown_backend_raises(self) -> None:
        with self.assertRaises(ValueError):
            FlightClient(backend="nope", api_key="k", timeout=10)


# ---------------------------------------------------------------------------
# 工具层：Mock 版与 Live 版
# ---------------------------------------------------------------------------


def _future_date(offset: int = 3) -> str:
    return (date.today() + timedelta(days=offset)).strftime("%Y-%m-%d")


class TestFlightToolsMock(unittest.TestCase):
    def test_mock_search_structure(self) -> None:
        r = FlightSearchTool().execute(from_city="北京", to_city="上海", date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("flight_no", r.data[0])
        self.assertIn("price", r.data[0])
        self.assertIn("from_airport_name", r.data[0])

    def test_mock_limit(self) -> None:
        r = FlightSearchTool().execute(from_city="北京", to_city="上海",
                                       date=_future_date(), limit=1)
        self.assertEqual(len(r.data), 1)


class TestFlightToolsLive(unittest.TestCase):
    def _client(self) -> MagicMock:
        return MagicMock()

    def test_live_parses_client_rows(self) -> None:
        client = self._client()
        client.query_flights.return_value = [
            {"flight_no": "CA8341", "airline": "中国国际航空公司",
             "from_airport": "PKX", "to_airport": "PVG",
             "depart_time": "22:00", "arrive_time": "23:45",
             "duration_min": 105, "price": 468.0, "date": "2026-09-05"},
        ]
        r = FlightSearchToolLive(client).execute(
            from_city="北京", to_city="上海", date="2026-09-05")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data[0]["from_airport_name"], "北京大兴机场")
        client.query_flights.assert_called_once_with("北京", "上海", "2026-09-05")

    def test_live_empty_is_ok(self) -> None:
        client = self._client()
        client.query_flights.return_value = []
        r = FlightSearchToolLive(client).execute(
            from_city="锦州", to_city="常州", date="2026-09-05")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data, [])

    def test_live_missing_city_errors(self) -> None:
        # C5（PR#3）：base_tool 按 schema required 校验，空串在 execute 层
        # 即报「必填参数为空: from_city」，不会走到 _run 内单独的空值分支。
        r = FlightSearchToolLive(self._client()).execute(
            from_city="", to_city="上海", date="2026-09-05")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("必填参数为空", r.error)
        self.assertIn("from_city", r.error)

    def test_live_bad_date_errors(self) -> None:
        r = FlightSearchToolLive(self._client()).execute(
            from_city="北京", to_city="上海", date="2026-13-01")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("日期无效", r.error)


if __name__ == "__main__":
    unittest.main()