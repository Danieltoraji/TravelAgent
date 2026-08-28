"""12306 查询客户端：会话 Cookie + 余票 / 中转换乘 / 经停站 / 票价 四个查询。

参照 Reference Code/mcp-server-12306 的实现移植到标准库 urllib：
- 请求前先 GET /otn/leftTicket/init 获取会话 Cookie（JSESSIONID/route 等，
  12306 无 token 机制），后续请求原样回传；
- 余票/换乘接口 URL 的 query 后缀字母会被 12306 不定期轮换（曾 U→G→I），
  依赖 urllib 自动跟随重定向；失效时按实际跳转地址更新 _QUERY_PATHS；
- 12306 证书链在部分 Python 环境校验失败，参考实现同样关闭了证书校验；
- 余票响应为 "|" 分隔字符串数组，字段索引见 _SEAT_INDICES 旁注。

仅标准库零依赖。URLError/超时统一转 ConnectionError，交由 BaseTool
的指数退避重试（每次重试会重新 init 会话）；反爬拦截转 ValueError 不重试。
"""

from __future__ import annotations

import gzip
import json
import logging
import re
import ssl
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

logger = logging.getLogger("tools.train")

_BASE = "https://kyfw.12306.cn"
_INIT_PATH = "/otn/leftTicket/init"

# query 后缀字母由 12306 不定期轮换；若反爬拦截持续出现，按真实跳转地址更新
_QUERY_PATHS = {
    "tickets": "/otn/leftTicket/queryI",
    "transfer": "/lcquery/queryG",
    "price": "/otn/leftTicketPrice/queryAllPublicPrice",
    "route": "/otn/czxx/queryByTrainNo",
}

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/123.0.0.0 Safari/537.36"
    ),
    "Referer": f"{_BASE}/otn/leftTicket/init",
    "Host": "kyfw.12306.cn",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Connection": "keep-alive",
    "X-Requested-With": "XMLHttpRequest",
    "Origin": _BASE,
}

# 12306 证书链在部分环境校验失败（参考实现 verify=False 同款处理）
_SSL_CTX = ssl.create_default_context()
_SSL_CTX.check_hostname = False
_SSL_CTX.verify_mode = ssl.CERT_NONE


# ---------------------------------------------------------------------------
# 日期校验
# ---------------------------------------------------------------------------

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def validate_depart_date(date_str: str) -> None:
    """校验 YYYY-MM-DD 格式与 12306 预售期（今天 ~ 今天+14 天），违规抛 ValueError。"""
    if not _DATE_RE.match(date_str or ""):
        raise ValueError("日期格式错误，请使用 YYYY-MM-DD")
    try:
        query_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"日期无效: {date_str}") from exc
    today = date.today()
    if query_date < today:
        raise ValueError(
            f"出发日期不能早于今天（{today:%Y-%m-%d}），12306 无法查询历史日期的车次"
        )
    max_date = today + timedelta(days=14)
    if query_date > max_date:
        raise ValueError(
            f"出发日期不能晚于 {max_date:%Y-%m-%d}，12306 仅支持提前 14 天购票"
        )


# ---------------------------------------------------------------------------
# 响应解析（纯函数，便于单测）
# ---------------------------------------------------------------------------

# 余票行 "|" 分隔字段索引（12306 未公开文档，随版本可能变化）：
# [1]预订/停运标记 [2]官方编号 [3]车次号 [6]/[7]出发/到达电报码
# [8]出发时刻 [9]到达时刻 [10]历时 [21~33]各坐席余票
_SEAT_INDICES = {
    "premium_soft_sleeper": 21,  # 高级软卧
    "soft_sleeper": 23,          # 软卧
    "soft_seat": 24,             # 软座
    "no_seat": 26,               # 无座
    "hard_sleeper": 28,          # 硬卧
    "hard_seat": 29,             # 硬座
    "second_class": 30,          # 二等座
    "first_class": 31,           # 一等座
    "business": 32,              # 商务座
    "dongwo": 33,                # 动卧
}

# 中转方案段坐席字段 → 输出键名
_TRANSFER_SEAT_MAP = {
    "swz_num": "business",
    "tz_num": "premium_seat",
    "zy_num": "first_class",
    "ze_num": "second_class",
    "gr_num": "premium_soft_sleeper",
    "rw_num": "soft_sleeper",
    "rz_num": "first_class_sleeper",
    "yw_num": "hard_sleeper",
    "yz_num": "hard_seat",
    "wz_num": "no_seat",
}

# 票价字段 → 输出键名（原始单位 0.1 元）
_PRICE_FIELDS = {
    "swz_price": "business",
    "tdz_price": "premium_seat",
    "zy_price": "first_class",
    "ze_price": "second_class",
    "gr_price": "premium_soft_sleeper",
    "rw_price": "soft_sleeper",
    "dw_price": "dongwo",
    "yw_price": "hard_sleeper",
    "yz_price": "hard_seat",
    "wz_price": "no_seat",
}


def _official_train_no(parts: List[str]) -> str:
    """从余票行提取官方列车编号（跟在"预订"标记后）；停运等无标记行返回 ""。"""
    try:
        return parts[parts.index("预订") + 1].strip()
    except (ValueError, IndexError):
        return ""


def parse_ticket_row(row: str) -> Optional[Dict[str, Any]]:
    """解析一行余票记录（"|" 分隔字符串）；列数不足返回 None。"""
    parts = row.split("|")
    if len(parts) < 35:
        return None
    seats: Dict[str, str] = {}
    for name, idx in _SEAT_INDICES.items():
        val = parts[idx].strip()
        if val and val != "--":   # "--" 表示该车次无此坐席
            seats[name] = val
    return {
        "code": parts[3].strip(),                       # 车次号，如 G39
        "train_no": _official_train_no(parts),          # 官方编号，经停查询需要
        "status": parts[1].strip(),                     # 预订 / 停运 等标记
        "from_station_code": parts[6],
        "to_station_code": parts[7],
        "depart_time": parts[8],
        "arrive_time": parts[9],
        "duration": parts[10],
        "seats": seats,
    }


def parse_transfer_item(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """解析一条中转换乘方案（两段行程）；不足两段返回 None。"""
    legs = item.get("fullList") or item.get("trainList") or []
    if len(legs) < 2:
        return None
    segments = []
    for seg in legs:
        seats = {}
        for field, name in _TRANSFER_SEAT_MAP.items():
            val = (seg.get(field) or "").strip()
            if val and val != "--":
                seats[name] = val
        segments.append({
            "code": seg.get("station_train_code", ""),
            "from_station": seg.get("from_station_name", ""),
            "to_station": seg.get("to_station_name", ""),
            "depart_time": seg.get("start_time", ""),
            "arrive_time": seg.get("arrive_time", ""),
            "duration": seg.get("lishi", ""),
            "seats": seats,
        })
    return {
        "middle_station": item.get("middle_station_name")
        or (legs[0].get("to_station_name", "") if legs else ""),
        "wait_time": item.get("wait_time", ""),
        "total_duration": item.get("all_lishi", ""),
        "segments": segments,
    }


def parse_route_station(st: Dict[str, Any]) -> Dict[str, Any]:
    """解析一个经停站记录（首站无到达时刻、末站无出发时刻，12306 返回 "----"）。"""
    return {
        "station_no": st.get("station_no", st.get("from_station_no", "")),
        "station_name": st.get("station_name", st.get("from_station_name", "")),
        "arrive_time": st.get("arrive_time", "----"),
        "depart_time": st.get("start_time", "----"),
        "stopover_time": st.get("stopover_time", "----"),
    }


def parse_price_row(dto: Dict[str, Any]) -> Dict[str, Any]:
    """解析一条票价记录（queryLeftNewDTO），价格从 0.1 元换算为元。"""
    prices = {}
    for field, name in _PRICE_FIELDS.items():
        val = dto.get(field)
        if val and val != "--":
            prices[name] = int(val) / 10 if isinstance(val, str) and val.isdigit() else val
    return {
        "code": dto.get("station_train_code", ""),
        "train_no": dto.get("train_no", ""),
        "from_station": dto.get("from_station_name", ""),
        "to_station": dto.get("to_station_name", ""),
        "depart_time": dto.get("start_time", ""),
        "arrive_time": dto.get("arrive_time", ""),
        "duration": dto.get("lishi", ""),
        "prices": prices,
    }


# ---------------------------------------------------------------------------
# HTTP 客户端
# ---------------------------------------------------------------------------


def _merge_cookies(existing: str, set_cookie_headers: List[str]) -> str:
    """合并 Set-Cookie 响应头到现有 Cookie 串（同名新值覆盖旧值，丢弃路径等属性）。"""
    jar: Dict[str, str] = {}
    for pair in existing.split("; "):
        if "=" in pair:
            k, v = pair.split("=", 1)
            jar[k.strip()] = v.strip()
    for header in set_cookie_headers:
        pair = header.split(";", 1)[0]
        if "=" in pair:
            k, v = pair.split("=", 1)
            jar[k.strip()] = v.strip()
    return "; ".join(f"{k}={v}" for k, v in jar.items())


class TrainClient:
    """12306 只读查询客户端（无 API Key，会话 Cookie 自动维持）。

    一个实例内复用会话 Cookie；线程安全性未做保证，与工具层单线程调用约定一致。
    """

    def __init__(self, timeout: float | None = None) -> None:
        from config.settings import settings
        self._timeout = timeout if timeout is not None else settings.api_timeout
        self._cookies: str = ""

    # -- 公开查询 ---------------------------------------------------------

    def query_tickets(self, from_code: str, to_code: str, date_str: str,
                      purpose_codes: str = "ADULT") -> List[str]:
        """余票查询，返回原始 "|" 分隔字符串列表（交由 parse_ticket_row 解析）。"""
        data = self._get_json(_QUERY_PATHS["tickets"], {
            "leftTicketDTO.train_date": date_str,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": purpose_codes,
        }, "余票查询")
        result = ((data or {}).get("data") or {}).get("result") or []
        return [r for r in result if isinstance(r, str)]

    def query_transfer(self, from_code: str, to_code: str, date_str: str,
                       middle_code: str = "", purpose_codes: str = "00",
                       show_no_seat: bool = False, max_pages: int = 10) -> List[Dict[str, Any]]:
        """中转换乘查询。接口每页固定 10 条，自动翻页直到取完。"""
        all_items: List[Dict[str, Any]] = []
        result_index = 0
        for _ in range(max_pages):
            data = self._get_json(_QUERY_PATHS["transfer"], {
                "train_date": date_str,
                "from_station_telecode": from_code,
                "to_station_telecode": to_code,
                "middle_station": middle_code,
                "result_index": str(result_index),
                "can_query": "Y",
                "isShowWZ": "Y" if show_no_seat else "N",
                "purpose_codes": purpose_codes,
                "channel": "E",
            }, "中转换乘查询")
            page = ((data or {}).get("data") or {}).get("middleList") or []
            if not page:
                break
            all_items.extend(page)
            if len(page) < 10:
                break
            result_index += 10
        return all_items

    def query_route(self, train_no: str, from_code: str, to_code: str,
                    date_str: str) -> List[Dict[str, Any]]:
        """经停站查询。train_no 须为官方编号（车次号先经 resolve_train_no 转换）。"""
        data = self._get_json(_QUERY_PATHS["route"], {
            "train_no": train_no,
            "from_station_telecode": from_code,
            "to_station_telecode": to_code,
            "depart_date": date_str,
        }, "经停站查询")
        d = (data or {}).get("data") or {}
        stations = d.get("data") or []
        if not stations and "middleList" in d:
            stations = []
            for m in d["middleList"]:
                stations.extend(m.get("fullList") or [])
        if not stations:
            stations = d.get("fullList") or d.get("route") or []
        return stations

    def query_price(self, from_code: str, to_code: str, date_str: str,
                    purpose_codes: str = "ADULT") -> List[Dict[str, Any]]:
        """票价查询，返回该线路全部车次的 queryLeftNewDTO 列表。"""
        data = self._get_json(_QUERY_PATHS["price"], {
            "leftTicketDTO.train_date": date_str,
            "leftTicketDTO.from_station": from_code,
            "leftTicketDTO.to_station": to_code,
            "purpose_codes": purpose_codes,
        }, "票价查询")
        items = (data or {}).get("data") or []
        return [it.get("queryLeftNewDTO", {})
                for it in items if isinstance(it, dict) and it.get("queryLeftNewDTO")]

    def resolve_train_no(self, train: str, from_code: str, to_code: str,
                         date_str: str) -> str:
        """车次号（G39）→ 官方编号；官方编号或无法转换时原样返回。"""
        t = (train or "").strip().upper()
        if not re.fullmatch(r"[A-Z]+\d+", t):
            return (train or "").strip()
        rows = self.query_tickets(from_code, to_code, date_str)
        for row in rows:
            parsed = parse_ticket_row(row)
            if parsed and parsed["code"].upper() == t and parsed["train_no"]:
                return parsed["train_no"]
        raise ValueError(
            f"未找到车次 {train} 的列车编号（{from_code}→{to_code} {date_str}），"
            "请确认车次在该线路当日开行"
        )

    # -- HTTP 底座 ---------------------------------------------------------

    def _ensure_session(self) -> None:
        """首次请求前访问 init 页获取会话 Cookie。"""
        if self._cookies:
            return
        status, final_url, _ = self._get(_BASE + _INIT_PATH)
        logger.debug("Train: init 会话 status=%s url=%s", status, final_url)

    def _get(self, url: str) -> tuple[int, str, str]:
        """GET 一个 12306 页面/接口，返回 (status, final_url, text)。

        自动回传并收集会话 Cookie；gzip 解压；网络错误统一转
        ConnectionError（可被 BaseTool 重试）。
        """
        headers = dict(_HEADERS)
        if self._cookies:
            headers["Cookie"] = self._cookies
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=self._timeout, context=_SSL_CTX) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "") == "gzip":
                    raw = gzip.decompress(raw)
                new_cookies = resp.headers.get_all("Set-Cookie") or []
                if new_cookies:
                    self._cookies = _merge_cookies(self._cookies, new_cookies)
                m = re.search(r"charset=([\w-]+)", resp.headers.get("Content-Type", ""))
                charset = m.group(1) if m else "utf-8"
                return resp.status, resp.url or url, raw.decode(charset, errors="replace")
        except (URLError, TimeoutError, OSError) as exc:
            # TimeoutError/OSError（含 socket.timeout）与 URLError 同样视为可重试网络错误
            raise ConnectionError(f"12306请求失败 [{url}]: {exc}") from exc

    def _get_json(self, path: str, params: Dict[str, str], operation: str) -> Any:
        """带会话的接口 GET + 反爬检测 + JSON 解析。"""
        self._ensure_session()
        url = f"{_BASE}{path}?{urlencode(params)}"
        status, final_url, text = self._get(url)
        self._check_crawl_block(status, final_url)
        try:
            return json.loads(text)
        except ValueError as exc:
            raise ValueError(f"12306响应解析失败（{operation}）: {text[:120]}") from exc

    @staticmethod
    def _check_crawl_block(status: int, final_url: str) -> None:
        """12306 把反爬命中的请求重定向到错误页，最终 URL 带这些特征。"""
        if (status != 200 or "error.html" in final_url
                or "/ntce/" in final_url or "resources/error" in final_url):
            raise ValueError(
                f"12306接口返回异常或反爬拦截 (status={status}, url={final_url})，"
                "请稍后重试"
            )
