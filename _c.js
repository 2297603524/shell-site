
  /* ===== 樱花 ===== */
  (function () {
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    var COUNT = (window.innerWidth < 640) ? 3 : 6;   // 帧率优先：少量粒子 + CSS 合成
    var GLYPHS = ['🌸', '❀', '✿', '🌸', '❀'];
    var colors = ['#ffb7d9', '#ffc7e5', '#f5c7ff', '#ffd6ea'];
    for (var i = 0; i < COUNT; i++) {
      var s = document.createElement('span');
      s.className = 'sakura';
      var dur = 9 + Math.random() * 10;
      s.style.left = (Math.random() * 100) + 'vw';
      s.style.fontSize = (10 + Math.random() * 15) + 'px';
      s.style.color = colors[Math.floor(Math.random() * colors.length)];
      s.style.setProperty('--dur', dur + 's');
      s.style.setProperty('--drift', (Math.random() * 40 - 20).toFixed(0) + 'px');
      s.style.setProperty('--op', (0.5 + Math.random() * 0.4).toFixed(2));
      s.style.animationDelay = (-Math.random() * dur) + 's';
      s.style.opacity = String(0.5 + Math.random() * 0.4);
      var inner = document.createElement('i');
      inner.textContent = GLYPHS[Math.floor(Math.random() * GLYPHS.length)];
      
      s.appendChild(inner);
      document.body.appendChild(s);
    }
  })();

  /* ===== 主逻辑 ===== */
  (function () {
    'use strict';
    var $ = function (id) { return document.getElementById(id); };
    var DATA = null, CUR = 0, WEIGHTS = {}, lastFngKey = '';   // 数据指纹
    var PART_NAMES = {
      momentum: '价格动量', trend: '短期趋势', breadth: '均线广度',
      volatility: '波动率', volume: '量能热度', margin: '融资热度',
      riskon: '风险偏好', drawdown: '回撤深度', valuation: '估值分位'
    };
    var PART_KEYS = ['momentum', 'trend', 'breadth', 'volatility', 'volume',
                     'margin', 'riskon', 'drawdown', 'valuation'];

    var svg = $('chartSvg'), tip = $('chartTip'), wrap = $('chartWrap');
    var lastHoverIdx = null;

    function fgCls(s) {
      if (s < 20) return 1;
      if (s < 40) return 2;
      if (s < 60) return 3;
      if (s < 80) return 4;
      return 5;
    }
    function f1(n) { return (Math.round(n * 10) / 10).toFixed(1); }

    /* ---------- 选中指数的展示 ---------- */
    function render() {
      if (!DATA || !DATA.indices || !DATA.indices.length) return;
      var it = DATA.indices[CUR];
      if (!it) return;
      var s = parseFloat(it.score);
      var c = fgCls(s);

      $('curScore').textContent = f1(s);
      $('curScore').className = 'price-big fg-c' + c;
      $('curRating').textContent = it.rating_cn || '--';
      $('curRating').className = 'badge bg-c' + c;
      $('curName').textContent = it.name + ' · ' + DATA.date;
      $('curMark').style.left = Math.max(1, Math.min(99, s)) + '%';
      $('asOf').textContent = '（数据 ' + (DATA.date || '--') + '）';
      var pnum = it.parts ? Object.keys(it.parts).length : 0;
      if (pnum && $('partCount')) $('partCount').textContent = String(pnum);

      var pts = it.points || [];
      if (pts.length > 6) {
        var diff = s - parseFloat(pts[pts.length - 6][1]);
        var sign = diff > 0 ? '+' : '';
        $('curDelta').textContent = sign + f1(diff) + '（较 5 个交易日前）';
        $('curDelta').className = 'price-chg fg-c' + c;
      } else {
        $('curDelta').textContent = '';
      }

      // 分项条
      // 分项条（含权重；估值分项标注为参考、不计入合成）
      var html = '';
      PART_KEYS.forEach(function (k) {
        var v = parseFloat((it.parts || {})[k]);
        if (!isFinite(v)) return;
        var w = WEIGHTS[k];
        var tag = (k === 'valuation')
          ? '<em class="pw ref">参考</em>'
          : (w ? '<em class="pw">×' + w + '</em>' : '<em class="pw"></em>');
        var cls = (k === 'valuation') ? 'part-row is-ref' : 'part-row';
        html += '<div class="' + cls + '"><span class="part-name">' + PART_NAMES[k] + '</span>'
          + '<span class="part-bar"><i style="width:' + Math.max(2, v) + '%"></i></span>'
          + '<span class="part-val fg-c' + fgCls(v) + '">' + f1(v) + '</span>' + tag + '</div>';
      });
      $('partBox').innerHTML = html || '<div class="note">分项数据缺失</div>';

      renderSegment();
      hideHover();
      drawChart();
    }

    function renderSegment() {
      if (!$('idxSeg') || !DATA) return;
      $('idxSeg').innerHTML = DATA.indices.map(function (it, i) {
        return '<button class="rbtn' + (i === CUR ? ' on' : '') + '" data-idx="' + i + '" type="button">'
          + it.name + '</button>';
      }).join('');
      Array.prototype.forEach.call($('idxSeg').querySelectorAll('.rbtn'), function (b) {
        b.addEventListener('click', function () {
          CUR = parseInt(b.getAttribute('data-idx'), 10) || 0;
          render();
        });
      });
    }

    /* ---------- 走势图（纵轴固定 0~100） ---------- */
    function calcSize() {
      var cw = wrap.clientWidth || 820;
      if (cw < 460) return { W: 400, H: 280, PAD: { l: 36, r: 50, t: 16, b: 30 } };
      if (cw < 700) return { W: 620, H: 290, PAD: { l: 42, r: 58, t: 17, b: 32 } };
      return { W: 820, H: 300, PAD: { l: 46, r: 66, t: 18, b: 34 } };
    }

    /* 周期 day|week；区间 0=全部，<=120 按点数（近 2 年的点均为日线），更长按日历天过滤 */
    var RANGE = 365, PERIOD = 'day';

    // 周线：日线点每 5 个合并为 1 个（保留每 5 个的最后一个 + 末点），月线点原样保留
    function applyPeriod(p) {
      if (PERIOD !== 'week') return p;
      var di = [];
      p.forEach(function (x, i) { if ((x[4] || 'd') === 'd') di.push(i); });
      if (di.length < 10) return p;
      var keep = {};
      di.forEach(function (i, k) { keep[i] = (k % 5 === 4) || (k === di.length - 1); });
      return p.filter(function (x, i) { return (x[4] || 'd') === 'm' || keep[i]; });
    }

    function pts() {
      var it = DATA && DATA.indices && DATA.indices[CUR];
      var all = (it && it.points) ? it.points : [];
      if (!all.length) return all;
      var out = all;
      if (RANGE) {
        if (RANGE <= 120) {
          out = all.slice(-RANGE);
        } else {
          var lastMs = Date.parse(all[all.length - 1][0] + 'T00:00:00Z');
          var cutoff = new Date(lastMs - RANGE * 86400000).toISOString().slice(0, 10);
          var f = all.filter(function (x) { return x[0] >= cutoff; });
          out = f.length >= 3 ? f : all.slice(-3);
        }
      }
      return applyPeriod(out);
    }

    (function bindControls() {
      var seg = $('rangeSeg');
      if (seg) {
        Array.prototype.forEach.call(seg.querySelectorAll('.rbtn'), function (b) {
          b.addEventListener('click', function () {
            Array.prototype.forEach.call(seg.querySelectorAll('.rbtn'), function (x) { x.classList.remove('on'); });
            b.classList.add('on');
            RANGE = parseInt(b.getAttribute('data-range'), 10) || 0;
            hideHover();
            drawChart();
          });
        });
      }
      var pseg = $('periodSeg');
      if (pseg) {
        Array.prototype.forEach.call(pseg.querySelectorAll('.rbtn'), function (b) {
          b.addEventListener('click', function () {
            Array.prototype.forEach.call(pseg.querySelectorAll('.rbtn'), function (x) { x.classList.remove('on'); });
            b.classList.add('on');
            PERIOD = b.getAttribute('data-period') || 'day';
            hideHover();
            drawChart();
          });
        });
      }
    })();

    function drawChart() {
      var p = pts();
      if (!p.length) {
        svg.innerHTML = '';
        $('chartSub').textContent = '（数据加载中…）';
        return;
      }
      var sz = calcSize(), W = sz.W, H = sz.H, PAD = sz.PAD;
      svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
      var fs = (W < 500) ? '13' : '11';

      var x0 = PAD.l, x1 = W - PAD.r, y0 = PAD.t, y1 = H - PAD.b;
      var nx = function (i) { return x0 + (i / (p.length - 1)) * (x1 - x0); };
      var ny = function (v) { return y1 - (v / 100) * (y1 - y0); };   // 固定 0~100

      var s = '<defs>'
        + '<linearGradient id="fngL" x1="0" y1="0" x2="1" y2="0">'
        + '<stop offset="0%" stop-color="#ff7eb6"/><stop offset="100%" stop-color="#a78bfa"/></linearGradient>'
        + '<linearGradient id="fngA" x1="0" y1="0" x2="0" y2="1">'
        + '<stop offset="0%" stop-color="#ff9ecb" stop-opacity="0.34"/>'
        + '<stop offset="100%" stop-color="#c4b5fd" stop-opacity="0.04"/></linearGradient></defs>';

      [0, 20, 40, 60, 80, 100].forEach(function (v) {
        var y = ny(v);
        var dash = (v === 0 || v === 100) ? '0' : '6 5';
        var col = v === 20 ? '#fda4af' : (v === 40 ? '#fcd34d' : (v === 60 ? '#bef264' : (v === 80 ? '#86efac' : '#ffe0f0')));
        s += '<line x1="' + x0 + '" y1="' + y + '" x2="' + x1 + '" y2="' + y + '" stroke="' + col + '" stroke-width="1" stroke-dasharray="' + dash + '"/>';
        s += '<text x="' + (x0 - 6) + '" y="' + (y + 4) + '" text-anchor="end" font-size="' + fs + '" fill="#b8a6c4">' + v + '</text>';
      });

      // 指数点位对照线（右轴；跨几十年用对数刻度，避免早期被压扁/出现负刻度）
      var priceVals = [];
      p.forEach(function (x) { if (isFinite(x[2]) && x[2] > 0) priceVals.push(x[2]); });
      if (priceVals.length > 1) {
        var pmn = Math.min.apply(null, priceVals), pmx = Math.max.apply(null, priceVals);
        var useLog = (pmx / pmn) > 4;
        var f = useLog ? Math.log : function (v) { return v; };
        var lo = f(pmn), hi = f(pmx);
        var pad = (hi - lo) * 0.05;
        lo -= pad; hi += pad;
        var pny = function (v) { return y1 - (f(v) - lo) / (hi - lo) * (y1 - y0); };
        var pline = '';
        p.forEach(function (x, i) {
          if (!isFinite(x[2]) || x[2] <= 0) return;
          pline += (pline ? 'L' : 'M') + nx(i).toFixed(1) + ' ' + pny(x[2]).toFixed(1);
        });
        s += '<path d="' + pline + '" fill="none" stroke="#8ea6c4" stroke-width="1.5"'
          + ' stroke-dasharray="5 4" opacity="0.95"/>';
        [0, 0.5, 1].forEach(function (t) {
          var v = useLog ? Math.exp(lo + (hi - lo) * t) : (lo + (hi - lo) * t);
          var txt = v >= 1000 ? Math.round(v).toLocaleString('en-US') : Math.round(v).toString();
          s += '<text x="' + (x1 + 6) + '" y="' + (pny(v) + 4) + '" font-size="' + fs
            + '" fill="#8ea6c4">' + txt + '</text>';
        });
      }

      // 恐贪指数折线 + 面积
      var line = '', area = '';
      p.forEach(function (it, i) {
        var x = nx(i).toFixed(1), y = ny(it[1]).toFixed(1);
        line += (i ? 'L' : 'M') + x + ' ' + y;
        area += (i ? 'L' : 'M') + x + ' ' + y;
      });
      area += 'L' + nx(p.length - 1).toFixed(1) + ' ' + y1 + 'L' + nx(0).toFixed(1) + ' ' + y1 + 'Z';
      s += '<path d="' + area + '" fill="url(#fngA)"/>';
      s += '<path d="' + line + '" fill="none" stroke="url(#fngL)" stroke-width="2.6" stroke-linejoin="round" stroke-linecap="round"/>';

      var xIdx = [];
      [0, Math.floor((p.length - 1) / 2), p.length - 1].forEach(function (i) {
        if (i >= 0 && xIdx.indexOf(i) === -1) xIdx.push(i);
      });
      var crossYear = p[0][0].slice(0, 4) !== p[p.length - 1][0].slice(0, 4);
      xIdx.forEach(function (i, k) {
        var anchor = (k === 0) ? 'start' : (k === xIdx.length - 1 ? 'end' : 'middle');
        var label = crossYear ? p[i][0].slice(0, 7) : p[i][0].slice(5);
        s += '<text x="' + nx(i).toFixed(1) + '" y="' + (H - 10) + '" text-anchor="' + anchor
          + '" font-size="' + fs + '" fill="#b8a6c4">' + label + '</text>';
      });

      s += '<line id="hoverLine" x1="0" y1="' + y0 + '" x2="0" y2="' + y1 + '" stroke="#c084fc" stroke-width="1.2" stroke-dasharray="4 4" opacity="0"/>';
      s += '<circle id="hoverDot" r="5" fill="#fff" stroke="#ff7eb6" stroke-width="2.4" opacity="0"/>';
      svg.innerHTML = s;

      svg._map = { pts: p, nx: nx, ny: ny, x0: x0, x1: x1, W: W, H: H };

      var it = DATA.indices[CUR];
      var full = it.fullDays ? '，全史 ' + it.fullDays + ' 个交易日' : '';
      var ptype = (PERIOD === 'week') ? '周线' : '日线';
      $('chartSub').textContent = '（' + it.name + '，' + p[0][0] + ' ~ ' + p[p.length - 1][0]
        + '，' + ptype + ' ' + p.length + ' 个点' + full + '）';

      if (lastHoverIdx !== null) applyHover(Math.min(lastHoverIdx, p.length - 1));
    }

    function applyHover(idx) {
      if (!svg._map) return;
      var map = svg._map, p = map.pts;
      if (idx === null || idx < 0 || idx >= p.length) { hideHover(); return; }
      var rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      var x = map.nx(idx), y = map.ny(p[idx][1]);
      var hl = svg.querySelector('#hoverLine'), hd = svg.querySelector('#hoverDot');
      if (!hl || !hd) return;
      hl.setAttribute('x1', x); hl.setAttribute('x2', x); hl.setAttribute('opacity', '0.9');
      hd.setAttribute('cx', x); hd.setAttribute('cy', y); hd.setAttribute('opacity', '1');
      var leftPx = x / map.W * rect.width, topPx = y / map.H * rect.height;
      var maxL = Math.max(rect.width - 56, 56);
      tip.style.left = Math.min(Math.max(leftPx, 56), maxL) + 'px';
      tip.style.top = Math.max(topPx, 14) + 'px';
      var priceTxt = isFinite(p[idx][2])
        ? '　点位 ' + (Math.abs(p[idx][2]) >= 1000
          ? Number(p[idx][2]).toLocaleString('en-US', { maximumFractionDigits: 2 })
          : p[idx][2])
        : '';
      var nTxt = (p[idx][3] && p[idx][3] < 9) ? '　分项 ' + p[idx][3] + '/9' : '';
      tip.textContent = p[idx][0] + '　恐贪 ' + f1(p[idx][1]) + priceTxt + nTxt;
      tip.classList.add('on');
      lastHoverIdx = idx;
    }

    function hideHover() {
      lastHoverIdx = null;
      tip.classList.remove('on');
      var hl = svg.querySelector('#hoverLine'), hd = svg.querySelector('#hoverDot');
      if (hl) hl.setAttribute('opacity', '0');
      if (hd) hd.setAttribute('opacity', '0');
    }

    function onMove(ev) {
      if (!svg._map) return;
      var map = svg._map, p = map.pts;
      var rect = svg.getBoundingClientRect();
      if (!rect.width) return;
      var px = (ev.clientX - rect.left) / rect.width * map.W;
      if (px < map.x0 - 10 || px > map.x1 + 10) { hideHover(); return; }
      var best = Math.round((px - map.x0) / (map.x1 - map.x0) * (p.length - 1));
      best = Math.max(0, Math.min(p.length - 1, best));
      applyHover(best);
    }

    wrap.addEventListener('mousemove', onMove);
    wrap.addEventListener('mouseleave', hideHover);
    wrap.addEventListener('touchstart', function (e) { if (e.touches[0]) onMove(e.touches[0]); }, { passive: true });
    wrap.addEventListener('touchmove', function (e) { if (e.touches[0]) onMove(e.touches[0]); }, { passive: true });
    wrap.addEventListener('touchend', hideHover);

    var rsTimer = null;
    window.addEventListener('resize', function () {
      clearTimeout(rsTimer);
      rsTimer = setTimeout(function () { hideHover(); drawChart(); }, 200);
    });

    /* ---------- 取数 ---------- */
    function load() {
      fetch('./data/fear-greed.json', { cache: 'no-cache' })
        .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
        .then(function (d) {
          var k = JSON.stringify(d);
          DATA = d;
          if (d.weights) WEIGHTS = d.weights;
          if (CUR >= (d.indices || []).length) CUR = 0;
          if (k !== lastFngKey) { lastFngKey = k; render(); }
          $('curErr').textContent = '';
        })
        .catch(function () { $('curErr').textContent = '⚠ 数据加载失败'; });
    }

    // ?idx=2 直达某指数、?range=30|60|120|365|1825|0 直达区间（也便于无头验证）
    (function initIdx() {
      var m = /[?&]idx=(\d+)/.exec(location.search);
      if (m) CUR = parseInt(m[1], 10) || 0;
      var r = /[?&]range=(\d+)/.exec(location.search);
      if (r) {
        var rv = parseInt(r[1], 10) || 0;
        RANGE = rv;
        var seg = $('rangeSeg');
        if (seg) {
          Array.prototype.forEach.call(seg.querySelectorAll('.rbtn'), function (x) {
            x.classList.toggle('on', (parseInt(x.getAttribute('data-range'), 10) || 0) === rv);
          });
        }
      }
      var pd = /[?&]period=(day|week)/.exec(location.search);
      if (pd) {
        PERIOD = pd[1];
        var pseg = $('periodSeg');
        if (pseg) {
          Array.prototype.forEach.call(pseg.querySelectorAll('.rbtn'), function (x) {
            x.classList.toggle('on', (x.getAttribute('data-period') || 'day') === PERIOD);
          });
        }
      }
    })();

    load();
    setInterval(load, 5000);   // 全部数据每 5 秒刷新
  })();
  
  /* ===== iOS 级动效：入场结束后释放合成层 + 数值刷新闪光反馈 ===== */
  (function () {
    // 卡片入场动画结束后解除 will-change（避免长期占用 GPU 合成层）
    document.addEventListener('animationend', function (e) {
      var t = e.target;
      if (t && t.classList && t.classList.contains('tile')) t.classList.add('settled');
    }, true);
    window.__flash = function (el) {
      if (!el) return;
      el.classList.remove('flash');
      void el.offsetWidth;          // 重排以重启动画
      el.classList.add('flash');
    };
  })();

  /* ===== 页面转场：浏览器不支持 View Transitions 时用淡入模拟（老设备/其它内核也能有转场感） ===== */
  (function () {
    var root = document.documentElement;
    if ('startViewTransition' in document) return;      // 原生支持，交给 CSS
    if (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches) return;
    root.style.opacity = '0';
    root.style.transition = 'opacity .30s cubic-bezier(.4, 0, .2, 1)';
    var show = function () { root.style.opacity = '1'; };
    if (document.readyState === 'complete') { requestAnimationFrame(show); }
    else { window.addEventListener('load', function () { requestAnimationFrame(show); }, { once: true }); }
  })();

  /* ===== 樱花智能暂停：页面隐藏或滚动时暂停（帧率与省电优先，停止后自动恢复） ===== */
  (function () {
    var els = document.querySelectorAll('.sakura');
    if (!els.length) return;
    var setAll = function (state) {
      for (var i = 0; i < els.length; i++) els[i].style.animationPlayState = state;
    };
    document.addEventListener('visibilitychange', function () {
      setAll(document.hidden ? 'paused' : 'running');
    });
    var t = null;
    window.addEventListener('scroll', function () {
      setAll('paused');
      clearTimeout(t);
      t = setTimeout(function () { setAll('running'); }, 280);
    }, { passive: true });
  })();
