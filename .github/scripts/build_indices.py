#!/usr/bin/env python3
"""抓取 CBOE 官方延迟行情，生成站点用的多指标快照 data/indices.json。

用法:
    python build_indices.py [输出json路径]

特点:
    - 单一数据源（CBOE cdn.cboe.com），免密钥
    - 任一 symbol 失败只跳过该项，不影响整体输出
    - 不写抓取时间戳，行情未变时文件内容不变（避免 cron 产生无意义提交）
"""
import json
import os
import sys
import urllib.request

BASE = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/%s.json"

# (CBOE 代码, 站点 key, 显示名, 说明)
SYMBOLS = [
    ("_VIX",   "vix",   "VIX 恐慌指数",  "标普500 30天隐含波动率"),
    ("_VVIX",  "vvix",  "VVIX",          "VIX 的波动率（恐慌的恐慌）"),
    ("_VIX9D", "vix9d", "VIX9D",         "9 天短期波动率"),
    ("_VIX3M", "vix3m", "VIX3M",         "3 个月中长期波动率"),
    ("_SPX",   "spx",   "标普500",       "SPX 大盘指数"),
    ("_NDX",   "ndx",   "纳斯达克100",   "NDX 科技股指数"),
    ("_RUT",   "rut",   "罗素2000",      "RUT 小盘股指数"),
]


def fetch(sym: str) -> dict:
    req = urllib.request.Request(BASE % sym, headers={"User-Agent": "Mozilla/5.0 (site-data-bot)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/indices.json"
    out = {}
    for sym, key, name, desc in SYMBOLS:
        try:
            d = fetch(sym)["data"]
            out[key] = {
                "name": name,
                "desc": desc,
                "symbol": d.get("symbol"),
                "price": d.get("current_price"),
                "change": d.get("price_change"),
                "pct": d.get("price_change_percent"),
                "open": d.get("open"),
                "high": d.get("high"),
                "low": d.get("low"),
                "prev": d.get("prev_day_close"),
                "iv30": d.get("iv30"),
                "time": d.get("last_trade_time"),
            }
            print("ok  %-7s %-14s %12s %7s%%" % (sym, name, d.get("current_price"), round(d.get("price_change_percent") or 0, 2)))
        except Exception as e:  # noqa: BLE001
            print("skip %s: %s" % (sym, e))

    if not out:
        print("全部指标抓取失败，保留旧文件")
        sys.exit(1)

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    # newline="\n" 固定为 LF：indent=0 也是多行输出，Windows 上默认 CRLF 会与
    # Linux runner 的 LF 产出产生纯行尾差异，导致定时任务每次都被判为"有变化"
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"), indent=0)
    print("wrote %s: %d 个指标" % (dst, len(out)))


if __name__ == "__main__":
    main()
