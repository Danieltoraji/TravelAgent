"""聚焦演示脚本：小景点排队激增（触发重规划）+ 小雨（未达阈值，不重规划）。

设计意图（替代多事件三连击，剧情更聚焦可控）：
  1. 只动"不必去"的小景点：注入排队激增 → 触发重规划 → 展示
     RePlanner 把该小景点移除/替换的 diff（大景点如故宫/慕田峪不动，
     核心行程不被大改）；
  2. 再注入一场小雨（降雨概率 < 60%）→ 展示"未达影响阈值，不重规划"，
     体现系统的影响判定克制力。

用法（先让 App 提交规划，确认行程里的小景点名字，再运行）：
    python demo/inject_focus.py --base http://39.96.89.133:8000 --place 景山公园

可选参数：
    --interval  排队 → 小雨 的间隔秒数（默认 15，留足 LLM 决策 + App 轮询）
    --queue-min 排队分钟数（默认 120，≥50 才触发）
    --rain      小雨降雨概率（默认 35，必须 <60 才不触发）
    --token     服务器 DEBUG_INJECT_TOKEN（若已配置）
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


def post(base: str, payload: Dict[str, Any], token: Optional[str]) -> Dict[str, Any]:
    url = base.rstrip("/") + "/api/debug/inject/"
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Debug-Token"] = token
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"  ✗ HTTP {exc.code}: {exc.read().decode('utf-8', errors='replace')}",
              file=sys.stderr)
        sys.exit(1)
    except urllib.error.URLError as exc:
        print(f"  ✗ 连接失败: {exc.reason}（确认 --base 指向运行中的 Django）",
              file=sys.stderr)
        sys.exit(1)


def show(resp: Dict[str, Any]) -> None:
    """打印注入结果。"""
    ev = resp.get("event") or {}
    decision = resp.get("decision")
    label = {
        "replanned": "✅ 已重规划（新时间轴已应用）",
        "recorded": "📋 达阈值但 A 侧判无需重规划",
        "hook_error": "⚠️ 决策引擎报错（检查服务器 DEEPSEEK_API_KEY）",
        "not_significant": "⏭ 未达影响阈值 → 不重规划（仅事件展示）",
    }.get(decision, decision or "?")
    print(f"  {label}  [{ev.get('event_type')}] {ev.get('place')}")
    if decision == "replanned":
        d = (resp.get("replan") or {}).get("decision") or {}
        print(f"      原因: {d.get('reason', '')}")
        for line in d.get("diff_summary", []):
            print(f"      • {line}")
    elif decision == "not_significant":
        data = ev.get("data") or {}
        print(f"      观测: {data}")


def main() -> None:
    ap = argparse.ArgumentParser(description="TravelAgent 聚焦演示：小景点排队 + 小雨")
    ap.add_argument("--base", default="http://127.0.0.1:8000", help="后端地址")
    ap.add_argument("--place", required=True,
                    help="小景点名称（必须在当前行程中，如 景山公园）")
    ap.add_argument("--interval", type=float, default=15.0,
                    help="排队 → 小雨 间隔秒数（默认 15）")
    ap.add_argument("--queue-min", type=int, default=120,
                    help="排队分钟数（默认 120，≥50 触发）")
    ap.add_argument("--rain", type=int, default=35,
                    help="小雨降雨概率（默认 35，必须 <60 才不触发）")
    ap.add_argument("--token", default=None, help="DEBUG_INJECT_TOKEN")
    args = ap.parse_args()

    if args.rain >= 60:
        ap.error(f"--rain 必须 < 60（否则会触发重规划，{args.rain} 不符合'小雨'剧情）")
    if args.queue_min < 50:
        ap.error(f"--queue-min 必须 ≥ 50（否则不触发决策，{args.queue_min} 无效）")

    print(f"🎬 剧情一：{args.place} 排队激增 {args.queue_min} 分钟（触发重规划）")
    show(post(args.base, {
        "event_type": "scenic", "place": args.place,
        "data": {"queue_min": args.queue_min},
    }, args.token))

    print(f"\n⏳ 等待 {args.interval}s（LLM 决策 + App 轮询）…")
    time.sleep(args.interval)

    print(f"🎬 剧情二：小雨（降雨概率 {args.rain}%，未达阈值 60%，不重规划）")
    show(post(args.base, {
        "event_type": "weather",
        "data": {"condition": "小雨", "rain_probability": args.rain, "uv_index": 2},
    }, args.token))

    print("\n📋 检查点：/api/events 应有两条事件（scenic + weather）；"
          "/api/replans 只在排队后 +1（小雨后不变）")


if __name__ == "__main__":
    main()
