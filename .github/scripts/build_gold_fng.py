#!/usr/bin/env python3
"""构建国际黄金恐贪指数（黄金专属因子模型 v2）→ data/gold-fng.json

因子口径融合自三个公开参考并按数据可得性改造（非照搬）:
    - preciousmetalsprices.com 黄金恐贪（动量25 / 波动率25 / 金银比15 / 加速度15 / ATH10 / 美元10）
    - Alternative.me 加密恐贪（经 CoinGlass 展示；波动急升=恐惧、买压=贪婪的方向设计）
    - 韭圈儿 A 股恐贪（滚动分位法 + 因子方向语义）
共同内核 = 动量 + 波动率急升 + 一个跨资产风险信号（股市=股债性价比、币=支配率、金=金银比/美元）。

⚠️ 黄金语义校正（与股市恐贪相反之处）:
    地缘/通胀恐慌时黄金上涨 —— 黄金恐贪衡量的是「贵金属市场的风险偏好」，
    金价涨 = 黄金情绪贪婪，金银比飙升（只抱黄金）= 避险 = 恐惧，美元走强（压金价）= 恐惧。

黄金版 7 因子（滚动 750 日分位 → 加权合成 → 3 日 EMA 平滑 → 平滑后重分位）:
    ① 价格动量  ×2.0  收盘 vs MA50 偏离（业界共识窗长）
    ② 波动率急升 ×1.2  10日/60日 年化波动率之比，反向（急升=恐慌事件）
    ③ 金银比    ×1.0  金银比 60 日变化，反向（比值飙升=只抱黄金=避险恐惧）
    ④ 趋势加速度 ×1.0  20 日动量 − 60 日动量（加速上行=贪婪）
    ⑤ 距历史高点 ×0.8  收盘距「截至当日」全史最高价的回撤，业界线性刻度（非分位）：
                        score = 100·exp(−回撤%/12) —— 贴近高点=贪婪、回撤 10%≈中性、
                        回撤 20%≈19、>30%=深度恐惧。长牛市中分位法会失真（正常回调被读成极端），线性刻度才符合直觉
    ⑥ 趋势质量  ×0.8  20 日内收盘在 MA20 上方的天数占比（单资产版广度）
    ⑦ 美元强弱  ×0.8  EUR/USD（腾讯外汇）60 日动量，反向（美元强=压金价=恐惧）
       —— 数据源不可用时该因子自动缺失降级。
    黄金现货无公开成交量，量能/融资/估值因子不适用。
    ⚠️ 平滑后重分位：EMA 会把分布压缩向 50，若直接输出会「粘在中档」；
       对平滑序列再做一次 750 日滚动分位，恢复 0–100 全幅语义（读数=比近 3 年多少时间更贪婪）。

数据源（全部免密钥）:
    伦敦金现 OHLC: 新浪 XAU（2006-09 起，约 5200 日）
    伦敦银现 OHLC: 新浪 XAG（2006-09 起，与 XAU 同区间）→ 金银比
    美元:          腾讯外汇 whEURUSD（EUR/USD 近 900 日）
    K 线前史:      FRED GOLDPMGBD228NLBM（1968-04 起伦敦金 PM 定盘价，仅收盘价 → 粒度 "p"）
                   首次成功抓取后缓存为 data/gold-prehistory.json，此后零网络开销

输出:
    points: [[date, score, close, n, grain], ...]  grain: d=日线 | m=月线 | p=定盘价月线
    kline:  [[date, open, high, low, close, grain], ...]  近 500 日日线 + 更早月度聚合

用法:
    python build_gold_fng.py [输出json路径]   默认 data/gold-fng.json
"""
import bisect
import io
import json
import math
import os
import sys
import time
import urllib.request
import zipfile
from datetime import date, datetime, timedelta

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36")

SINA_KLINE = ("https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%%20t=/"
              "GlobalFuturesService.getGlobalFuturesDailyKLine?symbol=%s")
TX_FX = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=whEURUSD,day,,,900,qfq"
FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=%s"

WINDOW = 750          # 分位窗口 = 近 3 年
RECENT_DAYS = 500     # 近期保留日线点数（约 2 年），更早按月采样
WARMUP = 260          # 因子热身：MA50/60日动量/250日滚动高点全部就位所需最少交易日

WEIGHTS = {
    "momentum": 2.0,    # 价格动量（vs MA50）
    "volSpike": 1.2,    # 波动率急升（10v60 比，反向）
    "goldSilver": 1.0,  # 金银比 60 日变化（反向）
    "accel": 1.0,       # 趋势加速度（20 日动量 − 60 日动量）
    "athDist": 0.8,     # 距全史高点（业界线性刻度，非分位）
    "breadth": 0.8,     # 趋势质量（20 日内收盘>MA20 占比）
    "cftc": 0.8,        # CFTC 杠杆基金净多头（周频，持仓分位）
    "usd": 0.8,         # 美元强弱（反向，数据源可用时）
}
PART_ORDER = ["momentum", "accel", "volSpike", "goldSilver", "athDist", "breadth", "cftc", "usd"]
CFTC_WEEKS = 156      # CFTC 周频分位窗口 ≈ 3 年

LEVELS = [
    (20, "extreme fear", "极度恐惧"),
    (40, "fear", "恐惧"),
    (60, "neutral", "中性"),
    (80, "greed", "贪婪"),
]


def _get(url, timeout=60, headers=None):
    h = {"User-Agent": UA, "Accept": "*/*", "Connection": "close"}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_sina(sym):
    """新浪全球贵金属/期货日 K：[(date, open, high, low, close)]"""
    text = _get(SINA_KLINE % sym, timeout=60,
                headers={"Referer": "https://finance.sina.com.cn/"}).decode("utf-8", "ignore")
    i, j = text.find("[{"), text.rfind("}]")
    if i < 0 or j <= i:
        raise ValueError("sina %s: no payload" % sym)
    rows = json.loads(text[i:j + 2])
    days = []
    for r in rows:
        try:
            d = str(r.get("date") or "")
            o, h, l, c = (float(r.get("open")), float(r.get("high")),
                          float(r.get("low")), float(r.get("close")))
        except (TypeError, ValueError):
            continue
        # 规范化 OHLC（脏数据防御：高低包住开收）
        h = max(h, o, c)
        l = min(l, o, c)
        if d and o > 0 and c > 0 and h >= l:
            days.append((d, o, h, l, c))
    if len(days) < 400:
        raise ValueError("sina %s: insufficient rows (%d)" % (sym, len(days)))
    print("   新浪 %s: %d 个交易日（%s ~ %s）" % (sym, len(days), days[0][0], days[-1][0]))
    return days


def fetch_fred(fid, timeout=12, retries=2):
    """FRED CSV：[(date, close)]；缺失 "." 丢弃；失败返回 []（调用方降级）。
    短超时 + 少重试：FRED 不可达时不能拖慢整条数据流水线"""
    for attempt in range(retries):
        try:
            text = _get(FRED_CSV % fid, timeout=timeout).decode("utf-8", "ignore")
            out = []
            for ln in text.strip().splitlines()[1:]:
                parts = ln.split(",")
                if len(parts) < 2 or not parts[1] or parts[1] == ".":
                    continue
                try:
                    v = float(parts[1])
                except ValueError:
                    continue
                if v > 0:
                    out.append((parts[0], v))
            print("   FRED %s: %d 条（%s ~ %s）" % (fid, len(out),
                  out[0][0] if out else "-", out[-1][0] if out else "-"))
            return out
        except Exception as e:  # noqa: BLE001
            if attempt == retries - 1:
                print("   FRED %s 不可用（相关因子降级）: %s" % (fid, e))
                return []
            time.sleep(1)


def fetch_fx_eurusd():
    """腾讯外汇 EUR/USD 日K（近 900 根 ≈ 3.5 年）→ {date: close}，供美元因子"""
    try:
        data = json.loads(_get(TX_FX, timeout=30,
                               headers={"Referer": "https://gu.qq.com/"}).decode("utf-8", "ignore"))
        node = (data.get("data") or {}).get("whEURUSD") or {}
        rows = node.get("qfqday") or node.get("day") or []
        out = {}
        for r in rows:
            try:
                if len(r) > 2 and float(r[2]) > 0:
                    out[r[0]] = float(r[2])
            except (TypeError, ValueError):
                continue
        print("   腾讯外汇 EUR/USD: %d 个交易日" % len(out))
        return out
    except Exception as e:  # noqa: BLE001
        print("   腾讯外汇不可用（美元因子降级）: %s" % e)
        return {}


def _parse_cot_date(raw):
    """COT 日期格式防御式解析（新文件 YYYY-MM-DD HH:MM:SS，老文件 MM/DD/YYYY）"""
    raw = str(raw).strip().strip('"')
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%Y%m%d"):
        try:
            return datetime.strptime(raw[:len(fmt) + 6 if fmt == "%Y-%m-%d" else 10], fmt).date()
        except ValueError:
            continue
    # YYYYMMDDHHMMSS 之类
    digits = re.sub(r"\D", "", raw)
    if len(digits) >= 8:
        try:
            return datetime.strptime(digits[:8], "%Y%m%d").date()
        except ValueError:
            pass
    return None


def fetch_cftc():
    """CFTC Legacy 年报（近 3 个年度 zip，商品类报告）：COMEX 黄金非商业净多头（周频）。
    返回 {生效日: net_long}——生效日 = 持仓报告日 + 3 天（周五发布），避免前视偏差"""
    out = {}
    this_year = date.today().year
    for yy in (this_year - 2, this_year - 1, this_year):
        url = "https://www.cftc.gov/files/dea/history/deacot%d.zip" % yy
        try:
            zdata = _get(url, timeout=45)
        except Exception as e:  # noqa: BLE001
            print("   CFTC %d 下载失败: %s" % (yy, e))
            continue
        try:
            zf = zipfile.ZipFile(io.BytesIO(zdata))
            name = zf.namelist()[0]
            text = zf.read(name).decode("utf-8-sig", "ignore")
        except Exception as e:  # noqa: BLE001
            print("   CFTC %d 解析失败: %s" % (yy, e))
            continue
        lines = text.splitlines()
        if len(lines) < 2:
            continue
        # 分隔符自动检测（CFTC 年度 .txt 实为逗号分隔 CSV）
        sep = "\t" if lines[0].count("\t") > lines[0].count(",") else ","
        header = [c.strip().strip('"').replace("\xa0", " ") for c in lines[0].split(sep)]

        def find_col(*needles):
            """子串匹配列名（免疫空格/引号/全角差异）"""
            for i, c in enumerate(header):
                cl = " ".join(c.lower().split())
                if all(n in cl for n in needles):
                    return i
            return -1

        col_mkt = find_col("market and exchange")
        col_date = find_col("as of date in form yyyy-mm-dd")
        if col_date < 0:
            col_date = find_col("report_date")
        col_long = find_col("noncommercial positions-long")
        col_short = find_col("noncommercial positions-short")
        if min(col_mkt, col_date, col_long, col_short) < 0:
            print("   CFTC %d 列缺失（%d 列）" % (yy, len(header)))
            continue
        hit = 0
        for ln in lines[1:]:
            cells = ln.split(sep)
            if len(cells) < len(header):
                continue
            mkt = cells[col_mkt].upper()
            if "GOLD" not in mkt or "COMMODITY EXCHANGE" not in mkt:
                continue
            rd = _parse_cot_date(cells[col_date])
            if rd is None:
                continue
            try:
                net = float(cells[col_long]) - float(cells[col_short])
            except (TypeError, ValueError):
                continue
            eff = (rd + timedelta(days=3)).strftime("%Y-%m-%d")
            out[eff] = net
            hit += 1
        print("   CFTC %d: %d 周黄金记录" % (yy, hit))
        time.sleep(1.0)
    if not out:
        print("   CFTC 数据为空（持仓因子降级）")
    return out


def align_map(dates, value_map):
    """按日期对齐（前值填充）"""
    out, last = [], None
    for d in dates:
        v = value_map.get(d)
        if v is not None:
            last = v
        out.append(last)
    return out


def pct_rank_series(series, window=WINDOW):
    """滑动窗口分位（bisect 有序窗口）"""
    out, win = [], []
    for i, v in enumerate(series):
        bisect.insort(win, v)
        if len(win) > window:
            del win[bisect.bisect_left(win, series[i - window])]
        out.append(100.0 * bisect.bisect_right(win, v) / len(win))
    return out


def rating_of(score):
    for cut, en, cn in LEVELS:
        if score < cut:
            return en, cn
    return "extreme greed", "极度贪婪"


def build_rows(days, xag_map, usd_by_date, cftc_map):
    """逐日计算黄金版因子原始值；缺失因子不参与该日合成"""
    dates = [d[0] for d in days]
    closes = [d[4] for d in days]
    n = len(closes)
    if n < WARMUP + 30:
        return None

    xag_ser = align_map(dates, xag_map)
    usd_ser = align_map(dates, usd_by_date)
    cftc_ser = align_map(dates, cftc_map)

    rets = [None] * n
    for i in range(1, n):
        if closes[i - 1]:
            rets[i] = math.log(closes[i] / closes[i - 1])

    def std_annual(i, win):
        seg = [r for r in rets[i - win + 1:i + 1] if r is not None]
        if len(seg) < win * 0.8:
            return None
        mean = sum(seg) / len(seg)
        var = sum((r - mean) ** 2 for r in seg) / (len(seg) - 1)
        return math.sqrt(var) * math.sqrt(252.0)

    rows = []
    running_high = max(closes[:WARMUP])       # 「截至当日」全史最高（无前视偏差）
    for i in range(WARMUP, n):
        d = dates[i]
        if closes[i] > running_high:
            running_high = closes[i]

        row = {"date": d, "close": closes[i]}
        # ① 动量：vs MA50
        ma50 = sum(closes[i - 49:i + 1]) / 50.0
        if ma50 > 0:
            row["momentum"] = closes[i] / ma50 - 1.0
        # ④ 加速度：20 日动量 − 60 日动量
        if closes[i - 20] and closes[i - 60]:
            row["accel"] = (closes[i] / closes[i - 20] - 1.0) - (closes[i] / closes[i - 60] - 1.0)
        # ② 波动率急升：10 日 / 60 日 年化波动率之比
        v10 = std_annual(i, 10)
        v60 = std_annual(i, 60)
        if v10 is not None and v60 is not None and v60 > 0:
            row["volSpike"] = v10 / v60
        # ③ 金银比 60 日变化（XAG 可用时）
        if xag_ser[i] and xag_ser[i - 60] and xag_ser[i - 60] > 0:
            row["goldSilver"] = (closes[i] / xag_ser[i]) / (closes[i - 60] / xag_ser[i - 60]) - 1.0
        # ⑤ 距全史高点：业界线性刻度（贴高点=贪婪、回撤 10%≈中性、20%≈19），
        #    长牛市中分位法会把正常回调读成极端恐惧，这里必须用刻度而非分位
        if running_high > 0:
            dd = max(0.0, 1.0 - closes[i] / running_high)     # 回撤幅度（正数）
            row["athDist"] = 100.0 * math.exp(-dd * 100.0 / 12.0)
        # ⑥ 趋势质量：20 日内收盘在 MA20 上方的天数占比
        ma20hits = 0
        for k in range(i - 19, i + 1):
            if closes[k] > sum(closes[k - 19:k + 1]) / 20.0:
                ma20hits += 1
        row["breadth"] = ma20hits / 20.0
        # ⑦ CFTC 杠杆基金净多头（周频前值填充；净多高=投机资金看多=贪婪）
        if cftc_ser[i] is not None:
            row["cftc"] = cftc_ser[i]
        # ⑧ 美元 60 日动量（反向语义在分位处处理）
        if usd_ser[i] and usd_ser[i - 60] and usd_ser[i - 60] > 0:
            row["usd"] = usd_ser[i] / usd_ser[i - 60] - 1.0

        rows.append(row)
    return rows


def score_rows(rows):
    """各因子滚动分位 → 加权合成（只用该日可用的因子；反向因子取 100−x）。
    athDist 已是 0~100 线性刻度，跳过分位直接使用"""
    keys = [k for k in PART_ORDER if any(k in r for r in rows)]
    for k in keys:
        if k == "athDist":
            for r in rows:
                if k in r:
                    r["_r_" + k] = r[k]
            continue
        ser = [r[k] for r in rows if k in r]
        if len(ser) < 30:
            continue
        ranks = pct_rank_series(ser)
        if k in ("volSpike", "goldSilver", "usd"):
            ranks = [100.0 - x for x in ranks]
        it = iter(ranks)
        for r in rows:
            if k in r:
                r["_r_" + k] = next(it)
    for r in rows:
        num = den = cnt = 0.0
        for k, v in r.items():
            if k.startswith("_r_"):
                w = WEIGHTS.get(k[3:], 1.0)
                num += v * w
                den += w
                cnt += 1
        if den > 0:
            r["score"] = round(num / den, 1)
            r["n"] = int(cnt)
    return [r for r in rows if "score" in r]


def smooth_scores(rows, span=3):
    """3 日 EMA 平滑输出（与 A 股恐贪同规格）；分项明细保持当日原始分位"""
    a = 2.0 / (span + 1)
    prev = None
    for r in rows:
        s = r["score"]
        prev = s if prev is None else prev + a * (s - prev)
        r["score"] = round(prev, 1)


def build_points(rows):
    """近 RECENT_DAYS 日线，更早按月采样（取当月最后一个交易日）"""
    n = len(rows)
    if n <= RECENT_DAYS:
        sel = list(range(n))
    else:
        older = {}
        for j in range(0, n - RECENT_DAYS):
            older[rows[j]["date"][:7]] = j
        sel = sorted(older.values()) + list(range(n - RECENT_DAYS, n))
    daily_from = n - min(RECENT_DAYS, n)
    return [[rows[j]["date"], rows[j]["score"], round(rows[j]["close"], 2), rows[j]["n"],
             "d" if j >= daily_from else "m"] for j in sel]


def build_kline(days, pre_history):
    """K 线：近 500 日真日线 + 更早月度聚合；1968–2006 定盘价期粒度 "p"（o=h=l=c）"""
    out = []
    if pre_history:
        mon = {}
        for d, c in pre_history:
            mon[d[:7]] = (d, c)                    # 每月最后一条定盘价
        for mk in sorted(mon):
            d, c = mon[mk]
            c = round(c, 2)
            out.append([d, c, c, c, c, "p"])
    n = len(days)
    daily_from = max(0, n - RECENT_DAYS)
    older = {}
    for d, o, h, l, c in days[:daily_from]:
        k = d[:7]
        if k not in older:
            older[k] = [d, o, h, l, c]
        else:
            m = older[k]
            m[2] = max(m[2], h)
            m[3] = min(m[3], l)
            m[4] = c
            m[0] = d
    for k in sorted(older):
        d, o, h, l, c = older[k]
        out.append([d, round(o, 2), round(h, 2), round(l, 2), round(c, 2), "m"])
    for d, o, h, l, c in days[daily_from:]:
        out.append([d, round(o, 2), round(h, 2), round(l, 2), round(c, 2), "d"])
    seen, dedup = set(), []
    for row in out:
        if row[0] not in seen:
            seen.add(row[0])
            dedup.append(row)
    return dedup


def main():
    dst = sys.argv[1] if len(sys.argv) > 1 else "data/gold-fng.json"

    print("抓取伦敦金现（新浪 XAU）…")
    days = fetch_sina("XAU")
    print("抓取伦敦银现（新浪 XAG）→ 金银比因子…")
    xag_days = fetch_sina("XAG")
    xag_map = {d[0]: d[4] for d in xag_days}

    print("抓取美元（腾讯外汇 EUR/USD）…")
    usd_by_date = fetch_fx_eurusd()

    print("抓取 CFTC 持仓（COMEX 黄金杠杆基金净多头，周频）…")
    cftc_map = fetch_cftc()

    # 定盘价前史：1968-2006 的月度数据永不变化 → 一次性抓取后缓存为静态文件，之后零网络开销
    pre_file = os.path.join(os.path.dirname(dst) or ".", "gold-prehistory.json")
    pre = []
    if os.path.exists(pre_file):
        try:
            with open(pre_file, encoding="utf-8") as f:
                pre = [(r[0], r[1]) for r in json.load(f)]
            print("   定盘价前史（缓存）: %d 月（%s ~ %s）" % (len(pre), pre[0][0], pre[-1][0]))
        except Exception:  # noqa: BLE001
            pre = []
    if not pre:
        print("抓取 FRED 定盘价前史（首次）…")
        pre = fetch_fred("GOLDPMGBD228NLBM")
        if pre:
            monthly = {}
            for d, c in pre:
                monthly[d[:7]] = [d, c]           # 每月最后一条
            save = sorted(monthly.values())
            with open(pre_file, "w", encoding="utf-8") as f:
                json.dump(save, f, ensure_ascii=False, separators=(",", ":"))
            print("   前史已缓存 → %s（%d 月）" % (pre_file, len(save)))
    xau_start = days[0][0]
    pre_history = [(d, c) for d, c in pre if d < xau_start]

    rows = build_rows(days, xag_map, usd_by_date, cftc_map)
    if not rows:
        print("insufficient data, keep old file")
        sys.exit(1)
    rows = score_rows(rows)
    if len(rows) < 30:
        print("insufficient scored rows, keep old file")
        sys.exit(1)
    smooth_scores(rows)
    # 平滑后重分位：EMA 会把分布压缩向 50（读数粘在中档），对平滑序列再取一次
    # 750 日滚动分位，恢复 0–100 全幅语义 —— 大涨期能上 80+，暴跌期能下 20−
    ranks = pct_rank_series([r["score"] for r in rows])
    for r, rk in zip(rows, ranks):
        r["score"] = round(rk, 1)

    points = build_points(rows)
    kline = build_kline(days, pre_history)

    last = rows[-1]
    rating, rating_cn = rating_of(last["score"])
    parts = {k: round(last["_r_" + k], 1) for k in PART_ORDER if ("_r_" + k) in last}

    out = {
        "name": "国际黄金（伦敦金现 XAU/USD）",
        "shortName": "国际黄金",
        "date": last["date"],
        "score": last["score"],
        "rating": rating,
        "rating_cn": rating_cn,
        "parts": parts,
        "points": points,
        "kline": kline,
        "fullDays": len(rows),
        "start": rows[0]["date"],
        "priceStart": xau_start,
        "preHistoryFrom": pre_history[0][0] if pre_history else "",
        "nParts": len(parts),
        "source": "新浪财经 XAU/XAG（OHLC）+ 腾讯外汇 EUR/USD + CFTC 官方 COT + FRED 定盘价前史",
        "model": ("黄金版 8 因子加权合成（动量2.0 / 波动率急升1.2 / 金银比1.0 / 趋势加速度1.0 / "
                  "距历史高点0.8·线性刻度 / 趋势质量0.8 / CFTC净多头0.8·周频 / 美元0.8，"
                  "个别数据源不可用时该因子自动缺失降级）；"
                  "均为近 3 年滚动分位（距历史高点除外），输出经 3 日 EMA 平滑后再重分位，"
                  "保证大涨期读数能上 80+、暴跌期能下 20−；"
                  "因子口径融合 preciousmetalsprices 黄金版、Alternative.me 加密版与韭圈儿 A 股版"),
        "weights": WEIGHTS,
        "sampling": ("近 %d 个交易日为日线（d），更早按月聚合（m）；1968-2006 为伦敦金 PM 定盘价（仅收盘价，p）"
                     % RECENT_DAYS),
    }

    os.makedirs(os.path.dirname(dst) or ".", exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: score=%s %s, %d 因子, points=%d, kline=%d (%s ~ %s), %.1f KB"
          % (dst, out["score"], rating_cn, len(parts), len(points), len(kline),
             kline[0][0], kline[-1][0], os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
