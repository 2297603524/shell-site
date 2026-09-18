#!/usr/bin/env python3
"""从 CBOE VIX_History.csv 生成站点用的完整历史数据 JSON（自 1990-01-02 指数发布起）。

用法:
    python build_vix_history.py [csv路径] [输出json路径]

默认: /tmp/vixhist.csv -> data/vix-history.json

输出结构（紧凑，控体积）:
    {
      "updated": "...", "count": 9276, "start": "1990-01-02", "end": "2026-09-17",
      "points": [["1990-01-02", 17.24], ...],       # 全量 [日期, 收盘]
      "stats": { ... 最近 120 日统计（兼容旧前端） }
    }
"""
import csv
import json
import os
import sys
from datetime import datetime

RECENT = 120


def main() -> None:
    src = sys.argv[1] if len(sys.argv) > 1 else "/tmp/vixhist.csv"
    dst = sys.argv[2] if len(sys.argv) > 2 else "data/vix-history.json"

    rows = []
    with open(src, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                d = datetime.strptime(row["DATE"].strip(), "%m/%d/%Y").strftime("%Y-%m-%d")
                rows.append([d, round(float(row["CLOSE"]), 2)])
            except (ValueError, KeyError, TypeError):
                continue

    if not rows:
        print("no valid rows, abort")
        sys.exit(1)

    # 去重 + 排序（同一日期只留一条）
    dedup = {}
    for d, c in rows:
        dedup[d] = c
    points = sorted(dedup.items())

    recent = points[-RECENT:]
    closes = [c for _, c in recent]
    cur = closes[-1]
    hi_d, hi_v = max(recent, key=lambda p: p[1])
    lo_d, lo_v = min(recent, key=lambda p: p[1])

    out = {
        # 不写入抓取时间戳：历史为日频数据，只有真正新增交易日时才应产生提交，
        # 否则每 5 分钟一次的 cron 会产出大量无意义 commit
        "count": len(points),
        "start": points[0][0],
        "end": points[-1][0],
        "source": "CBOE VIX_History.csv",
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
