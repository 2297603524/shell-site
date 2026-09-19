#!/usr/bin/env python3
"""抓取「标普500」与「纳斯达克100」历史行情 → data/us-history.json（供 VIX 页叠加对照线）

数据源:
    标普500   : CBOE 官方 _SPX 历史 JSON（与 VIX 同源、同口径，1975 年起）
    纳斯达克100: 国内纳指 ETF 513100（腾讯分段抓取，2013 年起）——美股指数本身只有当日快照，
                腾讯/东财均不提供连续历史，故用跟踪同一指数的境内 ETF 作为走势代理

输出（紧凑）: {"spx": [[日期, 收盘], ...], "ndx": [[日期, 收盘], ...], "src": {...}}
采样: 近 RECENT_DAYS 个交易日保留日线，更早按 5 个交易日 1 点（控制体积、保留完整跨度）

用法: python build_us_history.py [输出路径]   默认 data/us-history.json
"""
import json
import os
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

CBOE_SPX = "https://cdn.cboe.com/api/global/delayed_quotes/charts/historical/_SPX.json"
TX_RANGE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,800,qfq"
UA = "Mozilla/5.0 (compatible; site-data-bot)"
START = "1990-01-01"        # 与 VIX 历史起点对齐
NDX_CODE = "sh513100"       # 纳指100ETF（上交所）
NDX_FIRST_YEAR = 2013
RECENT_DAYS = 250


def _get(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def fetch_spx():
    d = _get(CBOE_SPX)
    out = []
    for x in (d.get("data") or []):
        dt, c = x.get("date"), x.get("close")
        if not dt or c is None:
            continue
        try:
            v = float(c)
        except (TypeError, ValueError):
            continue
        if dt >= START and v > 0:
            out.append((dt, v))
    out.sort()
    return out


def _tx_rows(code: str, start: str, end: str):
    d = ((_get(TX_RANGE % (code, start, end), 30).get("data")) or {}).get(code) or {}
    rows = d.get("qfqday") or d.get("day") or []
    out = []
    for r in rows:
        if len(r) < 3:
            continue
        try:
            out.append((r[0], float(r[2])))
        except (TypeError, ValueError):
            continue
    return out


def fetch_ndx():
    spans, y, ty = [], NDX_FIRST_YEAR, date.today().year
    while y <= ty:
        e = min(y + 2, ty)
        spans.append(("%d-01-01" % y, "%d-12-31" % e))
        y = e + 1
    merged = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        for rows in ex.map(lambda sp: _tx_rows(NDX_CODE, sp[0], sp[1]), spans):
            merged.extend(rows)
    dedup = {}
    for dt, c in merged:
        dedup[dt] = c
    return sorted(dedup.items())


def sample(series, recent: int = RECENT_DAYS):
    n = len(series)
    if n <= recent:
        return series
    older = series[:n - recent]
    out = older[::5]                       # 更早：每 5 个交易日取 1
    out.extend(series[n - recent:])        # 近期：保留日线
    return out


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "data", "us-history.json")

    out = {"src": {"spx": "CBOE 官方", "ndx": "纳指100ETF 513100（走势代理）"}}
    try:
        spx = fetch_spx()
        out["spx"] = [[d, round(c, 2)] for d, c in sample(spx)]
        print("标普500: %d 点（%s ~ %s）→ 采样后 %d 点" % (
            len(spx), spx[0][0], spx[-1][0], len(out["spx"])))
    except Exception as e:  # noqa: BLE001
        print("SPX 抓取失败: %s" % e)

    try:
        ndx = fetch_ndx()
        out["ndx"] = [[d, round(c, 3)] for d, c in sample(ndx)]
        print("纳斯达克100(513100): %d 点（%s ~ %s）→ 采样后 %d 点" % (
            len(ndx), ndx[0][0], ndx[-1][0], len(out["ndx"])))
    except Exception as e:  # noqa: BLE001
        print("NDX 抓取失败: %s" % e)

    if "spx" not in out and "ndx" not in out:
        print("两个都失败，保留旧文件")
        sys.exit(1)

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %.1f KB" % (dst, os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
