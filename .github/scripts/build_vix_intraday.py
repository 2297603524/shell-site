#!/usr/bin/env python3
"""从 CBOE 官方分时接口生成 VIX 分时数据（保留最近 2 个交易日）。

数据源:
    https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/_VIX.json
    每分钟一条（美股 09:31~16:00 共约 390 条），价格在 data[].price.close

用法:
    python build_vix_intraday.py [输出json路径]   默认 data/vix-intraday.json

输出结构:
    {
      "source": "CBOE charts/intraday",
      "sessions": [
        {"date": "2026-09-17", "points": [["09:32", 15.43], ...]},   # 最新一日在最后
        {"date": "2026-09-16", "points": [...]}
      ]
    }
    不写抓取时间戳：盘后数据不变时文件无差异，避免 cron 制造无意义提交
"""
import json
import os
import sys
import urllib.request

URL = "https://cdn.cboe.com/api/global/delayed_quotes/charts/intraday/_VIX.json"
KEEP_SESSIONS = 2


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (site-data-bot)"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/vix-intraday.json"
    raw = fetch(URL)
    rows = raw.get("data") or []

    sessions = {}   # date -> [[hh:mm, price], ...]
    order = []      # 保持日期顺序
    for r in rows:
        dt = r.get("datetime") or ""
        if "T" not in dt:
            continue
        date, hhmm = dt.split("T")[0], dt.split("T")[1][:5]
        try:
            price = round(float(r["price"]["close"]), 2)
        except (KeyError, TypeError, ValueError):
            continue
        if price <= 0:
            continue
        if date not in sessions:
            sessions[date] = []
            order.append(date)
        sessions[date].append([hhmm, price])

    if not sessions:
        print("no valid intraday rows, keep old file")
        sys.exit(1)

    keep = order[-KEEP_SESSIONS:]
    out = {
        "source": "CBOE charts/intraday",
        "sessions": [{"date": d, "points": sessions[d]} for d in keep],
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))

    size_kb = os.path.getsize(dst) / 1024
    print("wrote %s: %d sessions (%s) latest=%s %d points, %.1f KB" % (
        dst, len(keep), ",".join(keep), keep[-1], len(sessions[keep[-1]]), size_kb))


if __name__ == "__main__":
    main()
