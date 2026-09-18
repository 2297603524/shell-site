#!/usr/bin/env python3
"""从 CBOE 官方 JSON 接口生成站点用的 VIX 全量日线数据（自 1990-01-02 起）。

数据源:
    https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json
    （全量 OHLC，比 VIX_History.csv 更易解析；字段为字符串）

用法:
    python build_vix_history.py [输出json路径]   默认 data/vix-history.json

输出结构:
    {
      "count": 9276, "start": "1990-01-02", "end": "2026-09-17",
      "source": "CBOE charts/historical",
      "points": [["1990-01-02", 17.24, 17.24, 17.24, 17.24], ...],   # [日期, 收, 开, 高, 低]
      "stats": { ... 最近 120 日统计 }
    }
    不写抓取时间戳：只有真正新增交易日时才产生文件差异
"""
import csv
import json
import os
import sys
import urllib.request

URL = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_VIX.json"
RECENT = 120


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (site-data-bot)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def num(v) -> float:
    try:
        return round(float(v), 2)
    except (TypeError, ValueError):
        return 0.0


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/vix-history.json"
    raw = fetch(URL)
    rows = raw.get("data") or []

    dedup = {}
    for r in rows:
        d = r.get("date")
        if not d:
            continue
        dedup[d] = [d, num(r.get("close")), num(r.get("open")), num(r.get("high")), num(r.get("low"))]

    if not dedup:
        print("no valid rows from CBOE, keep old file")
        sys.exit(1)

    points = [dedup[k] for k in sorted(dedup)]
    closes = [p[1] for p in points[-RECENT:]]
    cur = closes[-1]
    recent = points[-RECENT:]
    hi_d, hi_v = max(((p[0], p[1]) for p in recent), key=lambda x: x[1])
    lo_d, lo_v = min(((p[0], p[1]) for p in recent), key=lambda x: x[1])

    out = {
        "count": len(points),
        "start": points[0][0],
        "end": points[-1][0],
        "source": "CBOE charts/historical",
        "points": points,
        "stats": {
            "current": cur,
            "high": {"v": hi_v, "d": hi_d},
            "low": {"v": lo_v, "d": lo_d},
            "avg": round(sum(closes) / len(closes), 2),
            "percentile": round(sum(1 for c in closes if c <= cur) / len(closes) * 100),
        },
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(dst) / 1024
    print("wrote %s: %d points (%s ~ %s), %.1f KB" % (dst, len(points), points[0][0], points[-1][0], size_kb))


if __name__ == "__main__":
    main()
