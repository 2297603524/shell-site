#!/usr/bin/env python3
"""构建 A 股各主要指数的恐贪指数（自建模型）→ data/fear-greed.json

双数据源（免密钥，自动容错）:
    1) 东方财富日K  https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=<secid>&klt=101&fqt=1
    2) 腾讯日K（兜底）https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=<code>,day,,,420,qfq
    两源都取 [日期, 开, 收, 高, 低, 成交量]，口径一致

模型（对每个指数独立计算，5 个分项各取近 250 交易日滚动分位，等权平均 0~100）:
    1. 价格动量  收盘 vs MA125 偏离度           → 越高越贪婪
    2. 短期趋势  近 20 日涨跌幅                 → 越高越贪婪
    3. 均线广度  近 20 日"收盘>MA20"的天数占比   → 越高越贪婪
    4. 量能热度  成交量 vs MA60 偏离度           → 越高越贪婪
    5. 波动率    20 日年化波动率取反向分位       → 波动越大越恐惧

用法:
    python build_fear_greed.py [输出json路径]   默认 data/fear-greed.json
"""
import json
import math
import os
import sys
import time
import urllib.request

EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid=%s&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57"
            "&klt=101&fqt=1&lmt=420&end=20500101")
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,420,qfq"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

# (东财 secid, 腾讯 code, 显示名)
INDICES = [
    ("1.000001", "sh000001", "上证指数"),
    ("0.399001", "sz399001", "深证成指"),
    ("0.399006", "sz399006", "创业板指"),
    ("1.000300", "sh000300", "沪深300"),
    ("1.000905", "sh000905", "中证500"),
    ("1.000016", "sh000016", "上证50"),
    ("1.000688", "sh000688", "科创50"),
]

WINDOW = 250
KEEP_DAYS = 150


def _get(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
        "Connection": "close",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_em(secid: str):
    """东财 → (name, dates, closes, volumes)"""
    data = (_get(EM_KLINE % secid).get("data")) or {}
    dates, closes, vols = [], [], []
    for line in (data.get("klines") or []):
        f = line.split(",")
        if len(f) < 6:
            continue
        try:
            dates.append(f[0])
            closes.append(float(f[2]))   # 收盘
            vols.append(float(f[5]))     # 成交量
        except ValueError:
            continue
    if not dates:
        raise ValueError("empty klines")
    return data.get("name") or secid, dates, closes, vols


def fetch_tx(code: str):
    """腾讯兜底 → (dates, closes, volumes)"""
    d = ((_get(TX_KLINE % code, timeout=30).get("data")) or {}).get(code) or {}
    rows = d.get("qfqday") or d.get("day") or []
    dates, closes, vols = [], [], []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            dates.append(row[0])
            closes.append(float(row[2]))
            vols.append(float(row[5]))
        except ValueError:
            continue
    if not dates:
        raise ValueError("empty day rows")
    return dates, closes, vols


def pct_rank_series(series, window=WINDOW):
    out = []
    for i, v in enumerate(series):
        lo = max(0, i - window + 1)
        win = series[lo:i + 1]
        out.append(100.0 * sum(1 for x in win if x <= v) / len(win))
    return out


def rating_of(score):
    if score < 20:
        return "extreme fear", "极度恐惧"
    if score < 40:
        return "fear", "恐惧"
    if score < 60:
        return "neutral", "中性"
    if score < 80:
        return "greed", "贪婪"
    return "extreme greed", "极度贪婪"


def compute(closes, vols, dates):
    """按日计算 5 分项分位并合成恐贪值"""
    n = len(closes)
    if n < 200:
        return None

    mom, trend, breadth, vol_amt, vola, idx = [], [], [], [], [], []
    for i in range(125, n):
        ma125 = sum(closes[i - 124:i + 1]) / 125.0
        ma60v = sum(vols[i - 59:i + 1]) / 60.0
        if ma125 <= 0 or ma60v <= 0:
            continue

        mom.append(closes[i] / ma125 - 1.0)
        trend.append(closes[i] / closes[i - 20] - 1.0)

        cnt = 0
        for k in range(i - 19, i + 1):
            if closes[k] > sum(closes[k - 19:k + 1]) / 20.0:
                cnt += 1
        breadth.append(cnt / 20.0)

        vol_amt.append(vols[i] / ma60v - 1.0)

        rets = []
        for k in range(i - 19, i + 1):
            prev = closes[k - 1]
            if prev:
                rets.append(math.log(closes[k] / prev))
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        vola.append(math.sqrt(var) * math.sqrt(252.0))

        idx.append(i)

    if len(idx) < 30:
        return None

    ranks = [
        pct_rank_series(mom),
        pct_rank_series(trend),
        pct_rank_series(breadth),
        pct_rank_series(vol_amt),
        [100.0 - x for x in pct_rank_series(vola)],
    ]

    scores = [round(sum(r[j] for r in ranks) / len(ranks), 1) for j in range(len(idx))]
    last = len(idx) - 1
    rating, rating_cn = rating_of(scores[last])
    parts = {k: round(ranks[i][last], 1) for i, k in enumerate(
        ["momentum", "trend", "breadth", "volume", "volatility"])}
    points = [[dates[idx[j]], scores[j]] for j in range(max(0, len(idx) - KEEP_DAYS), len(idx))]
    return scores[last], rating, rating_cn, parts, points, dates[idx[last]]


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/fear-greed.json"
    out_indices, as_of = [], ""

    for em_id, tx_code, label in INDICES:
        got = None
        for attempt in (1, 2):
            try:
                name, dates, closes, vols = fetch_em(em_id)
                got = (name, dates, closes, vols)
                break
            except Exception:  # noqa: BLE001
                if attempt == 1:
                    time.sleep(1.5)
        if got is None:
            try:
                dates, closes, vols = fetch_tx(tx_code)
                got = (label, dates, closes, vols)
                print("   fallback → 腾讯源: %s" % label)
            except Exception as e:  # noqa: BLE001
                print("skip %s: %s" % (label, e))
                continue

        name, dates, closes, vols = got
        res = compute(closes, vols, dates)
        if not res:
            print("skip %s: not enough data" % name)
            continue
        score, rating, rating_cn, parts, points, last_date = res
        as_of = max(as_of, last_date)
        out_indices.append({
            "code": None,
            "name": name,
            "score": score,
            "rating": rating,
            "rating_cn": rating_cn,
            "parts": parts,
            "points": points,
        })
        print("ok  %-8s %6s  %s  分项=%s" % (name, score, rating_cn, parts))
        time.sleep(1.0)

    if not out_indices:
        print("all indices failed, keep old file")
        sys.exit(1)

    out = {
        "date": as_of,
        "source": "自建模型（各指数 5 分项分位合成）",
        "model": ("分项：价格动量(收盘vs MA125) / 短期趋势(20日涨跌) / 均线广度(20日内收于MA20上方占比) / "
                  "量能热度(成交量vs MA60) / 波动率(20日年化,反向)"),
        "indices": out_indices,
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %d 个指数, as_of=%s, %.1f KB" % (dst, len(out_indices), as_of, os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
