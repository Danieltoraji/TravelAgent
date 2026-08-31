"""演示用突发事件注入脚本（仅标准库，可在任意机器上运行）。

用法（详见 docs/demo_event_injection.md）：

  单发预设：
    python demo/inject_events.py --base http://127.0.0.1:8000 storm
    python demo/inject_events.py queue --place 故宫
    python demo/inject_events.py traffic_jam --place 北京-故宫
    python demo/inject_events.py hotel_full --place 皇城景观酒店

  剧情三连（storm → queue → traffic_jam，间隔可配）：
    python demo/inject_events.py --all --interval 3

  原始注入（任意事件类型 + 自定义数据）：
    python demo/inject_events.py raw --event-type scenic --place 故宫 \
        --data '{"queue_min": 120}'

  穿透后从任意设备触发（base 换成你的隧道地址）：
    python demo/inject_events.py --base http://节点地址:端口 storm

  服务端设置了 DEBUG_INJECT_TOKEN 时：
    python demo/inject_events.py --token 你的token storm
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Optional

# Windows 控制台默认 GBK，无法打印 emoji；统一重配置为 UTF-8（失败则忽略）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

SCENARIOS = ("storm", "queue", "traffic_jam", "hotel_full")


def post(base: str, payload: Dict[str, Any], token: Optional[str]) -> Dict[str, Any]:
    """POST /api/debug/inject/，返回响应 JSON；HTTP/连接错误直接退出。"""
    url = base.rstrip("/") + "/api/debug/inject/"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Debug-Token"] = token
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"  ✗ HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}",
              file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"  ✗ 连接失败: {exc.reason}（确认 --base 指向运行中的 Django，且隧道已通）",
              file=sys.stderr)
        sys.exit(1)


def summarize(resp: Dict[str, Any]) -> None:
    """打印注入结果摘要。"""
    ev = resp.get("event") or {}
    decision = resp.get("decision")
    label = {
        "replanned": "✅ 已重规划",
        "recorded": "📋 已决策（无新方案）",
        "hook_error": "⚠️ 决策引擎报错（看服务端日志）",
        "not_significant": "⏭ 未达影响阈值，未触发决策",
    }.get(decision, decision or "?")
    print(f"  {label}  {ev.get('event_type')} @ {ev.get('place')}")
    if decision == "replanned":
        replan = (resp.get("replan") or {}).get("decision") or {}
        reason = replan.get("reason", "")
        if reason:
            print(f"      原因: {reason}")
        for d in replan.get("diff_summary", []):
            print(f"      • {d}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TravelAgent 演示突发事件注入")
    ap.add_argument("--base", default="http://127.0.0.1:8000",
                    help="后端地址（穿透后传隧道地址，如 http://节点:端口）")
    ap.add_argument("--token", default=None,
                    help="DEBUG_INJECT_TOKEN（服务端已配置时必填）")
    ap.add_argument("--persist", action="store_true",
                    help="同步写入假池（后续轮询持续可见，慎用：可能重复触发决策）")
    ap.add_argument("--all", action="store_true",
                    help="剧情三连：storm → queue → traffic_jam")
    ap.add_argument("--interval", type=float, default=2.0,
                    help="--all 模式下事件间隔秒数（默认 2）")
    ap.add_argument("--place", default=None,
                    help="地点（queue/traffic_jam/hotel_full 需要）")
    ap.add_argument("scenario", nargs="?", choices=SCENARIOS + ("raw",),
                    help="预设场景或 raw")
    ap.add_argument("--event-type", default=None, help="raw 模式的事件类型")
    ap.add_argument("--data", default=None, help="raw 模式的 JSON 数据字符串")
    args = ap.parse_args()

    if args.all:
        payloads = [
            {"scenario": "storm"},
            {"scenario": "queue", "place": args.place or "故宫"},
            {"scenario": "traffic_jam", "place": args.place or "北京-故宫"},
        ]
        print(f"🎬 剧情注入（{len(payloads)} 个事件，间隔 {args.interval}s）：")
        for i, payload in enumerate(payloads, 1):
            print(f"\n[{i}/{len(payloads)}] {payload['scenario']}")
            if args.persist:
                payload["persist_world"] = True
            summarize(post(args.base, payload, args.token))
            if i < len(payloads):
                time.sleep(args.interval)
        return

    if args.scenario is None:
        ap.error("需要 scenario（或用 --all 剧情三连）")

    if args.scenario == "raw":
        if not args.event_type:
            ap.error("raw 模式需要 --event-type")
        data: Dict[str, Any] = {}
        if args.data:
            try:
                data = json.loads(args.data)
            except json.JSONDecodeError as exc:
                ap.error(f"--data 不是合法 JSON: {exc}")
        payload: Dict[str, Any] = {"event_type": args.event_type, "data": data}
        if args.place:
            payload["place"] = args.place
    else:
        payload = {"scenario": args.scenario}
        if args.place:
            payload["place"] = args.place
    if args.persist:
        payload["persist_world"] = True

    print(f"⚡ 注入 {args.scenario}: {json.dumps(payload, ensure_ascii=False)}")
    summarize(post(args.base, payload, args.token))


if __name__ == "__main__":
    main()
