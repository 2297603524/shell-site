# 樱市罗盘 · 市场情绪观测站

樱花主题的纯静态市场情绪看板，托管于 GitHub Pages。

## 页面

| 页面 | 内容 |
|---|---|
| `index.html` | 总览：VIX 恐慌指数、A股恐贪、黄金恐贪 + 美股多指标墙 + VIX 期限结构 |
| `vix.html` | VIX 详情：实时行情、日/周/分时走势、区间统计、档位解读 |
| `fng.html` | A股 11 个指数的恐贪指数与分项明细 |
| `gold.html` | 国际黄金恐贪指数与 K 线联动（价格史回溯至 1968 年） |

## 架构

无后端。数据由 GitHub Actions 抓取后落成 `data/*.json`，页面同域 `fetch` 读取，因此不受 CORS 限制。

```
官方数据源 ──► GitHub Actions（快 / 慢双轨）──► data/*.json ──► 静态页面
```

两个 workflow 共用同一个 `concurrency` group，串行执行以避免并发提交冲突：

- **`update-fast.yml`** —— 设计为每 5 分钟。只抓 CBOE 实时报价并重建首页摘要，秒级完成。
  高频调度必须让单次 job 足够短，否则会被 GitHub 丢弃、退化成低频。
- **`update-history.yml`** —— 每日两次（A股收盘后、美股收盘后）。抓全量历史并重算恐贪模型。

## 数据源（全部免密钥）

| 用途 | 来源 |
|---|---|
| VIX 及美股指数 | CBOE 官方 `cdn.cboe.com` |
| A股 K 线 / 融资 | 东方财富、中证官网、腾讯行情 |
| 指数 PE 历史 | 乐咕乐股 |
| 黄金白银现货 | 新浪财经 XAU / XAG |
| 伦敦金定盘价前史 | FRED `GOLDPMGBD228NLBM` |

## 目录

```
.github/scripts/    数据抓取与模型脚本（产出 data/*.json）
.github/workflows/  定时任务
data/               抓取产物，页面直接读取
img/                背景与横幅（webp）
```

## 本地预览

```bash
python -m http.server 8099
# 打开 http://127.0.0.1:8099/index.html
```

## 说明

- 行情为延迟数据，仅用于观察市场情绪，不构成投资建议。
- 数据文件刻意不写入抓取时间戳：定时任务频繁运行时，内容未变就不产生空提交。
