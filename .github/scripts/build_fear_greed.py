#!/usr/bin/env python3
"""构建 A 股恐贪指数（自建模型）→ data/fear-greed.json

数据源（免密钥）:
    东方财富 上证指数日K（含成交额）
    https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.000001&klt=101&fqt=1
    返回 klines 每行: 日期,开,收,高,低,成交量(手),成交额(元)

模型（参考公开的 A 股恐贪方法学，5 个分项各自取近 250 交易日滚动分位，等权平均 0~100）:
    1. 价格动量  收盘 vs MA125 偏离度          → 越高越贪婪
    2. 短期趋势  近 20 日涨跌幅                → 越高越贪婪
    3. 均线广度  近 20 日"收盘>MA20"的天数占比  → 越高越贪婪
    4. 成交热度  成交额 vs MA60 偏离度          → 越高越贪婪
    5. 波动率    20 日年化波动率取反向分位      → 波动越大越恐惧

用法:
    python build_fear_greed.py [输出json路径]   默认 data/fear-greed.json
"""
import json
import math
import os
import sys
import urllib.request

URL = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
       "?secid=1.000001&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57"
       "&klt=101&fqt=1&lmt=420&end=20500101")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

WINDOW = 250      # 分位窗口（交易日）
KEEP_DAYS = 150   # 输出保留的历史天数


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"})
    with urllib.request.urlopen(req, timeout=45) as resp:
        return json.load(resp)


def pct_rank_series(series, window=WINDOW):
    """对序列每个元素，返回其在「截至自身的近 window 个样本」中的分位（0~100）"""
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


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/fear-greed.json"
    raw = fetch(URL)
    klines = ((raw.get("data") or {}).get("klines")) or []
    name = ((raw.get("data") or {}).get("name")) or "上证指数"

    dates, closes, amounts = [], [], []
    for line in klines:
        f = line.split(",")
        if len(f) < 7:
            continue
        try:
            dates.append(f[0])
            closes.append(float(f[2]))     # 收盘
            amounts.append(float(f[6]))    # 成交额
        except ValueError:
            continue

    n = len(closes)
    if n < 200:
        print("not enough klines (%d), keep old file" % n)
        sys.exit(1)

    # 逐日计算 5 个分项的原始值（前 124 天因缺 MA125 跳过）
    mom, trend, breadth, vol_amt, vola, idx = [], [], [], [], [], []
    for i in range(n):
        if i < 125:
            continue
        ma125 = sum(closes[i - 124:i + 1]) / 125.0
        ma20 = sum(closes[i - 19:i + 1]) / 20.0
        ma60a = sum(amounts[i - 59:i + 1]) / 60.0
        if ma125 <= 0 or ma60a <= 0:
            continue

        mom.append(closes[i] / ma125 - 1.0)
        trend.append(closes[i] / closes[i - 20] - 1.0)

        cnt = 0
        for k in range(i - 19, i + 1):
            m20 = sum(closes[k - 19:k + 1]) / 20.0
            if closes[k] > m20:
                cnt += 1
        breadth.append(cnt / 20.0)

        vol_amt.append(amounts[i] / ma60a - 1.0)

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
        print("not enough samples, keep old file")
        sys.exit(1)

    p_mom = pct_rank_series(mom)
    p_trend = pct_rank_series(trend)
    p_breadth = pct_rank_series(breadth)
    p_amt = pct_rank_series(vol_amt)
    p_vola = [100.0 - x for x in pct_rank_series(vola)]   # 波动率反向

    scores, parts = [], []
    for j in range(len(idx)):
        vals = [p_mom[j], p_trend[j], p_breadth[j], p_amt[j], p_vola[j]]
        s = round(sum(vals) / len(vals), 1)
        scores.append(s)
        parts.append([round(v, 1) for v in vals])

    last = len(idx) - 1
    rating, rating_cn = rating_of(scores[last])

    points = [[dates[idx[j]], scores[j]] for j in range(max(0, len(idx) - KEEP_DAYS), len(idx))]

    out = {
        "score": scores[last],
        "rating": rating,
        "rating_cn": rating_cn,
        "date": dates[idx[last]],
        "index_name": name,
        "parts": {
            "momentum": parts[last][0],
            "trend": parts[last][1],
            "breadth": parts[last][2],
            "volume": parts[last][3],
            "volatility": parts[last][4],
        },
        "points": points,
        "source": "自建模型（上证指数 5 分项分位）",
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %s -> %s (%s), %d 天历史, 分项=%s" % (
        dst, out["date"], out["score"], rating_cn, len(points), out["parts"]))


if __name__ == "__main__":
    main()
