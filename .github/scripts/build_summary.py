#!/usr/bin/env python3
"""为首页生成轻量汇总数据 data/summary.json

首页原本要加载 vix-history.json(350KB) + fear-greed.json(173KB) 才能画出两张迷你走势，
是首屏卡顿的主因。这里把它们裁剪成首页真正需要的部分（当前值 + 近 30 个走势点 + 各指数恐贪值
+ 美股多指标快照），体积约 5KB，首页只请求这一个文件。

用法:
    python build_summary.py [输出json路径]   默认 data/summary.json
    （需先存在 data/vix.json、data/vix-history.json、data/fear-greed.json、
      data/gold-fng.json、data/indices.json；缺失的模块会被跳过并保留其余部分）
"""
import json
import os
import sys

SPARK_N = 30
# 首页指标墙要展示的美股指标（顺序即展示顺序）；VIX 已单列，不重复
MKT_KEYS = ("vvix", "vix9d", "vix3m", "spx", "ndx", "rut")


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    dst = sys.argv[1] if len(sys.argv) > 1 else os.path.join(root, "data", "summary.json")
    data_dir = os.path.dirname(dst) or "data"

    out = {}

    # ---- VIX ----
    try:
        v = _load(os.path.join(data_dir, "vix.json"))
        h = _load(os.path.join(data_dir, "vix-history.json"))
        spark = [[p[0], p[1]] for p in (h.get("points") or [])[-SPARK_N:]]
        # CBOE 的 prev_day_close 在盘后已等于当日收盘价，不是前一交易日收盘；
        # 统一用 price - change 反推真实昨收，缺失时才退回原字段。
        prev = v.get("prev_close")
        try:
            if v.get("price") is not None and v.get("change") is not None:
                prev = round(float(v["price"]) - float(v["change"]), 4)
        except (TypeError, ValueError):
            pass
        out["vix"] = {
            "price": v.get("price"),
            "change": v.get("change"),
            "pct": v.get("change_percent"),
            "prev": prev,
            "time": v.get("last_trade_time"),
            "spark": spark,
        }
    except Exception as e:  # noqa: BLE001
        print("vix summary failed: %s" % e)

    # ---- 恐贪指数（主指数 = 第一个） ----
    try:
        f = _load(os.path.join(data_dir, "fear-greed.json"))
        idx = f.get("indices") or []
        if idx:
            main_it = idx[0]
            pts = main_it.get("points") or []
            delta5 = None
            if len(pts) > 6:
                delta5 = round(float(main_it["score"]) - float(pts[-6][1]), 1)
            out["fng"] = {
                "name": main_it.get("name"),
                "score": main_it.get("score"),
                "rating_cn": main_it.get("rating_cn"),
                "date": f.get("date"),
                "delta5": delta5,
                "n": len(main_it.get("parts") or {}),
                "spark": [[p[0], p[1]] for p in pts[-SPARK_N:]],
                "indices": [{"name": it.get("name"), "score": it.get("score")} for it in idx],
            }
    except Exception as e:  # noqa: BLE001
        print("fng summary failed: %s" % e)

    # ---- 国际黄金恐贪指数 ----
    try:
        g = _load(os.path.join(data_dir, "gold-fng.json"))
        gpts = g.get("points") or []
        gdelta5 = None
        if len(gpts) > 6:
            gdelta5 = round(float(g["score"]) - float(gpts[-6][1]), 1)
        out["gold"] = {
            "name": g.get("shortName") or g.get("name"),
            "score": g.get("score"),
            "rating_cn": g.get("rating_cn"),
            "date": g.get("date"),
            "delta5": gdelta5,
            "n": len(g.get("parts") or {}),
            "spark": [[p[0], p[1]] for p in gpts[-SPARK_N:]],
        }
    except Exception as e:  # noqa: BLE001
        print("gold summary failed: %s" % e)

    # ---- 美股多指标（首页指标墙；数据来自 build_indices.py 的 indices.json）----
    try:
        ix = _load(os.path.join(data_dir, "indices.json"))
        mkt = []
        for k in MKT_KEYS:
            it = ix.get(k) or {}
            price = it.get("price")
            if price is None or price == 0:
                continue
            mkt.append({
                "key": k,
                "name": it.get("name"),
                "desc": it.get("desc"),
                "price": price,
                "chg": it.get("change"),
                "pct": it.get("pct"),
            })
        if mkt:
            out["mkt"] = mkt
        # 期限结构只取三个波动率期限点（短名固定，便于前端横排展示）
        term = []
        for k, label in (("vix9d", "VIX9D"), ("vix", "VIX"), ("vix3m", "VIX3M")):
            p = (ix.get(k) or {}).get("price")
            if p:
                term.append({"key": k, "name": label, "price": p})
        if len(term) == 3:
            out["term"] = term
    except Exception as e:  # noqa: BLE001
        print("market summary failed: %s" % e)

    if not out:
        print("nothing built, keep old file")
        sys.exit(1)

    os.makedirs(data_dir, exist_ok=True)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    print("wrote %s: %.1f KB" % (dst, os.path.getsize(dst) / 1024))


if __name__ == "__main__":
    main()
