#!/usr/bin/env python3
"""从 CBOE VIX_History.csv 生成站点用的近 N 日历史数据 JSON。

用法:
    python build_vix_history.py [csv路径] [输出json路径]

默认: /tmp/vixhist.csv -> data/vix-history.json
"""
import csv
import json
import os
import sys
from datetime import datetime, timezone

DAYS = 120


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/vixhist.csv"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/vix-history.json"

    rows = []
    with open(src, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                d = datetime.strptime(row["DATE"].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
                rows.append({
                    "d": d,
                    "o": round(float(row["OPEN"]), 2),
                    "h": round(float(row["HIGH"]), 2),
                    "l": round(float(row["LOW"]), 2),
                    "c": round(float(row["CLOSE"]), 2),
                })
            except (ValueError, KeyError, TypeError):
                continue

    if not rows:
        print("no valid rows, abort")
        sys.exit(1)

    points = rows[-DAYS:]
    closes = [p["c"] for p in points]
    cur = closes[-1]
    hi = max(points, key=lambda p: p["c"])
    lo = min(points, key=lambda p: p["c"])
    avg = round(sum(closes) / len(closes), 2)
    pctile = round(sum(1 for c in closes if c <= cur) / len(closes) * 100)

    out = {
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "days": len(points),
        "points": points,
        "stats": {
            "current": cur,
            "high": {"v": hi["c"], "d": hi["d"]},
            "low": {"v": lo["c"], "d": lo["d"]},
            "avg": avg,
            "percentile": pctile,
        },
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %d points, latest %s close=%s" % (dst, len(points), points[-1]["d"], cur))


if __name__ == "__main__":
    main()
