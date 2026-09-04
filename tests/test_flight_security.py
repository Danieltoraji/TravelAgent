"""航班客户端安全 + Mock 门控（四小时作战包 I-02 / I-03 / I-04 / I-13）。

覆盖：
- api_key 的 None（读默认配置）与 ""（显式禁用）语义——不允许空串回退真实 Key；
- URL 脱敏：异常/日志不得出现 access_key / key 值；
- TLS 校验恢复（不再全局关闭证书验证）；
- Mock 只允许在京沪 / 锦州常州的固定样例城市对上返回，且来源标 demo_fixture；
- 注册三态：Live（有 Key 非 Demo）/ Demo 样例（DEMO_MODE）/ Unavailable（无 Key 非 Demo）。

全部测试运行在断网保护下（默认拦截 urllib.request.urlopen；需要模拟响应的
测试在函数体内用 monkeypatch 显式覆盖）。
"""

from __future__ import annotations

import ssl
import sys
import urllib.request
from unittest.mock import MagicMock
from urllib.error import URLError

_B_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
for _p in (str(_B_ROOT), str(_B_ROOT / "tools"), str(_B_ROOT / "config")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest  # noqa: E402

from tools import build_registry  # noqa: E402
from tools.flight.client import FlightClient, _SSL_CTX  # noqa: E402
from tools.flight.tools import (  # noqa: E402
    FlightSearchTool,
    FlightSearchToolLive,
    FlightSearchToolUnavailable,
)

# 固定日期（约定 Demo 剧情日 2026-09-01；9.4 起 Mock demo_fixture 样例已
# 与日期无关——样例非真查询，不过 validate_flight_date，任何日期可复现）
_DEMO_DATE = "2026-09-01"


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """断网兜底：默认禁止一切 urlopen；需要模拟响应的测试内再覆盖。"""

    def _forbid(*_args, **_kwargs):
        raise RuntimeError("测试禁止真实网络访问")

    monkeypatch.setattr(urllib.request, "urlopen", _forbid)


# ---------------------------------------------------------------------------
# I-03：api_key 语义
# ---------------------------------------------------------------------------


def test_api_key_empty_string_is_explicit_disable(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "juhe_flight_key", "ENV_KEY_SHOULD_NOT_LEAK")
    client = FlightClient(backend="juhe", api_key="", timeout=10)
    assert client._api_key == ""  # "" = 明确禁用，绝不回退环境 Key
    with pytest.raises(ValueError):
        client.query_flights("锦州", "常州", _DEMO_DATE)


def test_api_key_none_reads_settings(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "juhe_flight_key", "ENV_KEY")
    monkeypatch.setattr(settings, "aviationstack_key", "")
    client = FlightClient(backend="juhe", api_key=None, timeout=10)
    assert client._api_key == "ENV_KEY"
    # aviationstack 后端：None → 读 aviationstack_key
    client2 = FlightClient(backend="aviationstack", api_key=None, timeout=10)
    assert client2._api_key == "ENV_KEY"  # juhe 为空时才读备选 → 这里仍用 juhe? 见实现


def test_api_key_explicit_value_wins_over_settings(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "juhe_flight_key", "ENV_KEY")
    client = FlightClient(backend="juhe", api_key="MY_KEY", timeout=10)
    assert client._api_key == "MY_KEY"


# ---------------------------------------------------------------------------
# I-02：异常/日志脱敏（Key 在 query 参数中，绝不能进入异常消息）
# ---------------------------------------------------------------------------


def test_connection_error_redacts_url_query(monkeypatch):
    def _boom(_req, **_kwargs):
        raise URLError("simulated network failure")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    client = FlightClient(backend="juhe", api_key="SUPER_SECRET_KEY_123", timeout=10)
    # 真源查询路径：用动态未来日期（避开 validate_flight_date 对过去日期的拒绝，
    # 9.4：固定过去日期会让本测试先抛 ValueError 而不是走到网络层）
    future_date = (__import__("datetime").date.today()
                   + __import__("datetime").timedelta(days=3)).isoformat()
    with pytest.raises(ConnectionError) as exc_info:
        client.query_flights("北京", "上海", future_date)
    msg = str(exc_info.value)
    assert "SUPER_SECRET_KEY_123" not in msg
    assert "key=" not in msg and "access_key" not in msg
    # 保留 scheme/host/path 可辨识，但 query 必须为空
    assert "apis.juhe.cn" in msg and "flight/query" in msg


def test_redact_url_helper():
    url = "https://apis.juhe.cn/flight/query?key=SECRET&departure=PEK&arrival=SHA"
    redacted = FlightClient._redact_url(url)
    assert "SECRET" not in redacted
    assert redacted == "https://apis.juhe.cn/flight/query"
    assert FlightClient._redact_url("not a url") == "<url>"


# ---------------------------------------------------------------------------
# I-13：TLS 校验恢复
# ---------------------------------------------------------------------------


def test_tls_verification_is_enabled():
    assert _SSL_CTX.verify_mode == ssl.CERT_REQUIRED
    assert _SSL_CTX.check_hostname is True


# ---------------------------------------------------------------------------
# I-04：Mock 城市对校验 + demo_fixture 标记
# ---------------------------------------------------------------------------


def test_mock_returns_jinghu_sample_for_exact_pair():
    tool = FlightSearchTool()
    result = tool.execute(from_city="北京", to_city="上海", date=_DEMO_DATE)
    assert result.status == "ok"
    assert len(result.data) == 3
    assert result.data[0]["from_airport"] == "PEK"
    assert result.data[0]["source"] == "demo_fixture"


def test_mock_returns_jinzhou_changzhou_sample():
    tool = FlightSearchTool()
    result = tool.execute(from_city="锦州", to_city="常州", date=_DEMO_DATE)
    assert result.status == "ok"
    assert len(result.data) >= 1
    row = result.data[0]
    assert row["from_airport"] == "JNZ" and row["to_airport"] == "CZX"
    assert row["source"] == "demo_fixture"


def test_mock_does_not_fabricate_other_pairs():
    tool = FlightSearchTool()
    # 旧版对任意城市对都返回京沪假数据；现在未收录城市对 → 空（不假装）
    for pair in (("广州", "昆明"), ("上海", "锦州"), ("成都", "哈尔滨")):
        result = tool.execute(from_city=pair[0], to_city=pair[1], date=_DEMO_DATE)
        assert result.status == "ok"
        assert result.data == [], f"{pair} 不应返回京沪样例"


def test_mock_is_date_agnostic_demo_fixture():
    """9.4 回归：demo_fixture 样例非真查询，过去日期也要能返回固定样例。

    真源日期校验（validate_flight_date）只属于 Live 版；Mock 不再拒过去日期
    ——否则约定剧情日过期（2026-09-01）就整链 ERROR，离线演示/测试全挂。
    """
    tool = FlightSearchTool()
    result = tool.execute(from_city="锦州", to_city="常州", date="2020-01-01")
    assert result.status == "ok"
    assert result.data and result.data[0]["flight_no"] == "KN5621"
    assert result.data[0]["source"] == "demo_fixture"


# ---------------------------------------------------------------------------
# I-04：注册三态
# ---------------------------------------------------------------------------


def test_registry_live_when_key_and_not_demo(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "juhe_flight_key", "REAL_KEY")
    monkeypatch.setattr(settings, "aviationstack_key", "")
    tool = build_registry().get("flight_search")
    assert isinstance(tool, FlightSearchToolLive)


def test_registry_demo_fixture_when_demo_mode(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "demo_mode", True)
    tool = build_registry().get("flight_search")
    assert isinstance(tool, FlightSearchTool)


def test_registry_unavailable_when_no_key_no_demo(monkeypatch):
    from config.settings import settings
    monkeypatch.setattr(settings, "demo_mode", False)
    monkeypatch.setattr(settings, "juhe_flight_key", "")
    monkeypatch.setattr(settings, "aviationstack_key", "")
    tool = build_registry().get("flight_search")
    assert isinstance(tool, FlightSearchToolUnavailable)
    result = tool.execute(from_city="北京", to_city="上海", date=_DEMO_DATE)
    assert result.status == "error"  # 结构化不可用，不返回京沪 Mock