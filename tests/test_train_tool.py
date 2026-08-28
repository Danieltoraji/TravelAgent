"""火车票 Tool 组测试：站表解析、日期校验、12306 响应解析、客户端会话、Mock/Live 工具。"""

import gzip
import json
import unittest
from datetime import date, timedelta
from unittest.mock import MagicMock, patch

from core.schemas import ToolStatus
from tools.train.client import (
    TrainClient,
    parse_price_row,
    parse_ticket_row,
    parse_transfer_item,
)
from tools.train.stations import resolve_station, station_name
from tools.train.tools import (
    TrainPriceTool,
    TrainPriceToolLive,
    TrainRouteTool,
    TrainRouteToolLive,
    TrainTicketTool,
    TrainTicketToolLive,
    TrainTransferTool,
    TrainTransferToolLive,
)


# ---------------------------------------------------------------------------
# 测试数据构造
# ---------------------------------------------------------------------------


def make_ticket_row(code="G39", official="24000000G390I", status="预订",
                    from_code="VNP", to_code="AOH", depart="08:00",
                    arrive="13:24", duration="05:24", seats=None):
    """构造一行 40 列的余票记录（12306 真实行超过 35 列）。"""
    parts = [""] * 40
    parts[1] = status
    parts[2] = official
    parts[3] = code
    parts[6] = from_code
    parts[7] = to_code
    parts[8] = depart
    parts[9] = arrive
    parts[10] = duration
    for idx, val in (seats or {
        "21": "2", "23": "有", "24": "--", "26": "无", "28": "12",
        "29": "20", "30": "8", "31": "有", "32": "3", "33": "候补",
    }).items():
        parts[int(idx)] = val
    return "|".join(parts)


def make_transfer_item():
    return {
        "middle_station_name": "南京南",
        "wait_time": "28分",
        "all_lishi": "06:10",
        "fullList": [
            {"station_train_code": "G111", "from_station_name": "北京南",
             "to_station_name": "南京南", "start_time": "08:00",
             "arrive_time": "11:15", "lishi": "03:15",
             "swz_num": "1", "ze_num": "有", "yz_num": "--"},
            {"station_train_code": "G7357", "from_station_name": "南京南",
             "to_station_name": "上海虹桥", "start_time": "11:43",
             "arrive_time": "12:43", "lishi": "01:00",
             "ze_num": "15"},
        ],
    }


class _FakeHeaders:
    def __init__(self, headers=None, set_cookies=None):
        self._headers = headers or {}
        self._set_cookies = set_cookies or []

    def get(self, name, default=None):
        return self._headers.get(name, default)

    def get_all(self, name, default=None):
        if name == "Set-Cookie":
            return self._set_cookies or default
        return default


def fake_response(status=200, url="https://kyfw.12306.cn/otn/leftTicket/init",
                  body=b"ok", headers=None, set_cookies=None):
    resp = MagicMock()
    resp.status = status
    resp.url = url
    resp.read.return_value = body
    resp.headers = _FakeHeaders(headers, set_cookies)
    resp.__enter__.return_value = resp
    return resp


# ---------------------------------------------------------------------------
# 车站表
# ---------------------------------------------------------------------------


class TestStations(unittest.TestCase):
    def test_resolve_by_name(self) -> None:
        self.assertEqual(resolve_station("北京南"), "VNP")

    def test_resolve_strips_station_suffix(self) -> None:
        self.assertEqual(resolve_station("北京南站"), "VNP")

    def test_resolve_by_telecode(self) -> None:
        self.assertEqual(resolve_station("VNP"), "VNP")
        self.assertEqual(resolve_station("vnp"), "VNP")

    def test_resolve_by_pinyin(self) -> None:
        self.assertEqual(resolve_station("beijingnan"), "VNP")

    def test_resolve_empty_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_station("")

    def test_resolve_unknown_raises(self) -> None:
        with self.assertRaises(ValueError):
            resolve_station("不存在的车站XYZ")

    def test_station_name(self) -> None:
        self.assertEqual(station_name("VNP"), "北京南")
        self.assertEqual(station_name("ZZZ"), "ZZZ")  # 未收录原样返回


# ---------------------------------------------------------------------------
# 日期校验
# ---------------------------------------------------------------------------


class TestValidateDepartDate(unittest.TestCase):
    def _date(self, offset_days: int) -> str:
        return (date.today() + timedelta(days=offset_days)).strftime("%Y-%m-%d")

    def test_today_ok(self) -> None:
        from tools.train.client import validate_depart_date
        validate_depart_date(self._date(0))

    def test_within_presale_ok(self) -> None:
        from tools.train.client import validate_depart_date
        validate_depart_date(self._date(14))

    def test_bad_format_raises(self) -> None:
        from tools.train.client import validate_depart_date
        with self.assertRaises(ValueError):
            validate_depart_date("2026/09/01")

    def test_past_raises(self) -> None:
        from tools.train.client import validate_depart_date
        with self.assertRaises(ValueError):
            validate_depart_date(self._date(-1))

    def test_beyond_presale_raises(self) -> None:
        from tools.train.client import validate_depart_date
        with self.assertRaises(ValueError):
            validate_depart_date(self._date(15))


# ---------------------------------------------------------------------------
# 响应解析
# ---------------------------------------------------------------------------


class TestParseTicketRow(unittest.TestCase):
    def test_full_row(self) -> None:
        parsed = parse_ticket_row(make_ticket_row())
        self.assertEqual(parsed["code"], "G39")
        self.assertEqual(parsed["train_no"], "24000000G390I")
        self.assertEqual(parsed["status"], "预订")
        self.assertEqual(parsed["depart_time"], "08:00")
        self.assertEqual(parsed["duration"], "05:24")
        self.assertEqual(parsed["seats"]["business"], "3")
        self.assertEqual(parsed["seats"]["soft_sleeper"], "有")
        self.assertEqual(parsed["seats"]["dongwo"], "候补")

    def test_na_seat_filtered(self) -> None:
        parsed = parse_ticket_row(make_ticket_row())
        self.assertNotIn("soft_seat", parsed["seats"])  # "--" 过滤

    def test_stopped_train_keeps_no_official_no(self) -> None:
        parsed = parse_ticket_row(make_ticket_row(status="停运", official=""))
        self.assertEqual(parsed["status"], "停运")
        self.assertEqual(parsed["train_no"], "")
        self.assertEqual(parsed["code"], "G39")

    def test_short_row_returns_none(self) -> None:
        self.assertIsNone(parse_ticket_row("a|b|c"))


class TestParseTransferItem(unittest.TestCase):
    def test_two_leg_plan(self) -> None:
        parsed = parse_transfer_item(make_transfer_item())
        self.assertEqual(parsed["middle_station"], "南京南")
        self.assertEqual(parsed["wait_time"], "28分")
        self.assertEqual(parsed["total_duration"], "06:10")
        self.assertEqual(len(parsed["segments"]), 2)
        first = parsed["segments"][0]
        self.assertEqual(first["code"], "G111")
        self.assertEqual(first["seats"], {"business": "1", "second_class": "有"})

    def test_single_leg_returns_none(self) -> None:
        item = make_transfer_item()
        item["fullList"] = item["fullList"][:1]
        self.assertIsNone(parse_transfer_item(item))


class TestParsePriceRow(unittest.TestCase):
    def test_price_converted_to_yuan(self) -> None:
        dto = {"station_train_code": "G39", "train_no": "24000000G390I",
               "from_station_name": "北京南", "to_station_name": "上海虹桥",
               "start_time": "08:00", "arrive_time": "13:24", "lishi": "05:24",
               "ze_price": "6620", "swz_price": "21200", "yz_price": "--"}
        parsed = parse_price_row(dto)
        self.assertEqual(parsed["code"], "G39")
        self.assertEqual(parsed["prices"], {"second_class": 662.0,
                                            "business": 2120.0})


# ---------------------------------------------------------------------------
# TrainClient（patch 模块级 urlopen）
# ---------------------------------------------------------------------------


class TestTrainClient(unittest.TestCase):
    @patch("tools.train.client.urlopen")
    def test_query_inits_session_then_sends_cookie(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=[
            "JSESSIONID=abc; Path=/; HttpOnly", "route=1; Path=/"])
        query_resp = fake_response(
            url="https://kyfw.12306.cn/otn/leftTicket/queryI",
            body=json.dumps({"data": {"result": [make_ticket_row()]}}).encode("utf-8"))
        mock_urlopen.side_effect = [init_resp, query_resp]

        client = TrainClient(timeout=5)
        rows = client.query_tickets("VNP", "AOH", "2026-09-01")

        self.assertEqual(mock_urlopen.call_count, 2)
        init_req = mock_urlopen.call_args_list[0][0][0]
        query_req = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("otn/leftTicket/init", init_req.get_full_url())
        self.assertIn("otn/leftTicket/queryI", query_req.get_full_url())
        self.assertIn("leftTicketDTO.train_date=2026-09-01", query_req.get_full_url())
        self.assertIn("leftTicketDTO.from_station=VNP", query_req.get_full_url())
        self.assertIn("leftTicketDTO.to_station=AOH", query_req.get_full_url())
        self.assertIn("purpose_codes=ADULT", query_req.get_full_url())
        cookie = query_req.headers.get("Cookie", "")
        self.assertIn("JSESSIONID=abc", cookie)
        self.assertIn("route=1", cookie)
        self.assertEqual(len(rows), 1)
        self.assertEqual(parse_ticket_row(rows[0])["code"], "G39")

    @patch("tools.train.client.urlopen")
    def test_session_reused_across_queries(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        empty = fake_response(url="https://kyfw.12306.cn/otn/leftTicket/queryI",
                              body=b'{"data": {"result": []}}')
        mock_urlopen.side_effect = [init_resp, empty, empty]

        client = TrainClient(timeout=5)
        client.query_tickets("VNP", "AOH", "2026-09-01")
        client.query_tickets("VNP", "AOH", "2026-09-01")

        # 第二次查询不再重复访问 init（1 init + 2 query）
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("tools.train.client.urlopen")
    def test_gzip_response_decoded(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        payload = gzip.compress(json.dumps({"data": {"result": []}}).encode("utf-8"))
        query_resp = fake_response(
            url="https://kyfw.12306.cn/otn/leftTicket/queryI",
            body=payload, headers={"Content-Encoding": "gzip"})
        mock_urlopen.side_effect = [init_resp, query_resp]

        client = TrainClient(timeout=5)
        self.assertEqual(client.query_tickets("VNP", "AOH", "2026-09-01"), [])

    @patch("tools.train.client.urlopen")
    def test_crawl_block_redirect_raises_value_error(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        block_resp = fake_response(
            url="https://kyfw.12306.cn/resources/error.html")
        mock_urlopen.side_effect = [init_resp, block_resp]

        client = TrainClient(timeout=5)
        with self.assertRaises(ValueError):
            client.query_tickets("VNP", "AOH", "2026-09-01")

    @patch("tools.train.client.urlopen")
    def test_non_200_raises_value_error(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        resp = fake_response(status=502,
                             url="https://kyfw.12306.cn/otn/leftTicket/queryI")
        mock_urlopen.side_effect = [init_resp, resp]

        client = TrainClient(timeout=5)
        with self.assertRaises(ValueError):
            client.query_tickets("VNP", "AOH", "2026-09-01")

    @patch("tools.train.client.urlopen")
    def test_bad_json_raises_value_error(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        resp = fake_response(url="https://kyfw.12306.cn/otn/leftTicket/queryI",
                             body=b"<html>not json</html>")
        mock_urlopen.side_effect = [init_resp, resp]

        client = TrainClient(timeout=5)
        with self.assertRaises(ValueError):
            client.query_tickets("VNP", "AOH", "2026-09-01")

    @patch("tools.train.client.urlopen")
    def test_url_error_becomes_connection_error(self, mock_urlopen) -> None:
        from urllib.error import URLError
        mock_urlopen.side_effect = URLError("connection refused")

        client = TrainClient(timeout=5)
        with self.assertRaises(ConnectionError):
            client.query_tickets("VNP", "AOH", "2026-09-01")

    @patch("tools.train.client.urlopen")
    def test_timeout_becomes_connection_error(self, mock_urlopen) -> None:
        mock_urlopen.side_effect = TimeoutError("timed out")

        client = TrainClient(timeout=5)
        with self.assertRaises(ConnectionError):
            client.query_tickets("VNP", "AOH", "2026-09-01")

    @patch("tools.train.client.urlopen")
    def test_transfer_pagination(self, mock_urlopen) -> None:
        page = lambda n: [{"middle_station_name": "南京南", "fullList": [
            {"station_train_code": f"G{n}a", "ze_num": "有"},
            {"station_train_code": f"G{n}b", "ze_num": "有"},
        ]}]
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        p1 = fake_response(url="https://kyfw.12306.cn/lcquery/queryG",
                           body=json.dumps({"data": {"middleList": page(1) * 10}}).encode("utf-8"))
        p2 = fake_response(url="https://kyfw.12306.cn/lcquery/queryG",
                           body=json.dumps({"data": {"middleList": page(2) * 3}}).encode("utf-8"))
        mock_urlopen.side_effect = [init_resp, p1, p2]

        client = TrainClient(timeout=5)
        items = client.query_transfer("VNP", "AOH", "2026-09-01")

        self.assertEqual(len(items), 13)  # 满 10 条翻页，末页 3 条停止
        second_req = mock_urlopen.call_args_list[2][0][0]
        self.assertIn("result_index=10", second_req.get_full_url())

    @patch("tools.train.client.urlopen")
    def test_resolve_train_no_by_code(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        query_resp = fake_response(
            url="https://kyfw.12306.cn/otn/leftTicket/queryI",
            body=json.dumps({"data": {"result": [
                make_ticket_row(code="G37", official="24000000G370I"),
                make_ticket_row(code="G39", official="24000000G390I"),
            ]}}).encode("utf-8"))
        mock_urlopen.side_effect = [init_resp, query_resp]

        client = TrainClient(timeout=5)
        official = client.resolve_train_no("g39", "VNP", "AOH", "2026-09-01")
        self.assertEqual(official, "24000000G390I")

    @patch("tools.train.client.urlopen")
    def test_resolve_train_no_official_passthrough(self, mock_urlopen) -> None:
        client = TrainClient(timeout=5)
        self.assertEqual(
            client.resolve_train_no("5l000G390I", "VNP", "AOH", "2026-09-01"),
            "5l000G390I")
        mock_urlopen.assert_not_called()  # 官方编号无需查询

    @patch("tools.train.client.urlopen")
    def test_resolve_train_no_unknown_raises(self, mock_urlopen) -> None:
        init_resp = fake_response(set_cookies=["JSESSIONID=abc; Path=/"])
        query_resp = fake_response(
            url="https://kyfw.12306.cn/otn/leftTicket/queryI",
            body=json.dumps({"data": {"result": []}}).encode("utf-8"))
        mock_urlopen.side_effect = [init_resp, query_resp]

        client = TrainClient(timeout=5)
        with self.assertRaises(ValueError):
            client.resolve_train_no("G99", "VNP", "AOH", "2026-09-01")


# ---------------------------------------------------------------------------
# 工具层：Mock 版与 Live 版
# ---------------------------------------------------------------------------


def _future_date(offset: int = 3) -> str:
    return (date.today() + timedelta(days=offset)).strftime("%Y-%m-%d")


class TestTrainToolsMock(unittest.TestCase):
    def test_mock_ticket(self) -> None:
        r = TrainTicketTool().execute(from_station="北京南", to_station="上海虹桥",
                                      date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("code", r.data[0])
        self.assertIn("depart_time", r.data[0])
        self.assertIn("seats", r.data[0])

    def test_mock_transfer(self) -> None:
        r = TrainTransferTool().execute(from_station="北京南", to_station="上海虹桥",
                                        date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(len(r.data[0]["segments"]), 2)

    def test_mock_route(self) -> None:
        r = TrainRouteTool().execute(train="G39", from_station="北京南",
                                     to_station="上海虹桥", date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data[0]["station_name"], "北京南")

    def test_mock_price(self) -> None:
        r = TrainPriceTool().execute(from_station="北京南", to_station="上海虹桥",
                                     date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertIn("prices", r.data[0])


class TestTrainToolsLive(unittest.TestCase):
    def _client(self) -> MagicMock:
        return MagicMock()

    def test_live_ticket_parses_and_translates_station(self) -> None:
        client = self._client()
        client.query_tickets.return_value = [make_ticket_row()]
        r = TrainTicketToolLive(client).execute(
            from_station="北京南", to_station="上海虹桥", date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        train = r.data[0]
        self.assertEqual(train["code"], "G39")
        self.assertEqual(train["from_station"], "北京南")
        self.assertEqual(train["to_station"], "上海虹桥")
        self.assertEqual(train["seats"]["business"], "3")
        client.query_tickets.assert_called_once_with("VNP", "AOH", _future_date(),
                                                     purpose_codes="ADULT")

    def test_live_ticket_limit(self) -> None:
        client = self._client()
        client.query_tickets.return_value = [
            make_ticket_row(code="G1"), make_ticket_row(code="G3"),
            make_ticket_row(code="G5")]
        r = TrainTicketToolLive(client).execute(
            from_station="北京南", to_station="上海虹桥",
            date=_future_date(), limit=2)
        self.assertEqual([t["code"] for t in r.data], ["G1", "G3"])

    def test_live_ticket_unknown_station_errors(self) -> None:
        r = TrainTicketToolLive(self._client()).execute(
            from_station="火星站", to_station="上海虹桥", date=_future_date())
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("无法识别", r.error)

    def test_live_ticket_bad_date_errors(self) -> None:
        r = TrainTicketToolLive(self._client()).execute(
            from_station="北京南", to_station="上海虹桥", date="2026-13-01")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("日期无效", r.error)

    def test_live_ticket_bad_date_format_errors(self) -> None:
        r = TrainTicketToolLive(self._client()).execute(
            from_station="北京南", to_station="上海虹桥", date="2026/09/01")
        self.assertEqual(r.status, ToolStatus.ERROR)
        self.assertIn("YYYY-MM-DD", r.error)

    def test_live_transfer(self) -> None:
        client = self._client()
        client.query_transfer.return_value = [make_transfer_item()]
        r = TrainTransferToolLive(client).execute(
            from_station="北京南", to_station="上海虹桥", date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data[0]["middle_station"], "南京南")
        client.query_transfer.assert_called_once_with(
            "VNP", "AOH", _future_date(), middle_code="", purpose_codes="00")

    def test_live_route_resolves_official_no(self) -> None:
        client = self._client()
        client.resolve_train_no.return_value = "24000000G390I"
        client.query_route.return_value = [
            {"station_no": "01", "station_name": "北京南", "arrive_time": "----",
             "start_time": "08:00", "stopover_time": "----"},
            {"station_no": "02", "station_name": "济南西", "arrive_time": "09:41",
             "start_time": "09:43", "stopover_time": "2分"},
        ]
        r = TrainRouteToolLive(client).execute(
            train="G39", from_station="北京南", to_station="上海虹桥",
            date=_future_date())
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(r.data[1]["station_name"], "济南西")
        self.assertEqual(r.data[1]["depart_time"], "09:43")  # start_time → depart_time
        client.resolve_train_no.assert_called_once()

    def test_live_price_filters_by_train(self) -> None:
        client = self._client()
        dto_g39 = {"station_train_code": "G39", "train_no": "n1", "ze_price": "6620"}
        dto_g41 = {"station_train_code": "G41", "train_no": "n2", "ze_price": "6620"}
        client.query_price.return_value = [dto_g39, dto_g41]
        r = TrainPriceToolLive(client).execute(
            from_station="北京南", to_station="上海虹桥",
            date=_future_date(), train="G39")
        self.assertEqual(r.status, ToolStatus.OK)
        self.assertEqual(len(r.data), 1)
        self.assertEqual(r.data[0]["code"], "G39")
        self.assertEqual(r.data[0]["prices"]["second_class"], 662.0)


if __name__ == "__main__":
    unittest.main()
