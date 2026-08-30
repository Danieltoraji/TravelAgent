"""服务器侧功能冒烟（验收清单 1/3/4 + 清单 2 LLM 决策）。

由 deploy.yml 在部署后于容器内执行：
    docker compose exec -T web python smoke/smoke_acceptance.py

- 清单 1：POST /api/plan/ 生成 TripTimeline（走 HTTP，面向运行中的 gunicorn）
- 清单 3：酒店满房 → BOOKING 事件 → A 硬规则换酒店（BLOCKED Action + 新时间轴）
- 清单 4：导出 markdown/ics
- 清单 2：本进程内直接注入 SCENIC 事件驱动真实运行时（MockWorld 排队数据
  最高 40 < 阈值 50，poll 无法自然触发），走真实 decision_hook；
  LLM 决策需 DEEPSEEK_API_KEY（.env 由 GitHub Secrets 生成），未配置时跳过并提示。

任何断言失败以非 0 退出，Actions 日志即验收报告。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

# 容器内路径：脚本位于 /app/django_server/smoke/，需把 B 仓库根(/app)、
# django_server 与 a_side 都放进 sys.path（与 settings.py 的路径布局一致）
_APP = Path(__file__).resolve().parent.parent          # /app/django_server
_ROOT = _APP.parent                                    # /app
for _p in (str(_ROOT), str(_APP), str(_ROOT / "a_side")):
    if _p not in sys.path:
        sys.path.append(_p)

BASE = os.environ.get("SMOKE_BASE", "http://127.0.0.1:8000")
HOTEL_JSON = _ROOT / "a_side" / "fake_spots" / "beijing" / "hotel.json"


def post(path: str, payload: dict):
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return e.code, json.loads(body) if body else {"error": e.reason}


def get(path: str, retries: int = 3):
    """GET（带重试）：gunicorn 单 worker（内存单例约束），规划类长请求
    （2 天行程含锚点间隔 ~30s+）占用期间，后续短请求需排队——部署后冒烟
    与外部探测请求并发时曾 30s 超时误报失败（8.30 线上教训）。
    每次尝试 60s + 失败退避 5s，纯读接口重试安全。"""
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(BASE + path, timeout=60) as r:
                return r.status, json.loads(r.read().decode("utf-8"))
        except (TimeoutError, OSError) as exc:
            last_exc = exc
            print(f"    [smoke] GET {path} 超时/网络错误（尝试 {attempt + 1}/{retries}），5s 后重试：{exc}")
            time.sleep(5)
    raise AssertionError(f"GET {path} 重试 {retries} 次仍失败: {last_exc}")


def requirement() -> dict:
    return {
        "content": {
            "destination": "北京",
            "start_date": "2026-08-23",
            "days": 2,
            "visitor_number": 2,
            "constraints": {
                "budget": 2000,
                "must_visit": ["故宫"],
                "required_tags": [],
                "dismissed_tags": ["拥挤"],
                "daily_travel_time": 480,
                "include_meal_time_in_daily_limit": False,
            },
            "preferences": {"preferred_tags": ["历史文化"], "avoid_tags": []},
        }
    }


def main() -> None:
    from runtime.agent_runtime import runtime  # noqa: E402  延迟：需 sys.path 就绪

    llm_key = bool(os_environ_key())
    print(f"LLM key in container: {'SET' if llm_key else 'MISSING'}")

    # ── 清单 1：规划 ──────────────────────────────────────────────
    code, resp = post("/api/plan/", requirement())
    assert code == 200 and resp.get("status") == "ok", f"plan failed: {code} {resp}"
    tl = resp["timeline"]
    assert tl["city"] == "北京" and len(tl["days"]) == 2, tl
    # 8.27：酒店初始规划接入——时间轴每天末尾应有 hotel 段（cost 并入房费）
    hotel_cats = [p.get("category") for day in tl["days"] for p in day.get("items", [])]
    assert "hotel" in hotel_cats, f"timeline missing hotel segment: {hotel_cats}"
    print(f"[1] /api/plan/ -> ok, city={tl['city']} days={len(tl['days'])} cost={tl['total_cost']} hotel=yes")

    code, status = get("/api/status/")
    assert status.get("timeline_set") is True, status
    print("[1] /api/status -> timeline_set=True")

    # ── 清单 1.5：train / web 工具可调（R3：新增覆盖）─────────────
    train_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")
    code, resp = post("/api/tools/train_ticket/invoke/", {
        "from_station": "北京南", "to_station": "上海虹桥",
        "date": train_date,
    })
    # 结构性错误（工具缺失/路由错）必须失败；Live 模式下 12306 反爬/预售期
    # 属环境性失败，报告但不阻断部署（Mock 模式恒 ok）
    assert code == 200, f"train_ticket route broken: {code} {resp}"
    if resp.get("status") == "ok":
        print(f"[1.5] train_ticket invoke -> ok ({len(resp.get('data') or [])} trains)")
    else:
        assert "12306" in str(resp.get("error", "")), f"train_ticket failed: {resp}"
        print(f"[1.5] train_ticket invoke -> 环境性失败（容忍）: {resp.get('error')}")

    code, resp = post("/api/tools/web_search/invoke/", {"query": "故宫 门票"})
    assert code == 200 and resp.get("status") == "ok", f"web_search failed: {code} {resp}"
    print("[1.5] web_search invoke -> ok")

    # ── 清单 3：booking 满房 → 事件 → A 换酒店 ────────────────────
    with open(HOTEL_JSON, encoding="utf-8") as f:
        hotel = json.load(f)["hotels"][0]
    place = f"{hotel['name']}（满房）"
    code, rec = post("/api/booking/prepare/", {
        "place": place, "target_date": "2026-08-23",
        "party_size": 2, "booking_type": "hotel",
    })
    assert code == 200, f"prepare failed: {code} {rec}"
    bid = rec["booking_id"]
    print(f"[3] prepare -> {bid}")

    code, resp = post(f"/api/booking/{bid}/confirm/", {})
    # 8.27：confirm 失败从 500（空 body）改为 400 + 结构化信息（对 C 端友好）
    assert code == 400, f"confirm should fail with 400: {code} {resp}"
    assert "满房" in str(resp.get("error", "")), f"error missing 满房: {resp}"
    assert resp.get("booking", {}).get("status") == "failed", f"booking not failed: {resp}"
    blocked_in_body = [a for a in resp.get("actions", []) if a.get("status") == "blocked"]
    assert blocked_in_body, f"no BLOCKED action in body: {resp}"
    print(f"[3] confirm -> 失败(HTTP {code}, booking=failed, action=blocked)")

    code, ev = get("/api/events/")
    booking_events = [e for e in ev["events"] if e.get("event_type") == "booking"]
    assert booking_events, f"no BOOKING event: {ev}"
    be = booking_events[-1]
    print(f"[3] /api/events -> BOOKING: {be.get('place')} data={be.get('data')}")

    code, rp = get("/api/replans/")
    assert rp["count"] >= 1, rp
    last = rp["replans"][-1]
    reason = (last.get("decision") or {}).get("reason", "")
    diff = (last.get("decision") or {}).get("diff_summary", [])
    assert "满房" in reason, f"reason missing 满房: {reason}"
    assert any("hotel_changed" in d for d in diff), f"no hotel_changed: {diff}"
    print(f"[3] /api/replans -> decision: {reason[:50]}... | diff: {diff[:1]}")

    code, acts = get("/api/actions/")
    blocked = [a for a in acts["actions"] if a.get("status") == "blocked"]
    assert blocked, f"no BLOCKED action: {acts}"
    print(f"[3] /api/actions -> BLOCKED: {blocked[0]['title']}")

    code, tl2 = get("/api/timeline/")
    assert tl2.get("days"), "empty timeline after replan"
    print(f"[3] /api/timeline -> 重规划后 days={len(tl2['days'])}")

    # ── 清单 4：导出 ──────────────────────────────────────────────
    _, md = get("/api/export/markdown/")
    assert "北京" in md.get("content", ""), "markdown missing city"
    _, ics = get("/api/export/ics/")
    assert "VCALENDAR" in ics.get("content", ""), "ics missing VCALENDAR"
    print("[4] export markdown/ics -> ok")

    # ── 清单 2：SCENIC 事件 → LLM 决策 → 重规划（本进程驱动真实运行时）──
    if not llm_key:
        print("[2] 清单 2（LLM 决策）跳过：容器内未配置 DEEPSEEK_API_KEY/GLM_API_KEY"
              "（在 GitHub Secrets 添加后重新部署即可）")
    else:
        runtime.init_from_requirement(requirement())
        agent = runtime.require_agent()
        spot = next((it.name for d in runtime.timeline.days for it in d.items
                     if it.category == "scenic"), "故宫")
        from core.schemas import EventType, MonitorEvent  # noqa: E402
        before = len(runtime.replan_history)
        asyncio.run(agent.handle_event(MonitorEvent(
            event_id="smoke-scenic-1",
            event_type=EventType.SCENIC,
            place=spot,
            observed_at=datetime.now(),
            rule_name="smoke-acceptance",
            spot_id="",
            data={"queue_min": 120},
        )))
        after = len(runtime.replan_history)
        assert after > before, "SCENIC 决策未触发（replan_history 无新增）"
        decision = runtime.replan_history[-1].get("decision") or {}
        print(f"[2] SCENIC 注入 -> LLM 决策 -> replan 成功 (+{after - before}) "
              f"reason={str(decision.get('reason'))[:60]}")

    print("\nSERVER SMOKE ALL GREEN（清单 1/3/4" + (" + 2" if llm_key else "，清单 2 跳过") + "）")


def os_environ_key() -> str:
    import os
    return os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("GLM_API_KEY") or ""


if __name__ == "__main__":
    main()