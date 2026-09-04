"""Demo 候选链路（四小时修复与 Demo 作战·阶段 1 + 2：1:30-3:05）。

把「锦州 → 常州 → 上海」固定场景的最小候选生成独立成受控纵向切片
（02-四小时修复与Demo执行计划.md §1:30～3:05），**不做全国实时全局最优**：

- **最小城市-车站映射**：锦州 / 常州 / 上海 的铁路站与机场（I-09：
  不依赖估算图的表外城市映射；全国多车站目录属后续）；
- **有限模板候选生成**（替代通用 BFS 逐边调真源）：直达铁路 / 直达航空 /
  飞机→火车 / 火车→飞机 四模板；航空候选来自 ``air_routes`` 拓扑
  （``AirRoutes`` 只提名不报价，锦州→常州 ✓）；
- **铁路先查先缓存**（I-06、验收「常州→上海一次规划只查一次」）：
  候选键 ``(train, origin, dest, date)``，**正/负结果都缓存**；铁路段可行后
  才把对应航空段放进航班 Top-K（≤4，一次规划内一城市对只查一次）；
  不扫描全国城市对矩阵；
- **legs 最低字段**（I-10）：mode / origin / destination /
  from_station_or_airport / to_station_or_airport / depart_datetime /
  arrive_datetime / duration_min / price / source / service_no——每条 leg
  来自**同一个完整班次**（候选行），绝不拼接不同班次的时长与价格（I-05）；
- **换乘校验（阶段 2）**：飞→火 = 航班到达 + 出机场 30min + 转场 + 进站 30min；
  火→飞 = 火车到达 + 出站 20min + 转场 + 提前 1.5h 到机场；不可接续 →
  淘汰并给出原因（``feasible``/``reject_reason``，``include_rejected`` 可审计）；
- **完整总耗时（阶段 2，I-11）**：``total_minutes`` = 各段运行 + 等待 +
  值机/出机场缓冲（段间转场含在等待内），12h 硬约束按完整总耗时过滤；
- **来源聚合（阶段 2，I-12）**：按 legs 聚合 demo_fixture / live / mixed /
  estimated（B 侧 mock→demo_fixture、真源→live）。

Demo 边界：转场用确定性表（``DEMO_TRANSFER_MINUTES``，高德实算后续接入）；
本模块不发起任何真实付费请求，provider 由调用方注入（B 侧 mock / fixture /
真源，见 ``make_demo_train_provider`` / ``make_demo_flight_provider``）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from data_transmission.enums import Mode, Source

K_TRAIN = Mode.TRAIN.value
K_FLIGHT = "flight"

# 最小城市-车站映射（Demo 固定场景；全国目录见 01-问题清单 I-09 长期修法）
DEMO_STATIONS: Dict[str, Dict[str, List[str]]] = {
    "锦州": {"rail": ["锦州站"], "air": ["锦州湾机场"]},
    "常州": {"rail": ["常州站", "常州北站"], "air": ["常州奔牛机场"]},
    "上海": {
        "rail": ["上海站", "上海虹桥站"],
        "air": ["上海虹桥国际机场", "上海浦东国际机场"],
    },
}

DEFAULT_MAX_TOTAL_MINUTES = 720  # 单程 12h 硬约束（与 city_travel 表口径一致）
DEFAULT_TOP_K = 4                # 每城市对航班验证上限（验收：Top-K ≤4）
DEFAULT_MID_CITIES_MAX = 6       # 模板「火车→飞机」候选池截断（保持"有限"模板）

# -- 换乘缓冲 / 转场：口径单点定义在 leg_connection.py（2026-09-04 抽取，
#    去程班次精排与 Demo 链路共用）；此处 re-export 保持历史 import 路径兼容 --
from data_transmission.leg_connection import (  # noqa: F401
    AIR_ARRIVE_BUFFER_MIN,
    AIR_CHECKIN_BUFFER_MIN,
    DEFAULT_TRANSFER_MIN,
    DEMO_TRANSFER_MINUTES,
    RAIL_ARRIVE_BUFFER_MIN,
    RAIL_CHECKIN_BUFFER_MIN,
    _norm_place,
    lookup_transfer_minutes,
    required_gap_minutes,
)


# ---------------------------------------------------------------------------
# 数据形状
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CandidateLeg:
    """一段**完整班次**（不是代表边拼字段）：时刻/时长/价格/班次号来自同一行。

    字段即 02 执行计划 §1:30 的 legs 最低要求（I-10 契约）。
    """

    mode: str                    # "air" | "train"
    origin: str                  # 出发城市
    destination: str             # 到达城市
    from_station_or_airport: str
    to_station_or_airport: str
    depart_datetime: str         # "2026-09-01 08:00"
    arrive_datetime: str         # "2026-09-01 09:55"
    duration_min: int
    price: float
    source: str                  # demo_fixture / live / estimate
    service_no: str              # 航班号 / 车次号

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "origin": self.origin,
            "destination": self.destination,
            "from_station_or_airport": self.from_station_or_airport,
            "to_station_or_airport": self.to_station_or_airport,
            "depart_datetime": self.depart_datetime,
            "arrive_datetime": self.arrive_datetime,
            "duration_min": self.duration_min,
            "price": self.price,
            "source": self.source,
            "service_no": self.service_no,
        }


@dataclass(frozen=True)
class RouteCandidate:
    """一组**完整班次组合**（1 或 2 段），候选链路的最小交付单位。

    ``total_minutes`` = **完整总耗时**（I-11）= 各段运行 + 等待 + 市内转场 +
    值机/安检或进站缓冲；``running_minutes`` = 纯运行时长（排序用）。
    ``feasible=False`` 表示换乘不可接续（默认不出现在返回列表，审计时
    用 ``include_rejected=True`` 查看淘汰原因）。
    """

    template: str                # direct_train / direct_flight / flight_train / train_flight
    legs: Tuple[CandidateLeg, ...]
    total_minutes: int           # 完整总耗时（含等待+转场+缓冲，阶段 2 口径）
    running_minutes: int         # Σ 各段运行时长
    total_cost: float
    transfer_wait_min: int       # 单段 = 0；多段 = 后段发时 - 前段到时（可为负）
    transfer_note: str           # 中转说明（站/机场名 + 转场分钟）
    agg_source: str              # 按 legs 聚合：demo_fixture / live / mixed / estimated
    feasible: bool = True        # 换乘接续校验结果（阶段 2）
    reject_reason: str = ""      # 不可接续的淘汰原因（feasible=False 时）

    @property
    def is_chain(self) -> bool:
        return len(self.legs) > 1

    def to_dict(self) -> Dict[str, Any]:
        return {
            "template": self.template,
            "legs": [leg.to_dict() for leg in self.legs],
            "total_minutes": self.total_minutes,
            "running_minutes": self.running_minutes,
            "total_cost": self.total_cost,
            "transfer_wait_min": self.transfer_wait_min,
            "transfer_note": self.transfer_note,
            "agg_source": self.agg_source,
            "feasible": self.feasible,
            "reject_reason": self.reject_reason,
        }


@dataclass(frozen=True)
class TransferCheck:
    """一段换乘的接续校验结论（阶段 2，I-11 换乘口径）。

    ``ok = wait_min >= required_gap_min``；``required_gap_min`` =
    出站/出机缓冲 + 市内转场 + 进站/值机缓冲（用户拍板口径）：
    - 飞→火：出机场 30min + 高德转场 + 进站 30min；
    - 火→飞：出站 20min + 高德转场 + 提前 1.5h 到机场。
    """

    ok: bool
    wait_min: int                # 实际间隔 = 后段发时 - 前段到时（可为负）
    required_gap_min: int        # 所需最小间隔（缓冲 + 转场）
    reason: str = ""             # 淘汰原因（ok=False 时说明缺多少分钟）


def _connection_gap(leg_a: CandidateLeg, leg_b: CandidateLeg) -> int:
    """前段到达 → 后段出发 的间隔分钟（跨日/解析失败按 0 处理）。"""
    return (
        _datetime_to_minutes(leg_b.depart_datetime)
        - _datetime_to_minutes(leg_a.arrive_datetime)
    )


def check_leg_connection(
    leg_a: CandidateLeg,
    leg_b: CandidateLeg,
    transfer_minutes_fn: Callable[[str, str], int] = lookup_transfer_minutes,
) -> TransferCheck:
    """校验前段到达后能否接续后段（按 mode 分派缓冲口径）；不可接续给出原因。"""
    wait = _connection_gap(leg_a, leg_b)
    transfer = transfer_minutes_fn(
        leg_a.to_station_or_airport, leg_b.from_station_or_airport
    )
    if (leg_a.mode, leg_b.mode) == (Mode.AIR.value, Mode.TRAIN.value):
        # 飞 → 火：航班到达 + 出机场 30 + 转场 + 进站 30
        required = AIR_ARRIVE_BUFFER_MIN + transfer + RAIL_CHECKIN_BUFFER_MIN
        label = "出机场"
    elif (leg_a.mode, leg_b.mode) == (Mode.TRAIN.value, Mode.AIR.value):
        # 火 → 飞：火车到达 + 出站 20 + 转场 + 提前 1.5h 到机场
        required = RAIL_ARRIVE_BUFFER_MIN + transfer + AIR_CHECKIN_BUFFER_MIN
        label = "出站"
    else:
        # 同类相接（不应出现，保守放行并在原因里标注）
        required = wait
        label = "同类"
    if wait >= required:
        return TransferCheck(ok=True, wait_min=wait, required_gap_min=required)
    return TransferCheck(
        ok=False,
        wait_min=wait,
        required_gap_min=required,
        reason=(
            f"换乘间隔不足: {leg_a.depart_datetime} {leg_a.service_no}"
            f" 于 {leg_a.arrive_datetime.split(' ')[-1]} 到达{leg_a.destination}"
            f"，{label} {AIR_ARRIVE_BUFFER_MIN if label == '出机场' else RAIL_ARRIVE_BUFFER_MIN}min"
            f"+ 转场 {transfer}min + "
            f"{'进站' if label == '出机场' else '值机/安检'} "
            f"{RAIL_CHECKIN_BUFFER_MIN if label == '出机场' else AIR_CHECKIN_BUFFER_MIN}min"
            f" = {required}min；后段 {leg_b.service_no} {leg_b.depart_datetime.split(' ')[-1]}"
            f" 发车仅间隔 {wait}min，无法接续"
        ),
    )


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _hhmm_to_minutes(text: Any) -> int:
    """``"01:24"`` / ``"5:24"`` → 分钟；不可解析 → 0。"""
    t = str(text or "").strip()
    parts = t.split(":")
    if len(parts) != 2:
        return 0
    try:
        return int(parts[0]) * 60 + int(parts[1])
    except (TypeError, ValueError):
        return 0


def _minutes_to_datetime(date_str: str, time_text: Any) -> str:
    """``"2026-09-01" + "08:00"`` → ``"2026-09-01 08:00"``；时刻缺失 → 仅日期。"""
    t = str(time_text or "").strip()
    return f"{date_str} {t}" if t else date_str


def _datetime_to_minutes(datetime_text: str) -> int:
    """``"2026-09-01 08:00"`` → 当日分钟（跨日/解析失败 → 0）。"""
    parts = str(datetime_text or "").split(" ")
    if len(parts) < 2:
        return 0
    return _hhmm_to_minutes(parts[-1])


def _row_source(row: Dict[str, Any], default: str = Source.DEMO_FIXTURE.value) -> str:
    return str(row.get("source") or default)


# ---------------------------------------------------------------------------
# 查询缓存（一次候选规划内共享）
# ---------------------------------------------------------------------------


class IntercityQueryCache:
    """城际查询缓存与调用计数：键 ``(mode, origin, destination, date)``。

    - ``mode`` ∈ {"train", "flight"}（I-06 起点：键已含 mode 与 date）；
    - **正/负结果都缓存**：负结果（None/[]）存为 None，后续模板短路，
      不再重复查同一城市对；
    - ``calls`` 记录每个键的实际调用次数，供验收断言
      「常州→上海一次规划只查一次」与「无矩阵扫描」。
    """

    def __init__(self) -> None:
        self._store: Dict[Tuple[str, str, str, str], Optional[List[Dict[str, Any]]]] = {}
        self.calls: Dict[Tuple[str, str, str, str], int] = {}

    def get_or_query(
        self,
        mode: str,
        origin: str,
        destination: str,
        date: str,
        query_fn: Callable[[], Optional[Sequence[Dict[str, Any]]]],
    ) -> Optional[List[Dict[str, Any]]]:
        key = (mode, origin, destination, date)
        if key not in self._store:
            self.calls[key] = self.calls.get(key, 0) + 1
            rows = query_fn()
            self._store[key] = list(rows) if rows else None
        return self._store[key]

    def train_calls(self, origin: str, destination: str, date: str) -> int:
        return self.calls.get((K_TRAIN, origin, destination, date), 0)

    def flight_calls(self, origin: str, destination: str, date: str) -> int:
        return self.calls.get((K_FLIGHT, origin, destination, date), 0)


# ---------------------------------------------------------------------------
# 段行 → leg
# ---------------------------------------------------------------------------


def _flight_leg(row: Dict[str, Any], origin: str, destination: str, date: str) -> CandidateLeg:
    """航班候选行 → leg（行字段见 tools/flight/tools.py，含 demo_fixture 样例）。"""
    return CandidateLeg(
        mode=Mode.AIR.value,
        origin=origin,
        destination=destination,
        from_station_or_airport=str(
            row.get("from_airport_name") or row.get("from_airport") or ""
        ),
        to_station_or_airport=str(
            row.get("to_airport_name") or row.get("to_airport") or ""
        ),
        depart_datetime=_minutes_to_datetime(date, row.get("depart_time")),
        arrive_datetime=_minutes_to_datetime(date, row.get("arrive_time")),
        duration_min=_as_int(row.get("duration_min")),
        price=_as_float(row.get("price")),
        source=_row_source(row),
        service_no=str(row.get("flight_no") or ""),
    )


def _train_leg(row: Dict[str, Any], origin: str, destination: str, date: str) -> CandidateLeg:
    """车次候选行 → leg（行字段见 tools/train/tools.py；价格 0 表示未取到票价）。"""
    return CandidateLeg(
        mode=Mode.TRAIN.value,
        origin=origin,
        destination=destination,
        from_station_or_airport=str(row.get("from_station") or ""),
        to_station_or_airport=str(row.get("to_station") or ""),
        depart_datetime=_minutes_to_datetime(date, row.get("depart_time")),
        arrive_datetime=_minutes_to_datetime(date, row.get("arrive_time")),
        duration_min=_hhmm_to_minutes(row.get("duration")),
        price=_as_float(row.get("price")),
        source=_row_source(row),
        service_no=str(row.get("code") or ""),
    )


def _aggregate_source(legs: Sequence[CandidateLeg]) -> str:
    """按 legs 简单聚合来源（I-12 起点；阶段 2 细化 live/mixed/estimated）。"""
    sources = {leg.source for leg in legs}
    if not sources:
        return Source.ESTIMATED.value
    if len(sources) == 1:
        return next(iter(sources))
    return "mixed"  # 聚合表达：不同来源段混合（方案 P1 明确保留，不进逐段 source）


# ---------------------------------------------------------------------------
# 主入口：有限模板候选生成
# ---------------------------------------------------------------------------


def build_demo_candidates(
    origin: str,
    destination: str,
    date: str,
    *,
    train_provider: Callable[[str, str, str], Optional[Sequence[Dict[str, Any]]]],
    flight_provider: Callable[[str, str, str], Optional[Sequence[Dict[str, Any]]]],
    air_routes: Any,
    priority: Optional[str] = None,
    max_total_minutes: int = DEFAULT_MAX_TOTAL_MINUTES,
    top_k: int = DEFAULT_TOP_K,
    mid_cities_max: int = DEFAULT_MID_CITIES_MAX,
    cache: Optional[IntercityQueryCache] = None,
    transfer_provider: Optional[Callable[[str, str], int]] = None,
    include_rejected: bool = False,
) -> List[RouteCandidate]:
    """按四模板生成「锦州→常州→上海」式候选链路（阶段 2 口径，含换乘校验）。

    provider 契约：``fn(origin, destination, date) -> Optional[list[dict]]``——
    train 返回车次候选行（code/depart_time/arrive_time/duration/price/
    from_station/to_station），flight 返回航班候选行（flight_no/时刻/duration_min/
    price/机场名）；查询失败或空都返回 None/[]（诚实降级，不假装）。

    模板与执行顺序：
    1. ``direct_train`` 直达铁路：``train(origin→destination)``；
    2. ``direct_flight`` 直达航空：**先看拓扑** ``air_routes.has`` 才查航班；
    3. ``flight_train`` 飞机→火车：``AirOut(origin)`` 邻居 m →
       **铁路先查** ``train(m→destination)``（缓存），可行才查 ``flight(origin→m)``；
    4. ``train_flight`` 火车→飞机：``air_routes.in_cities(destination)`` 截断
       ``mid_cities_max`` → 铁路先查 ``train(origin→m)``，可行且拓扑有
       ``m→destination`` 才查航班。

    阶段 2（2:25~3:05）在阶段 1 候选链路上加：
    - **换乘校验**（I-10/I-11）：链式候选按用户拍板口径检查接续——飞→火 =
      航班到达 + 出机场 30min + 转场 + 进站 30min；火→飞 = 火车到达 + 出站 20min
      + 转场 + 提前 1.5h 到机场；**不可接续 → 淘汰（feasible=False + reject_reason）**；
    - **完整总耗时**（I-11）：``total_minutes`` = 各段运行 + 等待 + 值机/出机场
      缓冲（段间转场已含在等待内），12h 硬约束按完整耗时过滤；
    - **来源聚合**（I-12）：按 legs 聚合 demo_fixture / live / mixed / estimated；
    - 每条 leg 仍来自**同一个完整班次**（I-05，绝不拼接不同班次时长+价格）。

    默认只返回可接续候选（按完整总耗时升序）；``include_rejected=True`` 时
    同时返回被淘汰候选（feasible=False，reject_reason 给出原因，供审计/演练）。
    """
    if not origin or not destination or origin == destination:
        return []
    cache = cache or IntercityQueryCache()
    transfer_fn = transfer_provider or lookup_transfer_minutes
    candidates: List[RouteCandidate] = []

    def train_rows(a: str, b: str) -> Optional[List[Dict[str, Any]]]:
        return cache.get_or_query(K_TRAIN, a, b, date, lambda: train_provider(a, b, date))

    def flight_rows(a: str, b: str) -> List[Dict[str, Any]]:
        """航班查询 + Top-K 截断：一城市对一次规划内只查一次，候选 ≤ top_k。"""
        rows = cache.get_or_query(K_FLIGHT, a, b, date, lambda: flight_provider(a, b, date))
        return (rows or [])[:top_k]

    def add(template: str, legs: Sequence[CandidateLeg], transfer_note: str) -> None:
        running = sum(leg.duration_min for leg in legs)
        wait = 0
        check = None
        if len(legs) > 1:
            wait = _connection_gap(legs[0], legs[1])
            check = check_leg_connection(legs[0], legs[1], transfer_fn)
            if not transfer_note:
                transfer_note = (
                    f"中转 {legs[0].destination}：{legs[0].to_station_or_airport}"
                    f" → {legs[1].from_station_or_airport}（转场 "
                    f"{lookup_transfer_minutes(legs[0].to_station_or_airport, legs[1].from_station_or_airport)}min）"
                )
        # 完整总耗时（I-11）：直达航班含值机+出机场；链式首段航空含值机、
        # 末段航空含出机场；段间缓冲已含在等待内
        if template == "direct_flight":
            full_total = running + AIR_CHECKIN_BUFFER_MIN + AIR_ARRIVE_BUFFER_MIN
        elif template == "flight_train":
            full_total = AIR_CHECKIN_BUFFER_MIN + running + max(wait, 0)
        elif template == "train_flight":
            full_total = running + max(wait, 0) + AIR_CHECKIN_BUFFER_MIN + AIR_ARRIVE_BUFFER_MIN
        else:
            full_total = running
        if full_total > max_total_minutes:
            return  # 12h 硬约束（完整总耗时口径）
        feasible = check is None or check.ok
        candidates.append(RouteCandidate(
            template=template,
            legs=tuple(legs),
            total_minutes=full_total,
            running_minutes=running,
            total_cost=sum(leg.price for leg in legs),
            transfer_wait_min=wait,
            transfer_note=transfer_note,
            agg_source=_aggregate_source(legs),
            feasible=feasible,
            reject_reason=check.reason if check is not None and not check.ok else "",
        ))

    # 1) 直达铁路
    for row in train_rows(origin, destination) or []:
        add("direct_train", (_train_leg(row, origin, destination, date),),
            "直达铁路，无换乘")

    # 2) 直达航空（拓扑提名后才查航班）
    if air_routes.has(origin, destination, date):
        for row in flight_rows(origin, destination):
            add("direct_flight", (_flight_leg(row, origin, destination, date),),
                "直达航空，无换乘")

    # 3) 飞机→火车（核心 Demo 链）：AirOut(origin) → 铁路先查
    for mid in air_routes.out_cities(origin, date):
        if mid in (origin, destination):
            continue
        mid_trains = train_rows(mid, destination)
        if not mid_trains:
            continue  # 铁路不可行 → 不再为该航空段查询/付费验证
        for flight_row in flight_rows(origin, mid):
            for train_row in mid_trains:
                add(
                    "flight_train",
                    (
                        _flight_leg(flight_row, origin, mid, date),
                        _train_leg(train_row, mid, destination, date),
                    ),
                    "",
                )

    # 4) 火车→飞机（候选池截断，保持有限）
    for mid in air_routes.in_cities(destination, date)[:mid_cities_max]:
        if mid in (origin, destination):
            continue
        mid_trains = train_rows(origin, mid)
        if not mid_trains:
            continue  # 铁路不可行 → 该 m 无候选
        if not air_routes.has(mid, destination, date):
            continue  # 拓扑无 m→destination 直飞 → 不查该航班对（提名后再验证）
        for train_row in mid_trains:
            for flight_row in flight_rows(mid, destination):
                add(
                    "train_flight",
                    (
                        _train_leg(train_row, origin, mid, date),
                        _flight_leg(flight_row, mid, destination, date),
                    ),
                    "",
                )

    # 完整总耗时升序（时间优先，Demo 固定输入）；被淘汰候选带原因、沉底
    candidates.sort(key=lambda c: (c.feasible is False, c.total_minutes, c.total_cost))
    if not include_rejected:
        candidates = [c for c in candidates if c.feasible]
    return candidates


# ---------------------------------------------------------------------------
# B 侧工具 → provider 工厂（ToolResult → 候选行 list / None）
# ---------------------------------------------------------------------------


def _tool_payload_rows(result: Any) -> Optional[List[Dict[str, Any]]]:
    """ToolResult/dict → 业务候选行 list；error/空 → None（与 live_data 口径一致）。"""
    if result is None:
        return None
    if isinstance(result, dict):
        data = result.get("data")
        if isinstance(data, list) and data:
            return [r for r in data if isinstance(r, dict)]
        return None
    status = getattr(result, "status", None)
    if status is not None:
        status_value = getattr(status, "value", status)
        if str(status_value).lower() != "ok":
            return None
    data = getattr(result, "data", None)
    if isinstance(data, list) and data:
        return [r for r in data if isinstance(r, dict)]
    return None


_SOURCE_FALLBACK = {
    Source.MOCK.value: Source.DEMO_FIXTURE.value,
    Source.DEMO_FIXTURE.value: Source.DEMO_FIXTURE.value,
    Source.LIVE.value: Source.LIVE.value,
    "real_api": Source.LIVE.value,
}


def _row_source_from_result(row: Dict[str, Any], result: Any) -> Dict[str, Any]:
    """行源标记：行自带 source 优先；否则按 ToolResult.source 映射（mock→demo_fixture）。"""
    if row.get("source"):
        return row
    result_source = str(getattr(result, "source", "") or "")
    out = dict(row)
    out["source"] = _SOURCE_FALLBACK.get(result_source, result_source) or Source.DEMO_FIXTURE.value
    return out


def make_demo_train_provider(
    tool_provider: Any,
) -> Callable[[str, str, str], Optional[List[Dict[str, Any]]]]:
    """B 侧 ``train_ticket`` 工具 → ``fn(origin, dest, date) -> list[dict] | None``。

    入参直接传城市名（B 侧 mock 的城市对校验 / Live 的站名解析内部处理）；
    工具 error / 非 list / 空 → None（不假装，由模板逐段丢弃）。
    """

    def provider(origin: str, destination: str, date: str) -> Optional[List[Dict[str, Any]]]:
        try:
            result = tool_provider.call(
                "train_ticket", from_station=origin, to_station=destination, date=date
            )
        except Exception:  # noqa: BLE001  工具缺/参数错 → 该段无车次
            return None
        rows = _tool_payload_rows(result)
        if not rows:
            return None
        return [_row_source_from_result(row, result) for row in rows]

    return provider


def make_demo_flight_provider(
    tool_provider: Any,
) -> Callable[[str, str, str], Optional[List[Dict[str, Any]]]]:
    """B 侧 ``flight_search`` 工具 → ``fn(origin, dest, date) -> list[dict] | None``。

    工具 error / 非 list / 空 → None；航班行自带 source（demo_fixture/live）。
    """

    def provider(origin: str, destination: str, date: str) -> Optional[List[Dict[str, Any]]]:
        try:
            result = tool_provider.call(
                "flight_search", from_city=origin, to_city=destination,
                date=date, limit=DEFAULT_TOP_K,
            )
        except Exception:  # noqa: BLE001
            return None
        rows = _tool_payload_rows(result)
        if not rows:
            return None
        return [_row_source_from_result(row, result) for row in rows]

    return provider


# ---------------------------------------------------------------------------
# B 入口段组装（3:35 集成验收）：候选链路 → 与 build_trip_segments 同构的 segments
# ---------------------------------------------------------------------------


def _demo_leg_to_intercity(leg: CandidateLeg) -> Dict[str, Any]:
    """CandidateLeg → B 侧 intercity leg（对齐 ``travel._build_route_legs`` 结构，
    并带班次与发到时刻——验收三：每段包含方式/起终点/站点机场/发到时刻/费用/来源）。"""
    return {
        "kind": "intercity",
        "from": leg.from_station_or_airport,
        "to": leg.to_station_or_airport,
        "mode": Mode.AIR.value if leg.mode == Mode.AIR.value else Mode.TRAIN.value,
        "service_no": leg.service_no,
        "depart_datetime": leg.depart_datetime,
        "arrive_datetime": leg.arrive_datetime,
        "duration_min": int(leg.duration_min),
        "buffer_min": (
            AIR_CHECKIN_BUFFER_MIN if leg.mode == Mode.AIR.value else RAIL_CHECKIN_BUFFER_MIN
        ),
        "cost_per_person": float(leg.price),
        "source": leg.source,
        "note": f"城际主段（{leg.mode}）",
    }


def _demo_local_leg(a: str, b: str, note: str) -> Dict[str, Any]:
    return {
        "kind": "local", "from": a, "to": b,
        "duration_min": None, "mode": "", "source": "", "note": note,
    }


def build_demo_trip_segments(
    plan: Dict[str, Any],
    requirement: Dict[str, Any],
    *,
    tool_provider: Any = None,
) -> List[Dict[str, Any]]:
    """固定 Demo 场景（锦州→上海 + travel_schedule 日期）从 B 入口生成去程段。

    仅在 requirement 匹配固定场景且存在**可接续候选**时返回段列表（结构对齐
    ``travel.build_trip_segments``：type/name/day_label/start/end/duration +
    details.from/to/mode/from_station/to_station/cost_per_person/source/kind/
    stops/legs）；否则返回 []（调用方走原链路，非 Demo 场景零影响）。

    - 候选 = ``build_demo_candidates`` 完整总耗时升序的第一条可行（时间优先）；
    - 段时长 = **完整总耗时**（含等待/转场/值机进站缓冲，I-11）；
    - legs 每条 intercity 段带 service_no + 发到时刻（同一完整班次，I-05）；
    - 来源 = 按 legs 聚合（demo_fixture / mixed，I-12），绝不冒充 live；
    - 不发起任何真实付费请求（fixture / B 侧 mock 工具）。
    """
    try:
        from data_transmission.air_routes import load_air_routes
    except ImportError:  # pragma: no cover
        return []
    content = requirement.get("content") or {}
    origin = (content.get("origin") or "").strip()
    destination = (content.get("destination") or "").strip()
    if not (origin == "锦州" and destination == "上海"):
        return []  # 非固定 Demo 场景 → 调用方走原链
    schedule = content.get("travel_schedule") or {}
    departure_date = schedule.get("departure_date")
    if not departure_date:
        return []
    if tool_provider is None:
        return []

    candidates = build_demo_candidates(
        origin, destination, departure_date,
        train_provider=make_demo_train_provider(tool_provider),
        flight_provider=make_demo_flight_provider(tool_provider),
        air_routes=load_air_routes(),
    )
    if not candidates:
        return []  # 无可行候选 → 调用方走原链（诚实降级）
    best = candidates[0]  # 时间优先：完整总耗时最短的可行候选

    legs = best.legs
    first, last = legs[0], legs[-1]
    via = " → ".join(leg.destination for leg in legs[:-1])
    name = f"{origin} → {via} → {destination}（联运）" if best.is_chain else (
        f"{origin} → {destination}（{('air' if first.mode == 'air' else 'train')}）"
    )
    out_legs: List[Dict[str, Any]] = [
        _demo_local_leg(origin, first.from_station_or_airport or origin,
                        "市内衔接（Demo 机场/车站接驳占位）")
    ]
    prev_to = first.to_station_or_airport
    for i, leg in enumerate(legs):
        if i > 0:
            transfer_min = lookup_transfer_minutes(prev_to, leg.from_station_or_airport)
            out_legs.append(_demo_local_leg(
                prev_to, leg.from_station_or_airport,
                f"同城转场 {transfer_min}min（{prev_to} → {leg.from_station_or_airport}）",
            ))
        out_legs.append(_demo_leg_to_intercity(leg))
        prev_to = leg.to_station_or_airport
    out_legs.append(_demo_local_leg(
        last.to_station_or_airport or destination, destination,
        "市内衔接（Demo 到达接驳占位）",
    ))

    start = _hhmm_to_minutes(schedule.get("departure_time")) if schedule.get(
        "departure_time") else 480
    return [{
        "type": "transport",
        "name": name,
        "day_label": departure_date,
        "start_minutes": start,
        "end_minutes": start + best.total_minutes,
        "duration_minutes": best.total_minutes,
        "details": {
            "from": origin,
            "to": destination,
            "mode": "联运" if best.is_chain else (
            Mode.AIR.value if first.mode == Mode.AIR.value else Mode.TRAIN.value
        ),
            "from_station": first.from_station_or_airport,
            "to_station": last.to_station_or_airport,
            "cost_per_person": best.total_cost,
            "source": best.agg_source,
            "kind": "outbound",
            "stops": [leg.destination for leg in legs[:-1]] if best.is_chain else [],
            "transfer_note": best.transfer_note,
            "transfer_wait_min": best.transfer_wait_min,
            "running_minutes": best.running_minutes,
            "reject_reason": best.reject_reason or "",
            "legs": out_legs,
        },
    }]