#!/usr/bin/env python3
"""构建 A 股各主要指数的恐贪指数（9 分项多维模型）→ data/fear-greed.json

数据源（全部免密钥）:
    指数日K:  东财 push2his.eastmoney.com  →  腾讯 web.ifzq.gtimg.cn（兜底）
    融资数据: 东财 datacenter RPTA_RZRQ_LSHJ（全市场融资买入额，每日）
    指数估值: 乐咕乐股 legulegu.com（月度 PE-TTM 历史；token = 当天日期 MD5，csrf 从页面取）

模型（9 个分项；价格/估值类随指数独立计算，市场类全市场共享）:
    1. 价格动量   收盘 vs MA125 偏离度              → 越高越贪婪
    2. 短期趋势   近 20 日涨跌幅                    → 越高越贪婪
    3. 均线广度   近 20 日"收盘>MA20"的天数占比      → 越高越贪婪
    4. 波动率     20 日年化波动率（反向分位）        → 波动越大越恐惧
    5. 量能热度   成交量 vs MA60 偏离度              → 越高越贪婪
    6. 融资热度   全市场融资买入额 vs MA20 偏离度     → 加杠杆越猛越贪婪
    7. 风险偏好   中证1000 与沪深300 近 60 日相对强弱  → 小盘越强越贪婪
    8. 回撤深度   收盘距近 250 交易日高点             → 越接近新高越贪婪
    9. 估值分位   指数 PE(TTM) 在近 60 个月中的分位   → 估值越贵越贪婪  ★

价格类分项取近 3 年（750 交易日）滚动分位；估值分项直接取月度 PE 分位。

用法:
    python build_fear_greed.py [输出json路径]   默认 data/fear-greed.json
"""
import hashlib
import http.cookiejar
import json
import math
import os
import re
import sys
import time
import urllib.request
from datetime import date

EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid=%s&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57"
            "&klt=101&fqt=1&lmt=%d&end=20500101")
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
DC_MARGIN = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
             "?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageNumber=%d&pageSize=500"
             "&sortColumns=DIM_DATE&sortTypes=-1&source=WEB&client=WEB")
LG_PAGE = "https://legulegu.com/stockdata/sz50-ttm-lyr"
LG_API = "https://legulegu.com/api/stockdata/index-basic-pe"
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
SMALL_CAP = ("1.000852", "sh000852")     # 中证1000
LARGE_CAP = ("1.000300", "sh000300")     # 沪深300

# 估值数据（乐咕支持的指数，取同风格代表）
VAL_MAP = {
    "上证指数": "000010.SH",   # 上证180（沪市大盘）
    "深证成指": "399330.SZ",   # 深证100（深市大盘）
    "创业板指": "399673.SZ",   # 创业板50
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "上证50": "000016.SH",
    "科创50": "399673.SZ",     # 创业板50（成长风格代理）
}

KLIMIT = 1500
WINDOW = 750         # 价格类分位窗口 = 近 3 年
VAL_WINDOW = 60      # 估值分位窗口 = 近 60 个月（5 年）
KEEP_DAYS = 150


def _get(url: str, timeout: int = 45, headers: dict = None) -> dict:
    h = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/", "Accept": "*/*", "Connection": "close"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
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
            return fetch_em(em_id)
        except Exception:  # noqa: BLE001
            if attempt == 1:
                time.sleep(1.5)
    dates, closes, vols = fetch_tx(tx_code)
    print("   fallback → 腾讯源: %s" % label)
    return label, dates, closes, vols


def fetch_margin():
    """全市场融资买入额（流量）→ {date: 值}"""
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
            v = r.get("RZMRE") or r.get("RZYE")
            if d and v:
                out[d] = float(v)
        time.sleep(0.6)
    print("   融资数据历史: %d 条" % len(out))
    return out


def fetch_valuation():
    """乐咕指数 PE-TTM（月度）→ {indexCode: {YYYY-MM: 分位(0~100)}}；失败返回 {}"""
    try:
        token = hashlib.md5(date.today().isoformat().encode("utf-8")).hexdigest()
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [("User-Agent", UA)]
        html = opener.open(LG_PAGE, timeout=40).read().decode("utf-8", "ignore")
        m = re.search(r'_csrf"\s+content="([^"]+)"', html)
        csrf = m.group(1) if m else ""

        out = {}
        for code in sorted(set(VAL_MAP.values())):
            try:
                req = urllib.request.Request(
                    "%s?token=%s&indexCode=%s" % (LG_API, token, code),
                    headers={"User-Agent": UA, "X-CSRF-TOKEN": csrf, "Referer": LG_PAGE})
                rows = (json.loads(opener.open(req, timeout=40).read()) or {}).get("data") or []
                series = [(str(r.get("date"))[:7], float(r["ttmPe"]))
                          for r in rows if r.get("date") and r.get("ttmPe")]
                if not series:
                    print("   valuation empty: %s" % code)
                    continue
                q = {}
                for i, (mo, v) in enumerate(series):
                    lo = max(0, i - VAL_WINDOW + 1)
                    win = [x[1] for x in series[lo:i + 1]]
                    q[mo] = round(100.0 * sum(1 for x in win if x <= v) / len(win), 1)
                out[code] = q
                print("   估值 %-11s %d 个月（最新 %s = %s 分位）" % (code, len(series), series[-1][0], q[series[-1][0]]))
            except Exception as e:  # noqa: BLE001
                print("   valuation %s failed: %s" % (code, e))
            time.sleep(0.8)
        return out
    except Exception as e:  # noqa: BLE001
        print("   估值数据源不可用（该分项将跳过）: %s" % e)
        return {}


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
    out, last = [], None
    for d in dates:
        v = value_map.get(d)
        if v is not None:
            last = v
        out.append(last)
    return out


def compute(dates, closes, vols, margin_by_date, small, large, val_by_month):
    n = len(closes)
    if n < 300:
        return None

    m_ser = align_map(dates, margin_by_date)
    s_ser = align_map(dates, small)
    l_ser = align_map(dates, large)

    mom, trend, breadth, vol_amt, vola, margin_dev, risk_pref, drawdown, valuation, idx = \
        [], [], [], [], [], [], [], [], [], []
    has_val = bool(val_by_month)

    for i in range(125, n):
        ma125 = sum(closes[i - 124:i + 1]) / 125.0
        ma60v = sum(vols[i - 59:i + 1]) / 60.0
        if ma125 <= 0 or ma60v <= 0:
            continue

        # 估值分项（月度 PE 分位，已是 0~100）
        if has_val:
            vq = val_by_month.get(dates[i][:7])
            if vq is None:
                continue
            valuation.append(vq)

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

        if m_ser[i] is None or m_ser[i - 19] is None or m_ser[i] <= 0:
            continue
        margin_dev.append(m_ser[i] / (sum(m_ser[i - 19:i + 1]) / 20.0) - 1.0)

        if s_ser[i] is None or s_ser[i - 60] is None or l_ser[i] is None or l_ser[i - 60] is None:
            continue
        risk_pref.append((s_ser[i] / s_ser[i - 60] - 1.0) - (l_ser[i] / l_ser[i - 60] - 1.0))

        hi250 = max(closes[max(0, i - 249):i + 1])
        drawdown.append(closes[i] / hi250 - 1.0)

        idx.append(i)

    if len(idx) < 60:
        return None

    ranks = [
        pct_rank_series(mom),
        pct_rank_series(trend),
        pct_rank_series(breadth),
        [100.0 - x for x in pct_rank_series(vola)],
        pct_rank_series(vol_amt),
        pct_rank_series(margin_dev),
        pct_rank_series(risk_pref),
        pct_rank_series(drawdown),
    ]
    part_names = ["momentum", "trend", "breadth", "volatility", "volume", "margin", "riskon", "drawdown"]
    if has_val and len(valuation) == len(idx):
        ranks.append(valuation)          # 已是分位，直接用
        part_names.append("valuation")

    scores = [round(sum(r[j] for r in ranks) / len(ranks), 1) for j in range(len(idx))]
    last = len(idx) - 1
    rating, rating_cn = rating_of(scores[last])
    parts = {k: round(ranks[i][last], 1) for i, k in enumerate(part_names)}
    points = [[dates[idx[j]], scores[j], round(closes[idx[j]], 2)]
              for j in range(max(0, len(idx) - KEEP_DAYS), len(idx))]
    return scores[last], rating, rating_cn, parts, points, dates[idx[last]]


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/fear-greed.json"

    margin_map = fetch_margin()
    print("   抓取估值数据（乐咕乐股）…")
    val_raw = fetch_valuation()

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

        vcode = VAL_MAP.get(name) or VAL_MAP.get(label)
        val_by_month = val_raw.get(vcode) if vcode else None
        res = compute(dates, closes, vols, margin_map, small_map, large_map, val_by_month)
        if not res:
            print("skip %s: not enough data" % name)
            continue
        score, rating, rating_cn, parts, points, last_date = res
        as_of = max(as_of, last_date)
        out_indices.append({
            "name": name, "score": score, "rating": rating, "rating_cn": rating_cn,
            "parts": parts, "points": points,
            "valIndex": vcode or "",
        })
        print("ok  %-8s %6s  %s  分项=%s" % (name, score, rating_cn, parts))
        time.sleep(1.0)

    if not out_indices:
        print("all indices failed, keep old file")
        sys.exit(1)

    n_parts = max(len(it["parts"]) for it in out_indices)
    out = {
        "date": as_of,
        "source": "自建多维模型（%d 分项分位合成）" % n_parts,
        "model": ("价格动量 / 短期趋势 / 均线广度 / 波动率(反向) / 量能热度 / 融资热度 / "
                  "风险偏好 / 回撤深度 / 估值分位(PE-TTM 近 60 个月)"),
        "window": "价格类取近 3 年（750 交易日）滚动分位；估值取近 60 个月 PE 分位",
        "indices": out_indices,
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %d 个指数, %d 分项, as_of=%s, %.1f KB" % (
        dst, len(out_indices), n_parts, as_of, os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
