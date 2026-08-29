"""航班查询客户端：双后端（aviationstack / juhe 聚合数据-飞常准）。

设计
----
- 后端可插拔：``FlightClient(backend="aviationstack" | "juhe")``，key 从
  ``config.settings`` 读（``aviationstack_key`` / ``juhe_flight_key``）；
- 统一输出 list[dict]（与 Mock 同构，调用方零改动）：:
      {"flight_no": "CA1501", "airline": "中国国航",
       "from_airport": "PEK", "to_airport": "SHA",
       "depart_time": "08:00", "arrive_time": "10:25",
       "duration_min": 145, "price": 1080.0, "date": "2026-09-01"}
- 免费档限制（aviationstack 100 次/月；juhe 按次付费）→ 查询失败 / 无结果 /
  key 缺失一律抛 ``ConnectionError``（可重试）或 ``ValueError``（业务错），
  由上层 Tool 统一转 ToolResult；A 侧适配层再按 None 降级估算表。
- **juhe 端点/参数为文档推断，尚未实测**：聚合数据-飞常准（id=123）页面为
  JS 动态渲染拿不到静态参数；key 开通后须按真实文档校准 ``_JUHE_PATHS`` 与
  响应解析（见 ``parse_*``）。aviationstack 端点已用真实请求验证（401 仅缺 key）。

仅标准库零依赖。网络错误统一转 ConnectionError（BaseTool 会重试）。
"""

from __future__ import annotations

import json
import logging
import re
import ssl
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("tools.flight")

# aviationstack：免费档 100 次/月；access_key 注入 query（已验证 URL 可达）
_AVSTACK_BASE = "https://api.aviationstack.com/v1/flights"
# juhe 聚合数据-航班查询（1962 接口）——端点/参数已按官方调用示例校准：
#   https://apis.juhe.cn/flight/query?key=...&departure=...&arrival=...&departureDate=...
_JUHE_QUERY_URL = "https://apis.juhe.cn/flight/query"

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
}

# 证书链问题同 12306：部分环境校验失败
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# aviationstack 航司 IATA → 中文名（仅常用，兜底原样返回）
_AIRLINE_ZH = {
    "CA": "中国国航", "MU": "东方航空", "CZ": "南方航空", "HU": "海南航空",
    "ZH": "深圳航空", "3U": "四川航空", "MF": "厦门航空", "FM": "上海航空",
    "SC": "山东航空", "GS": "天津航空", "9C": "春秋航空", "KN": "中国联合航空",
    "HO": "吉祥航空", "G5": "华夏航空", "KY": "昆明航空", "8L": "祥鹏航空",
    "EU": "成都航空", "PN": "西部航空", "DZ": "东海航空", "AQ": "九元航空",
}


def validate_flight_date(date_str: str) -> None:
    """校验 YYYY-MM-DD 格式与范围（今天 ~ 今天+60 天，航司预售一般提前 60 天）。"""
    if not _DATE_RE.match(date_str or ""):
        raise ValueError("日期格式错误，请使用 YYYY-MM-DD")
    try:
        query_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期无效: {date_str}") from exc
    today = date.today()
    if query_date < today:
        raise ValueError(
            f"出发日期不能早于今天（{today:%Y-%m-%d}），无法查询历史日期的航班"
        )
    max_date = today + timedelta(days=60)
    if query_date > max_date:
        raise ValueError(
            f"出发日期不能晚于 {max_date:%Y-%m-%d}（航司一般仅提前 60 天售票）"
        )


def airline_zh(iata: str) -> str:
    """航司 IATA → 中文名；未收录返回原码。"""
    return _AIRLINE_ZH.get((iata or "").strip().upper(), iata or "")


def _hhmm(iso: Any) -> str:
    """ISO 时间（'2026-09-01T08:00:00+08:00' 等）→ HH:MM；无法解析返回 ""。"""
    if not iso:
        return ""
    text = str(iso)
    m = re.search(r"(\d{1,2}):(\d{2})", text)
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else ""


def _duration_min(dep: Any, arr: Any) -> int:
    """起降时间 → 分钟（跨日正确）；任一缺 → 0。"""
    dep_s, arr_s = str(dep or ""), str(arr or "")
    if not dep_s or not arr_s:
        return 0
    m_dep = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})", dep_s)
    m_arr = re.search(r"(\d{4})-(\d{2})-(\d{2})[T ](\d{1,2}):(\d{2})", arr_s)
    if not m_dep or not m_arr:
        return 0
    def ts(m: Any) -> int:
        from datetime import datetime as _dt
        return int(_dt(
            int(m.group(1)), int(m.group(2)), int(m.group(3)),
            int(m.group(4)), int(m.group(5)),
        ).timestamp())
    return max(0, int(round((ts(m_arr) - ts(m_dep)) / 60)))


# ---------------------------------------------------------------------------
# 响应解析（纯函数，便于单测）
# ---------------------------------------------------------------------------


def parse_avstack_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """一条 aviationstack flight 记录 → 统一输出；无航班号/无起降 → None。

    aviationstack 免费档返回实时/当日航班；响应用例（官方文档结构）::

        {"flight_date": "2026-09-01", "flight_status": "scheduled",
         "departure": {"airport": "...", "iata": "PEK",
                       "scheduled": "2026-09-01T08:00:00+08:00", ...},
         "arrival": {"iata": "SHA", "scheduled": "...", ...},
         "airline": {"name": "Air China", "iata": "CA"},
         "flight": {"number": "1501", "iata": "CA1501", "icao": "CCA1501"}}
    """
    if not isinstance(row, dict):
        return None
    flight = row.get("flight") or {}
    flight_no = (flight.get("iata") or flight.get("number") or "").strip()
    dep = row.get("departure") or {}
    arr = row.get("arrival") or {}
    dep_iata = (dep.get("iata") or "").strip()
    arr_iata = (arr.get("iata") or "").strip()
    if not (flight_no and dep_iata and arr_iata):
        return None
    airline = (row.get("airline") or {}).get("iata") or flight_no[:2]
    dep_sched = dep.get("scheduled") or dep.get("estimated")
    arr_sched = arr.get("scheduled") or arr.get("estimated")
    date_str = _as_date_str(row.get("flight_date"), dep_sched)
    return {
        "flight_no": flight_no,
        "airline": airline_zh(airline),
        "airline_iata": airline,
        "from_airport": dep_iata,
        "to_airport": arr_iata,
        "depart_time": _hhmm(dep_sched),
        "arrive_time": _hhmm(arr_sched),
        "duration_min": _duration_min(dep_sched, arr_sched),
        "price": 0.0,          # aviationstack 免费档无票价
        "date": date_str,
        "status": (row.get("flight_status") or "").strip(),
    }


def _as_date_str(flight_date: Any, iso: Any) -> str:
    """优先 flight_date 字段，否则从 ISO 时刻提取日期。"""
    if flight_date:
        text = str(flight_date)
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if m:
            return m.group(1)
    if iso:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", str(iso))
        if m:
            return m.group(1)
    return date.today().isoformat()


def _duration_to_min(text: Any) -> int:
    """"1h45m" / "2h5m" 等时长串 → 分钟；无法解析返回 0。"""
    t = str(text or "").strip()
    if not t:
        return 0
    h = re.search(r"(\d+)h", t)
    m = re.search(r"(\d+)m", t)
    minutes = 0
    if h:
        minutes += int(h.group(1)) * 60
    if m:
        minutes += int(m.group(1))
    return minutes


def parse_juhe_row(row: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """聚合数据-航班查询（1962）一条 flightInfo → 统一输出（已按实测结构校准）。

    实测结构（2026-08-29 探针确证）::
        {"airline": "CA", "airlineName": "中国国际航空公司", "flightNo": "CA8341",
         "isCodeShare": false, "equipment": "320",
         "departure": "PKX", "departureName": "大兴国际机场",
         "departureDate": "2026-09-05", "departureTime": "22:00",
         "arrivalDate": "2026-09-05", "arrivalTime": "23:45",
         "arrival": "PVG", "arrivalName": "浦东国际机场",
         "duration": "1h45m", "transferNum": 1, "ticketPrice": 468, "segments": []}
    """
    if not isinstance(row, dict):
        return None
    flight_no = str(
        row.get("flightNo") or row.get("flight_no") or row.get("code") or ""
    ).strip()
    if not flight_no:
        return None
    dep_iata = str(row.get("departure") or "").strip()
    arr_iata = str(row.get("arrival") or "").strip()
    dep_time = str(row.get("departureTime") or row.get("dep_time") or "")
    arr_time = str(row.get("arrivalTime") or row.get("arr_time") or "")
    price = 0.0
    try:
        price = float(row.get("ticketPrice") or row.get("price") or 0)
    except (TypeError, ValueError):
        price = 0.0
    return {
        "flight_no": flight_no,
        "airline": str(row.get("airlineName") or row.get("airline") or ""),
        "airline_iata": str(row.get("airline") or flight_no[:2]),
        "from_airport": _airport_code(dep_iata) or _airport_code(row.get("departureName")),
        "to_airport": _airport_code(arr_iata) or _airport_code(row.get("arrivalName")),
        "from_airport_name": str(row.get("departureName") or ""),
        "to_airport_name": str(row.get("arrivalName") or ""),
        "depart_time": dep_time,
        "arrive_time": arr_time,
        "duration_min": _duration_to_min(row.get("duration"))
        or int(row.get("duration_min") or 0),
        "price": price,
        "date": str(row.get("departureDate") or row.get("date") or date.today().isoformat()),
        "status": str(row.get("status") or ""),
        "transfer_num": int(row.get("transferNum") or 0),
    }


def _airport_code(value: Any) -> str:
    """机场字段（三字码 或 中文名）→ 三字码；解析失败原样返回。"""
    text = str(value or "").strip()
    if not text:
        return ""
    m = re.search(r"\b([A-Z]{3})\b", text)
    return m.group(1) if m else text


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


class FlightClient:
    """航班查询客户端（aviationstack / juhe 双后端）。

    key 缺失或后端异常时抛异常（ConnectionError 可重试 / ValueError 业务错），
    由上层 Tool 统一处理降级；不静默返回假数据。
    """

    def __init__(self, backend: str = "juhe",
                 api_key: str = "", timeout: float | None = None) -> None:
        from config.settings import settings
        self.backend = (backend or "juhe").strip().lower()
        self._api_key = api_key or settings.juhe_flight_key
        if self.backend == "aviationstack" and not self._api_key:
            self._api_key = settings.aviationstack_key
        self._timeout = timeout if timeout is not None else settings.api_timeout
        if self.backend not in ("aviationstack", "juhe"):
            raise ValueError(f"未知航班后端: {backend}（aviationstack | juhe）")

    # -- 对外查询 ---------------------------------------------------------

    def query_flights(self, from_airport: str, to_airport: str,
                      date_str: str) -> List[Dict[str, Any]]:
        """城市对/机场对 → 当日航班列表（统一输出结构，见模块 docstring）。"""
        validate_flight_date(date_str)
        if not self._api_key:
            raise ValueError(
                f"航班后端 {self.backend} 未配置 API Key（settings 或环境变量），"
                "无法进行真源查询"
            )
        from tools.flight.airports import resolve_airport
        dep = resolve_airport(from_airport)
        arr = resolve_airport(to_airport)
        if self.backend == "juhe":
            return self._query_juhe(dep, arr, date_str)
        return self._query_avstack(dep, arr, date_str)

    # -- aviationstack ---------------------------------------------------

    def _query_avstack(self, dep: str, arr: str, date_str: str) -> List[Dict[str, Any]]:
        params = {
            "access_key": self._api_key,
            "dep_iata": dep,
            "arr_iata": arr,
            "flight_date": date_str,
            "limit": "100",
        }
        payload = self._get_json(f"{_AVSTACK_BASE}?{urlencode(params)}", "aviationstack 航班查询")
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            logger.warning("aviationstack 未返回 data 列表: %s", str(payload)[:200])
            return []
        rows = [parse_avstack_row(r) for r in data]
        out = [r for r in rows if r is not None]
        logger.info("Flight(avstack): %s→%s %s → %d 航班", dep, arr, date_str, len(out))
        return out

    # -- juhe ------------------------------------------------------------

    def _query_juhe(self, dep: str, arr: str, date_str: str) -> List[Dict[str, Any]]:
        """聚合数据-航班查询（1962 接口，参数/端点已按官方示例校准）。

        入参：key / departure / arrival / departureDate / flightNo / maxSegments。
        城市对用三字码（dep/arr 已由 resolve_airport 转换）。
        """
        params = {
            "key": self._api_key,
            "departure": dep,
            "arrival": arr,
            "departureDate": date_str,
            "flightNo": "",
            "maxSegments": "",
        }
        payload = self._get_json(
            f"{_JUHE_QUERY_URL}?{urlencode(params)}",
            "juhe 航班查询",
        )
        # juhe 结构（实测确证）：{"reason":"成功","result":{"orderid":...,"flightInfo":[...]},
        # "error_code":0}；error_code 非 0 为业务错误（如 10012 次数不足）
        if isinstance(payload, dict) and payload.get("error_code"):
            raise ValueError(
                f"juhe 航班接口业务错误: {payload.get('reason') or payload.get('error_code')}"
            )
        result = payload.get("result") if isinstance(payload, dict) else payload
        items = None
        if isinstance(result, dict):
            items = result.get("flightInfo") or result.get("list")
        elif isinstance(result, list):
            items = result
        if not isinstance(items, list):
            logger.warning("juhe 未返回航班列表: %s", str(payload)[:200])
            return []
        rows = [parse_juhe_row(r) for r in items]
        out = [r for r in rows if r is not None]
        logger.info("Flight(juhe): %s→%s %s → %d 航班", dep, arr, date_str, len(out))
        return out

    # -- HTTP 底座 ---------------------------------------------------------

    def _get_json(self, url: str, operation: str) -> Any:
        req = Request(url, headers=_HEADERS)
        try:
            with urlopen(req, timeout=self._timeout, context=_SSL_CTX) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "") == "gzip":
                    import gzip
                    raw = gzip.decompress(raw)
                text = raw.decode("utf-8", errors="replace")
        except URLError as exc:
            raise ConnectionError(f"{operation}请求失败 [{url}]: {exc}") from exc
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError(f"{operation}响应解析失败: {text[:120]}") from exc