#!/usr/bin/env python3
"""抓取 CBOE 官方 VIX 实时报价 → data/vix.json

数据源:
    https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json （免密钥，延迟行情）

两个必须守住的点（都踩过）：
    1. CBOE 返回的 prev_day_close 在盘后已等于当日收盘价，直接当「昨收」用会得到
       「昨收 == 当前价」的荒谬结果，并让振幅偏大。这里统一用 current_price - price_change
       反推真实昨收；round 到 4 位是必须的，否则会写进 15.440000000000001 这种浮点尾巴。
    2. 不写抓取时间戳。本脚本由每 5 分钟的高频任务调用，一旦写入 fetched_at，
       数据没变也会产生 diff → 每天 288 次无意义提交，把仓库历史彻底埋掉。
       判断数据是否更新，请看 last_trade_time（数据自身的最后成交时间）。

用法:
    python build_vix_quote.py [输出json路径]   默认 data/vix.json
    抓取失败时以非 0 退出并保留旧文件，调用方应忽略该错误继续后续步骤。
"""
import json
import os
import sys
import urllib.request

URL = "https://cdn.cboe.com/api/global/delayed_quotes/quotes/_VIX.json"


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (site-data-bot)"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/vix.json"

    try:
        raw = fetch(URL)
    except Exception as e:  # noqa: BLE001
        print("CBOE quote fetch failed: %s, keep old file" % e)
        sys.exit(1)

    d = raw.get("data") or {}
    price = d.get("current_price")
    if price is None:
        print("no current_price in CBOE response, keep old file")
        sys.exit(1)

    chg = d.get("price_change")
    prev = d.get("prev_day_close")
    try:
        if chg is not None:
            prev = round(float(price) - float(chg), 4)
    except (TypeError, ValueError):
        pass

    out = {
        "price": price,
        "change": chg,
        "change_percent": d.get("price_change_percent"),
        "open": d.get("open"),
        "high": d.get("high"),
        "low": d.get("low"),
        "prev_close": prev,
        "iv30": d.get("iv30"),
        "last_trade_time": d.get("last_trade_time"),
        "source": "CBOE",
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    # newline="\n" 是必须的：这里用了 indent=2 的多行输出，Windows 上默认会写成 CRLF，
    # 与 Linux runner 产出的 LF 版本字节不同 → 每次定时任务都会产生一次"全文件重写"的空提交
    with open(dst, "w", encoding="utf-8", newline="\n") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)

    print("wrote %s" % dst)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
