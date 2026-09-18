#!/usr/bin/env python3
"""构建 A 股各主要指数的恐贪指数（9 分项多维模型，覆盖指数成立以来全历史）→ data/fear-greed.json

数据源（全部免密钥）:
    指数日K:  东财 push2his.eastmoney.com  →  腾讯 web.ifzq.gtimg.cn（兜底），尽量取全历史
    融资数据: 东财 datacenter RPTA_RZRQ_LSHJ（全市场融资买入额，2010-03 融资融券业务启动以来）
    指数估值: 乐咕乐股 legulegu.com（月度 PE-TTM 历史，部分自 2005 年起）

模型（9 个分项）:
    ① 价格动量  收盘 vs MA125 偏离      ② 短期趋势  近 20 日涨跌
    ③ 均线广度  20 日内收盘>MA20 占比   ④ 波动率    20 日年化（反向）
    ⑤ 量能热度  成交量 vs MA60          ⑥ 融资热度  全市场融资买入额 vs MA20
    ⑦ 风险偏好  中证1000 vs 沪深300    ⑧ 回撤深度  距 250 日高点
    ⑨ 估值分位  指数 PE-TTM 近 60 个月分位
    ⚠️ 早期（如融资数据 2010 年前、估值数据缺失期）自动降级：该日只用可用分项等权平均。
       每个交易日的实际分项数记录在 "n" 字段中（3=价格类基础，完整期为 9）

输出采样（控制体积同时保留完整时间跨度）:
    近 RECENT_DAYS(250) 个交易日 → 全部日线点
    更早 → 每月最后一个交易日
    points: [[日期, 恐贪值, 收盘点位, 当日分项数, 粒度], ...]   粒度: "d"=日线, "m"=月线

用法:
    python build_fear_greed.py [输出json路径]   默认 data/fear-greed.json
"""
import bisect
import hashlib
import http.cookiejar
import json
import math
import os
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date

EM_KLINE = ("https://push2his.eastmoney.com/api/qt/stock/kline/get"
            "?secid=%s&fields1=f1,f2,f3,f4,f5&fields2=f51,f52,f53,f54,f55,f56,f57"
            "&klt=101&fqt=1&lmt=%d&end=20500101")
TX_KLINE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,,,%d,qfq"
TX_KLINE_RANGE = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=%s,day,%s,%s,800,qfq"
DC_MARGIN = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
             "?reportName=RPTA_RZRQ_LSHJ&columns=ALL&pageNumber=%d&pageSize=500"
             "&sortColumns=DIM_DATE&sortTypes=-1&source=WEB&client=WEB")
LG_PAGE = "https://legulegu.com/stockdata/sz50-ttm-lyr"
LG_API = "https://legulegu.com/api/stockdata/index-basic-pe"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

# (东财 secid, 腾讯 code, 显示名, 起始年份)
INDICES = [
    ("1.000001", "sh000001", "上证指数", 1990),
    ("0.399001", "sz399001", "深证成指", 1991),
    ("0.399006", "sz399006", "创业板指", 2010),
    ("1.000300", "sh000300", "沪深300", 2005),
    ("1.000905", "sh000905", "中证500", 2007),
    ("1.000016", "sh000016", "上证50", 2004),
    ("1.000688", "sh000688", "科创50", 2020),
    ("1.517400", "sh517400", "黄金股", 2021),   # 黄金股ETF（跟踪中证沪深300黄金股指数）
]
SMALL_CAP = ("1.000852", "sh000852", "中证1000", 2014)
LARGE_CAP = ("1.000300", "sh000300", "沪深300", 2005)

VAL_MAP = {
    "上证指数": "000010.SH",
    "深证成指": "399330.SZ",
    "创业板指": "399673.SZ",
    "沪深300": "000300.SH",
    "中证500": "000905.SH",
    "上证50": "000016.SH",
    "科创50": "399673.SZ",
}

KLIMIT = 9000        # 日K 抓取上限（覆盖上证指数 1990 年至今）
MARGIN_PAGES = 9     # 融资数据页数（9×500=4500 条，覆盖 2010 年至今）
WINDOW = 750         # 价格类分位窗口 = 近 3 年
VAL_WINDOW = 60      # 估值分位窗口 = 近 60 个月
RECENT_DAYS = 500    # 近期保留日线点的天数（约 2 年），更早按月采样

PRICE_KEYS = ["momentum", "trend", "breadth", "volatility", "volume", "drawdown"]
EXTRA_KEYS = ["margin", "riskon"]
PART_ORDER = PRICE_KEYS + EXTRA_KEYS + ["valuation"]

# 分项权重（依据：① 成交与价格动量是 A 股情绪最直接的体现 ② 融资是真实的杠杆情绪
# ③ 波动率降权——A 股"缩量低波动"多为情绪低迷而非贪婪，与美股语义不同
# ④ 估值不参与合成——恐贪指数为情绪/交易面指标，估值单独作为参考项展示）
WEIGHTS = {
    "momentum": 1.6,    # 价格动量
    "volume": 1.6,      # 量能热度
    "margin": 1.4,      # 融资热度（杠杆资金）
    "trend": 1.0,       # 短期趋势
    "breadth": 1.0,     # 均线广度
    "drawdown": 1.0,    # 回撤深度
    "riskon": 0.8,      # 风险偏好
    "volatility": 0.5,  # 波动率（降权）
}   # valuation = 参考项，不计入


def _get(url: str, timeout: int = 60, headers: dict = None) -> dict:
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


def _tx_rows(code: str, start: str, end: str):
    d = ((_get(TX_KLINE_RANGE % (code, start, end), timeout=30).get("data")) or {}).get(code) or {}
    rows = d.get("qfqday") or d.get("day") or []
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            out.append((row[0], float(row[2]), float(row[5])))
        except ValueError:
            continue
    return out


def fetch_tx(code: str, first_year: int = 1990, seg: int = 3, workers: int = 3):
    """腾讯单次上限 800 条 → 按 3 年分段并发抓取完整历史"""
    spans = []
    y = first_year
    this_year = date.today().year
    while y <= this_year:
        e = min(y + seg - 1, this_year)
        spans.append(("%d-01-01" % y, "%d-12-31" % e))
        y = e + 1
    merged = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for rows in ex.map(lambda sp: _tx_rows(code, sp[0], sp[1]), spans):
            merged.extend(rows)
    dedup = {}
    for d, c, v in merged:
        dedup[d] = (c, v)
    if not dedup:
        raise ValueError("empty day rows")
    keys = sorted(dedup)
    return keys, [dedup[k][0] for k in keys], [dedup[k][1] for k in keys]


def get_kline(label: str, em_id: str, tx_code: str, first_year: int = 1990):
    try:
        name, dates, closes, vols = fetch_em(em_id)
        if len(dates) > 1000:          # 东财可用且拿到长历史，直接用
            return name, dates, closes, vols
    except Exception:  # noqa: BLE001
        pass
    dates, closes, vols = fetch_tx(tx_code, first_year)
    print("   fallback → 腾讯分段: %s（%s 起 %d 条）" % (label, dates[0], len(dates)))
    return label, dates, closes, vols


def fetch_margin():
    out = {}
    for page in range(1, MARGIN_PAGES + 1):
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
        time.sleep(0.5)
    print("   融资数据历史: %d 条（%s 起）" % (len(out), min(out).__str__() if out else "-"))
    return out


def fetch_valuation():
    try:
        token = hashlib.md5(date.today().isoformat().encode("utf-8")).hexdigest()
        cj = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        opener.addheaders = [("User-Agent", UA)]
        html = opener.open(LG_PAGE, timeout=45).read().decode("utf-8", "ignore")
        m = re.search(r'_csrf"\s+content="([^"]+)"', html)
        csrf = m.group(1) if m else ""

        out = {}
        for code in sorted(set(VAL_MAP.values())):
            try:
                req = urllib.request.Request(
                    "%s?token=%s&indexCode=%s" % (LG_API, token, code),
                    headers={"User-Agent": UA, "X-CSRF-TOKEN": csrf, "Referer": LG_PAGE})
                rows = (json.loads(opener.open(req, timeout=45).read()) or {}).get("data") or []
                series = [(str(r.get("date"))[:7], float(r["ttmPe"]))
                          for r in rows if r.get("date") and r.get("ttmPe")]
                if not series:
                    print("   估值 %s 无数据" % code)
                    continue
                q = {}
                for i, (mo, v) in enumerate(series):
                    lo = max(0, i - VAL_WINDOW + 1)
                    win = [x[1] for x in series[lo:i + 1]]
                    q[mo] = round(100.0 * sum(1 for x in win if x <= v) / len(win), 1)
                out[code] = q
                print("   估值 %-11s %d 个月（%s 起）" % (code, len(series), series[0][0]))
            except Exception as e:  # noqa: BLE001
                print("   估值 %s 失败: %s" % (code, e))
            time.sleep(0.7)
        return out
    except Exception as e:  # noqa: BLE001
        print("   估值数据源不可用（该分项跳过）: %s" % e)
        return {}


def pct_rank_series(series, window=WINDOW):
    """滑动窗口分位（bisect 维护有序窗口，比逐窗口求和快一个量级）"""
    out, win = [], []
    n = len(series)
    for i, v in enumerate(series):
        bisect.insort(win, v)
        if len(win) > window:
            del win[bisect.bisect_left(win, series[i - window])]
        out.append(100.0 * bisect.bisect_right(win, v) / len(win))
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


def build_rows(dates, closes, vols, margin_by_date, small, large, val_by_month):
    """逐日计算各分项原始值；缺失的分项不参与该日合成（早期数据自动降级）"""
    n = len(closes)
    if n < 200:
        return None

    m_ser = align_map(dates, margin_by_date)
    s_ser = align_map(dates, small)
    l_ser = align_map(dates, large)

    rows = []
    for i in range(125, n):
        ma125 = sum(closes[i - 124:i + 1]) / 125.0
        if ma125 <= 0:
            continue
        row = {
            "date": dates[i],
            "close": closes[i],
            "momentum": closes[i] / ma125 - 1.0,
            "trend": closes[i] / closes[i - 20] - 1.0,
        }
        ma20hits = 0
        for k in range(i - 19, i + 1):
            if closes[k] > sum(closes[k - 19:k + 1]) / 20.0:
                ma20hits += 1
        row["breadth"] = ma20hits / 20.0

        rets = []
        for k in range(i - 19, i + 1):
            if closes[k - 1]:
                rets.append(math.log(closes[k] / closes[k - 1]))
        mean = sum(rets) / len(rets)
        var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
        row["volatility"] = math.sqrt(var) * math.sqrt(252.0)

        ma60v = sum(vols[i - 59:i + 1]) / 60.0
        if ma60v > 0:
            row["volume"] = vols[i] / ma60v - 1.0

        hi250 = max(closes[max(0, i - 249):i + 1])
        row["drawdown"] = closes[i] / hi250 - 1.0

        if m_ser[i] is not None and m_ser[i - 19] is not None and m_ser[i] > 0:
            base = sum(m_ser[i - 19:i + 1])
            if base > 0:
                row["margin"] = m_ser[i] / (base / 20.0) - 1.0

        if (s_ser[i] and s_ser[i - 60] and l_ser[i] and l_ser[i - 60]):
            row["riskon"] = (s_ser[i] / s_ser[i - 60] - 1.0) - (l_ser[i] / l_ser[i - 60] - 1.0)

        if val_by_month:
            vq = val_by_month.get(dates[i][:7])
            if vq is not None:
                row["valuation"] = vq          # 已是分位，不再二次排名

        rows.append(row)
    return rows


def score_rows(rows):
    """对每个分项分别做滚动分位，再按行等权平均（只用该日可用的分项）"""
    keys = [k for k in PART_ORDER if k in rows[-1] or any(k in r for r in rows)]
    for k in keys:
        if k == "valuation":
            continue                      # 估值已是分位
        ser = [r[k] for r in rows if k in r]
        if len(ser) < 30:
            continue
        ranks = pct_rank_series(ser)
        if k == "volatility":
            ranks = [100.0 - x for x in ranks]      # 波动率反向
        it = iter(ranks)
        for r in rows:
            if k in r:
                r["_r_" + k] = next(it)
    for r in rows:
        if "valuation" in r:
            r["_r_valuation"] = r["valuation"]          # 仅展示，不参与合成
        num = den = 0.0
        cnt = 0
        for k, v in r.items():
            if k.startswith("_r_") and k != "_r_valuation":
                w = WEIGHTS.get(k[3:], 1.0)
                num += v * w
                den += w
                cnt += 1
        if den > 0:
            r["score"] = round(num / den, 1)
            r["n"] = cnt
    return [r for r in rows if "score" in r]


def sample_rows(rows, recent=RECENT_DAYS):
    """近 recent 个交易日保留日线，更早按每月最后一个交易日采样"""
    n = len(rows)
    if n <= recent:
        return list(range(n))
    idx = []
    older = {}
    for j in range(0, n - recent):
        older[rows[j]["date"][:7]] = j        # 同月覆盖 → 保留当月最后一个
    idx.extend(sorted(older.values()))
    idx.extend(range(n - recent, n))
    return idx


def compute(dates, closes, vols, margin_by_date, small, large, val_by_month, risk_pair=None):
    # 风险偏好基准特异化：板块类（如黄金股）传 risk_pair=(基准A收盘, 基准B收盘)，
    # 即「自身 vs 沪深300」的相对强弱；宽基类不传，用默认的「中证1000 vs 沪深300」
    if risk_pair:
        small = dict(zip(risk_pair[0], risk_pair[1]))
        large = dict(zip(risk_pair[0], risk_pair[2]))
    rows = build_rows(dates, closes, vols, margin_by_date, small, large, val_by_month)
    if not rows:
        return None
    rows = score_rows(rows)
    if len(rows) < 30:
        return None
    if len(rows) > 210:
        rows = rows[60:]           # 冷启动分位不可靠：序列开头的窗口未满，读数虚高/虚低

    sel = sample_rows(rows)
    daily_from = len(rows) - RECENT_DAYS
    points = [[rows[j]["date"], rows[j]["score"], round(rows[j]["close"], 2), rows[j]["n"],
               "d" if j >= daily_from else "m"] for j in sel]

    last = rows[-1]
    rating, rating_cn = rating_of(last["score"])
    parts = {k: round(last["_r_" + k], 1) for k in PART_ORDER if ("_r_" + k) in last}
    return last["score"], rating, rating_cn, parts, points, last["date"], len(rows)


def main() -> None:
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/fear-greed.json"

    margin_map = fetch_margin()
    print("   抓取估值数据（乐咕乐股）…")
    val_raw = fetch_valuation()

    print("   抓取中证1000 / 沪深300（风险偏好分项）…")
    _, s_dates, s_closes, _ = get_kline(SMALL_CAP[2], SMALL_CAP[0], SMALL_CAP[1], SMALL_CAP[3])
    _, l_dates, l_closes, _ = get_kline(LARGE_CAP[2], LARGE_CAP[0], LARGE_CAP[1], LARGE_CAP[3])
    small_map = dict(zip(s_dates, s_closes))
    large_map = dict(zip(l_dates, l_closes))

    out_indices, as_of = [], ""
    for em_id, tx_code, label, first_year in INDICES:
        try:
            name, dates, closes, vols = get_kline(label, em_id, tx_code, first_year)
        except Exception as e:  # noqa: BLE001
            print("skip %s: %s" % (label, e))
            continue

        vcode = VAL_MAP.get(name) or VAL_MAP.get(label)
        rp = None
        if label == "黄金股":                      # 板块类：风险偏好 = 自身 vs 沪深300
            l_map = dict(zip(l_dates, l_closes))
            rp = (dates, closes, [l_map.get(d) for d in dates])
        res = compute(dates, closes, vols, margin_map, small_map, large_map,
                      val_raw.get(vcode) if vcode else None, rp)
        if not res:
            print("skip %s: not enough data" % name)
            continue
        score, rating, rating_cn, parts, points, last_date, total = res
        as_of = max(as_of, last_date)
        out_indices.append({
            "name": name, "score": score, "rating": rating, "rating_cn": rating_cn,
            "parts": parts, "points": points, "valIndex": vcode or "",
            "fullDays": total, "start": dates[0],
        })
        print("ok  %-8s %6s  %s | 全史 %d 日至 %s，输出 %d 点（%s ~ %s）" % (
            name, score, rating_cn, total, last_date, len(points), points[0][0], points[-1][0]))
        time.sleep(1.0)

    if not out_indices:
        print("all indices failed, keep old file")
        sys.exit(1)

    n_parts = max(len(it["parts"]) for it in out_indices)
    out = {
        "date": as_of,
        "source": "自建多维模型（%d 分项分位合成）" % n_parts,
        "model": ("加权合成（价格动量1.6 / 量能1.6 / 融资1.4 / 短期趋势1 / 均线广度1 / "
                  "回撤1 / 风险偏好0.8 / 波动率0.5）；估值分位为参考项，不计入合成"),
        "window": "价格类取近 3 年（750 交易日）滚动分位；估值取近 60 个月 PE 分位",
        "weights": WEIGHTS,
        "sampling": "近 %d 个交易日为日线（标记 d），更早按每月最后一个交易日采样（标记 m）；时间跨度覆盖指数成立以来" % RECENT_DAYS,
        "indices": out_indices,
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %d 个指数, %d 分项, as_of=%s, %.1f KB" % (
        dst, len(out_indices), n_parts, as_of, os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
