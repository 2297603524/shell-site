#!/usr/bin/env python3
"""构建 A 股各主要指数的恐贪指数（多维模型）→ data/fear-greed.json

数据源（免密钥、双源容错）:
    指数日K:  东财 push2his.eastmoney.com  →  腾讯 web.ifzq.gtimg.cn（兜底）
    融资余额: 东财 datacenter RPTA_RZRQ_LSHJ（沪深两市合计，每日）

模型（7 个分项；价格类分项对每个指数独立计算，市场类分项全市场共享）:
    1. 价格动量   收盘 vs MA125 偏离度            → 越高越贪婪
    2. 短期趋势   近 20 日涨跌幅                  → 越高越贪婪
    3. 均线广度   近 20 日"收盘>MA20"的天数占比    → 越高越贪婪
    4. 波动率     20 日年化波动率（反向分位）      → 波动越大越恐惧
    5. 量能热度   成交量 vs MA60 偏离度            → 越高越贪婪
    6. 杠杆情绪   全市场融资余额 vs MA20 偏离度     → 加杠杆越猛越贪婪  ★
    7. 风险偏好   中证1000 与沪深300 近 60 日相对强弱 → 小盘越强越贪婪  ★

每个分项取近 WINDOW(750 ≈ 3 年) 个交易日的滚动分位（0~100），等权平均。

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
            "&klt=101&fqt=1&lmt=%d&end=20500101")
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
DC_MARGIN = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
             "?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageNumber=%d&pageSize=500"
             "&sortColumns=DIM_DATE&sortTypes=-1&source=WEB&client=WEB")
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
SMALL_CAP = ("1.000852", "sh000852")     # 中证1000（风险偏好分项）
LARGE_CAP = ("1.000300", "sh000300")     # 沪深300（风险偏好分项）

KLIMIT = 1500        # 抓取日K条数（需覆盖 750 分位窗口 + 前置均线）
WINDOW = 750         # 分位窗口 = 近 3 年
KEEP_DAYS = 150      # 输出保留的历史天数


def _get(url: str, timeout: int = 45) -> dict:
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Referer": "https://quote.eastmoney.com/",
        "Accept": "*/*",
        "Connection": "close",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def fetch_em(secid: str, limit: int = KLIMIT):
    data = (_get(EM_KLINE % (secid, limit)).get("data")) or {}
    dates, closes, vols = [], [], []
    for line in (data.get("klines") or []):
        f = line.split(",")
        if len(f) < 6:
            continue
        try:
            dates.append(f[0])
            closes.append(float(f[2]))
            vols.append(float(f[5]))
        except ValueError:
            continue
    if not dates:
        raise ValueError("empty klines")
    return data.get("name") or secid, dates, closes, vols


def fetch_tx(code: str, limit: int = KLIMIT):
    d = ((_get(TX_KLINE % (code, limit), timeout=30).get("data")) or {}).get(code) or {}
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


def get_kline(label: str, em_id: str, tx_code: str):
    for attempt in (1, 2):
        try:
            name, dates, closes, vols = fetch_em(em_id)
            return name, dates, closes, vols
        except Exception:  # noqa: BLE001
            if attempt == 1:
                time.sleep(1.5)
    try:
        dates, closes, vols = fetch_tx(tx_code)
        print("   fallback → 腾讯源: %s" % label)
        return label, dates, closes, vols
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("both sources failed: %s" % e)


def fetch_margin():
    """全市场融资余额（沪深合计）→ {date: 融资余额}"""
    out = {}
    for page in (1, 2, 3):
        try:
            res = _get(DC_MARGIN % page).get("result") or {}
            rows = res.get("data") or []
        except Exception as e:  # noqa: BLE001
            print("   margin page %d failed: %s" % (page, e))
            break
        if not rows:
            break
        for r in rows:
            d = str(r.get("DIM_DATE") or "")[:10]
            v = r.get("RZYE")
            if d and v:
                out[d] = float(v)
        time.sleep(0.6)
    print("   融资余额历史: %d 条" % len(out))
    return out


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


def align_map(dates, value_map):
    """按给定交易日序列取映射值（缺失则沿用前值）"""
    out, last = [], None
    for d in dates:
        v = value_map.get(d)
        if v is not None:
            last = v
        out.append(last)
    return out


def compute(dates, closes, vols, margin_by_date, small, large):
    n = len(closes)
    if n < 300:
        return None

    mom, trend, breadth, vol_amt, vola, margin_dev, risk_pref, idx = [], [], [], [], [], [], [], []

    m_ser = align_map(dates, margin_by_date)
    s_ser = align_map(dates, small)
    l_ser = align_map(dates, large)

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

        # 杠杆情绪：融资余额 vs 20 日均值
        if m_ser[i] is None or m_ser[i - 19] is None or m_ser[i] <= 0:
            continue
        m_ma20 = sum(m_ser[i - 19:i + 1]) / 20.0
        margin_dev.append(m_ser[i] / m_ma20 - 1.0)

        # 风险偏好：中证1000 与沪深300 近 60 日涨幅差
        if s_ser[i] is None or s_ser[i - 60] is None or l_ser[i] is None or l_ser[i - 60] is None:
            continue
        risk_pref.append((s_ser[i] / s_ser[i - 60] - 1.0) - (l_ser[i] / l_ser[i - 60] - 1.0))

        idx.append(i)

    if len(idx) < 60:
        return None

    ranks = [
        pct_rank_series(mom),
        pct_rank_series(trend),
        pct_rank_series(breadth),
        [100.0 - x for x in pct_rank_series(vola)],     # 波动率反向
        pct_rank_series(vol_amt),
        pct_rank_series(margin_dev),
        pct_rank_series(risk_pref),
    ]

    scores = [round(sum(r[j] for r in ranks) / len(ranks), 1) for j in range(len(idx))]
    last = len(idx) - 1
    rating, rating_cn = rating_of(scores[last])
    parts = {k: round(ranks[i][last], 1) for i, k in enumerate(
        ["momentum", "trend", "breadth", "volatility", "volume", "margin", "riskon"])}
    points = [[dates[idx[j]], scores[j]] for j in range(max(0, len(idx) - KEEP_DAYS), len(idx))]
    return scores[last], rating, rating_cn, parts, points, dates[idx[last]]


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/fear-greed.json"

    margin_map = fetch_margin()

    print("   抓取中证1000 / 沪深300（风险偏好分项）…")
    _, s_dates, s_closes, _ = get_kline("中证1000", SMALL_CAP[0], SMALL_CAP[1])
    _, l_dates, l_closes, _ = get_kline("沪深300", LARGE_CAP[0], LARGE_CAP[1])
    small_map = dict(zip(s_dates, s_closes))
    large_map = dict(zip(l_dates, l_closes))

    out_indices, as_of = [], ""
    for em_id, tx_code, label in INDICES:
        try:
            name, dates, closes, vols = get_kline(label, em_id, tx_code)
        except Exception as e:  # noqa: BLE001
            print("skip %s: %s" % (label, e))
            continue
        res = compute(dates, closes, vols, margin_map, small_map, large_map)
        if not res:
            print("skip %s: not enough data" % name)
            continue
        score, rating, rating_cn, parts, points, last_date = res
        as_of = max(as_of, last_date)
        out_indices.append({
            "name": name, "score": score, "rating": rating, "rating_cn": rating_cn,
            "parts": parts, "points": points,
        })
        print("ok  %-8s %6s  %s  分项=%s" % (name, score, rating_cn, parts))
        time.sleep(1.0)

    if not out_indices:
        print("all indices failed, keep old file")
        sys.exit(1)

    out = {
        "date": as_of,
        "source": "自建多维模型（7 分项分位合成）",
        "model": ("分项：价格动量(收盘vs MA125) / 短期趋势(20日涨跌) / 均线广度(20日内收于MA20上方占比) / "
                  "波动率(20日年化,反向) / 量能热度(成交量vs MA60) / 杠杆情绪(全市场融资余额vs MA20) / "
                  "风险偏好(中证1000与沪深300 60日相对强弱)"),
        "window": "近 3 年（750 个交易日）滚动分位",
        "indices": out_indices,
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %d 个指数, as_of=%s, %.1f KB" % (dst, len(out_indices), as_of, os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
