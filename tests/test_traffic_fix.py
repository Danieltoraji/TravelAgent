"""C 端报告交通故障修复测试：T1 city 透传 / T2 限流重试 / T3 动线监控 / T4 错误分类。"""

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

from core.schemas import (
    DayPlan,
    EventType,
    Place,
    ToolResult,
    ToolStatus,
    TripTimeline,
)
from execution.execution_agent import ExecutionAgent, _classify_error
from tools.amap_client import AmapClient
from tools.traffic_tool import TrafficToolLive


def _timeline(city: str = "广州", places=None) -> TripTimeline:
    items = places if places is not None else [
        Place(name="广州塔", category="scenic", arrival="09:00"),
        Place(name="陈家祠", category="scenic", arrival="14:00"),
    ]
    return TripTimeline(
        city=city,
        start_date=date(2026, 9, 5),
        end_date=date(2026, 9, 6),
        days=[DayPlan(day=1, date=date(2026, 9, 5), items=items)],
    )


# ---------------------------------------------------------------------------
# T1：traffic 工具 city 透传
# ---------------------------------------------------------------------------


class TestT1TrafficCity(unittest.TestCase):
    def _tool(self):
        client = MagicMock()
        client.geocode.return_value = (23.1, 113.3)
        client.get_route.return_value = {"distance": 5000, "duration": 1500}
        return TrafficToolLive(client), client

    def test_city_passed_to_geocode_and_route(self) -> None:
        tool, client = self._tool()
        r = tool.execute(origin="广州塔", destination="陈家祠", city="广州")
        self.assertEqual(r.status, ToolStatus.OK)
        client.geocode.assert_any_call("广州塔", city="广州")
        client.geocode.assert_any_call("陈家祠", city="广州")
        client.get_route.assert_called_once_with(
            (23.1, 113.3), (23.1, 113.3), mode="transit", city="广州",
        )

    def test_without_city_keeps_compat(self) -> None:
        # city 缺省 → geocode 不限定（全国）；transit 规划回退北京（调用方应显式传）
        tool, client = self._tool()
        tool.execute(origin="广州塔", destination="陈家祠")
        client.geocode.assert_any_call("广州塔", city="")
        client.get_route.assert_called_once_with(
            (23.1, 113.3), (23.1, 113.3), mode="transit", city="北京",
        )


# ---------------------------------------------------------------------------
# T2：geocode / get_route 限流退避重试
# ---------------------------------------------------------------------------


class TestT2TransientRetry(unittest.TestCase):
    def _client(self):
        return AmapClient(api_key="dummy")

    def test_geocode_retries_on_qps(self) -> None:
        client = self._client()
        responses = [
            ValueError("高德 API 错误 [10021]: CUQPS_HAS_EXCEEDED_THE_LIMIT"),
            {"geocodes": [{"location": "113.30,23.10"}]},
        ]
        with patch.object(client, "_get", side_effect=responses) as mock_get, \
                patch("tools.amap_client.time.sleep"):
            lat, lng = client.geocode("广州塔")
        self.assertEqual((lat, lng), (23.1, 113.3))
        self.assertEqual(mock_get.call_count, 2)

    def test_non_transient_error_no_retry(self) -> None:
        client = self._client()
        err = ValueError("高德 API 错误 [10001]: INVALID_USER_KEY")
        with patch.object(client, "_get", side_effect=[err]) as mock_get, \
                patch("tools.amap_client.time.sleep"):
            with self.assertRaises(ValueError):
                client.geocode("广州塔")
        self.assertEqual(mock_get.call_count, 1)

    def test_all_attempts_exhausted_raises(self) -> None:
        client = self._client()
        err = ValueError("高德 API 错误 [10021]: CUQPS")
        with patch.object(client, "_get", side_effect=[err] * 3) as mock_get, \
                patch("tools.amap_client.time.sleep"):
            with self.assertRaises(ValueError):
                client.geocode("广州塔")
        self.assertEqual(mock_get.call_count, 3)


# ---------------------------------------------------------------------------
# T3/T4：监控动线与错误分类
# ---------------------------------------------------------------------------


def _recording_agent(timeline, result):
    registry = MagicMock()
    registry.call.return_value = result
    agent = ExecutionAgent(timeline=timeline, registry=registry)
    return agent, registry


class TestT3TrafficPair(unittest.TestCase):
    def test_uses_first_two_places_with_city(self) -> None:
        agent, registry = _recording_agent(
            _timeline(),
            ToolResult(tool="traffic", status=ToolStatus.OK,
                       data={"duration_min": 30}, source="mock"),
        )
        agent._poll_traffic_pair()
        kwargs = registry.call.call_args.kwargs
        self.assertEqual(kwargs["origin"], "广州塔")
        self.assertEqual(kwargs["destination"], "陈家祠")
        self.assertEqual(kwargs["city"], "广州")

    def test_fallback_single_place(self) -> None:
        agent, registry = _recording_agent(
            _timeline(places=[Place(name="广州塔", category="scenic", arrival="09:00")]),
            ToolResult(tool="traffic", status=ToolStatus.OK,
                       data={"duration_min": 30}, source="mock"),
        )
        agent._poll_traffic_pair()
        kwargs = registry.call.call_args.kwargs
        self.assertEqual(kwargs["origin"], "广州")     # 回退：城市名
        self.assertEqual(kwargs["destination"], "广州塔")

    def test_hotel_excluded_from_places(self) -> None:
        # 到达点排除 hotel 段：动线监控测的是游客行程（广州塔→陈家祠）
        timeline = _timeline(places=[
            Place(name="酒店", category="hotel", arrival="20:00"),
            Place(name="广州塔", category="scenic", arrival="09:00"),
            Place(name="陈家祠", category="scenic", arrival="14:00"),
        ])
        agent, registry = _recording_agent(
            timeline,
            ToolResult(tool="traffic", status=ToolStatus.OK,
                       data={"duration_min": 30}, source="mock"),
        )
        agent._poll_traffic_pair()
        kwargs = registry.call.call_args.kwargs
        self.assertEqual(kwargs["origin"], "广州塔")
        self.assertEqual(kwargs["destination"], "陈家祠")


class TestT4ErrorPayload(unittest.TestCase):
    def _agent_with_error(self, error: str):
        agent, registry = _recording_agent(
            _timeline(),
            ToolResult(tool="traffic", status=ToolStatus.ERROR,
                       data=None, error=error),
        )
        return agent, registry

    def test_rate_limited_classified(self) -> None:
        agent, registry = self._agent_with_error("高德 API 错误 [10021]: CUQPS")
        data = agent._poll(EventType.TRAFFIC, origin="广州塔",
                           destination="陈家祠", city="广州")
        self.assertEqual(data["error_category"], "RATE_LIMITED")
        self.assertEqual(data["params"]["origin"], "广州塔")
        self.assertEqual(data["status"], "error")

    def test_geocode_not_found_classified(self) -> None:
        agent, registry = self._agent_with_error("高德地理编码未找到地址: 某景点")
        data = agent._poll(EventType.TRAFFIC, origin="某景点",
                           destination="陈家祠", city="广州")
        self.assertEqual(data["error_category"], "GEOCODE_NOT_FOUND")
        self.assertEqual(data["params"]["destination"], "陈家祠")

    def test_other_category(self) -> None:
        agent, registry = self._agent_with_error("未知错误")
        data = agent._poll(EventType.TRAFFIC, origin="a", destination="b", city="c")
        self.assertEqual(data["error_category"], "OTHER")
        self.assertEqual(data["params"], {"origin": "a", "destination": "b",
                                          "city": "c"})


if __name__ == "__main__":
    unittest.main()
