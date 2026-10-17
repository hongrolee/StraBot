// ── 차트 분석 탭: 코인 하나를 크게 · 같은 코인 여러 봉 · 여러 코인을 한 화면에 (2026-10-11) ──
// bot_dashboard.js 다음에 읽음: formatPrice · formatChangeRate · formatTradeValue · pnlClass · escapeHtml · myAvgBuy · addAvgBuyLine
// · getHeldCurrencies · openSparkZoom · szState · SZ_UNITS · SZ_UNIT_SEC · showNotification 을 함께 씀
// ── 차트 분석 보조 지표 23가지 (슈퍼트렌드 · 스퀴즈 모멘텀 · 스마트 머니 구조 더함 · StraHub '전략 선택'과 같은 고르기 · 숫자 · 색 바꾸기, 계산은 화면 안에서) (2026-10-12) ──
// 계산은 TA-Lib 과 같은 방식 (앞쪽 값이 모자란 봉은 null)
// 거래량이 필요한 지표(OBV · MFI)는 봉 거래대금(원)으로 계산

// ── 계산 도구 ──
const ciNull = (n) => new Array(n).fill(null);
function ciSma(a, n) {
    const out = ciNull(a.length);
    let s = 0, cnt = 0;
    for (let i = 0; i < a.length; i++) {
        if (a[i] === null || a[i] === undefined) { s = 0; cnt = 0; continue; }
        s += a[i]; cnt++;
        if (cnt > n) { s -= a[i - n]; cnt = n; }
        if (cnt === n) out[i] = s / n;
    }
    return out;
}
function ciEma(a, n) {                       // 첫 n개 평균에서 시작 (null 은 건너뜀)
    const out = ciNull(a.length), k = 2 / (n + 1);
    let e = null, seed = [], start = -1;
    for (let i = 0; i < a.length; i++) {
        const v = a[i];
        if (v === null || v === undefined) continue;
        if (e === null) {
            seed.push(v);
            if (seed.length === n) { e = seed.reduce((x, y) => x + y, 0) / n; out[i] = e; }
            continue;
        }
        e = v * k + e * (1 - k);
        out[i] = e;
    }
    return out;
}
function ciWilder(a, n) {                     // 와일더 평활 (RSI · ATR · ADX)
    const out = ciNull(a.length);
    let s = null, seed = [];
    for (let i = 0; i < a.length; i++) {
        const v = a[i];
        if (v === null || v === undefined) continue;
        if (s === null) { seed.push(v); if (seed.length === n) { s = seed.reduce((x, y) => x + y, 0) / n; out[i] = s; } continue; }
        s = (s * (n - 1) + v) / n;
        out[i] = s;
    }
    return out;
}
function ciWma(a, n) {
    const out = ciNull(a.length), den = n * (n + 1) / 2;
    for (let i = n - 1; i < a.length; i++) {
        let s = 0;
        for (let j = 0; j < n; j++) s += a[i - j] * (n - j);
        out[i] = s / den;
    }
    return out;
}
function ciHighest(a, n, i) { let m = -Infinity; for (let j = i - n + 1; j <= i; j++) if (a[j] > m) m = a[j]; return m; }
function ciLowest(a, n, i) { let m = Infinity; for (let j = i - n + 1; j <= i; j++) if (a[j] < m) m = a[j]; return m; }
// 스퀴즈 모멘텀 [LazyBear]: BB 가 KC 안 = 스퀴즈(on), 밖 = 풀림(off) · 모멘텀 = linreg(종가 − ((n봉 최고+최저)/2 + n봉 평균)/2, n)
function ciSqueeze(H, L, C, n, m, nk, mk) {
    const len = C.length, basis = ciSma(C, n), ma = ciSma(C, nk), val = ciNull(len), dot = ciNull(len), sq = ciNull(len);
    const tr = ciTrueRange(H, L, C).map((x, i) => (x === null ? H[i] - L[i] : x)), rng = ciSma(tr, nk);
    const src = ciNull(len);
    for (let i = 0; i < len; i++) {
        if (i >= n - 1 && basis[i] !== null) {
            let v = 0;
            for (let j = i - n + 1; j <= i; j++) v += (C[j] - basis[i]) ** 2;
            const dev = m * Math.sqrt(v / n), lb = basis[i] - dev, ub = basis[i] + dev;
            if (ma[i] !== null && rng[i] !== null) {
                const lk = ma[i] - rng[i] * mk, uk = ma[i] + rng[i] * mk;
                sq[i] = lb > lk && ub < uk ? 1 : lb < lk && ub > uk ? -1 : 0;
                dot[i] = 0;
            }
        }
        if (i >= nk - 1) src[i] = C[i] - ((ciHighest(H, nk, i) + ciLowest(L, nk, i)) / 2 + (ma[i] ?? C[i])) / 2;
    }
    const xm = (nk - 1) / 2;
    let den = 0;
    for (let x = 0; x < nk; x++) den += (x - xm) ** 2;
    for (let i = 2 * nk - 2; i < len; i++) {
        let sy = 0, sxy = 0, ok = true;
        for (let x = 0; x < nk; x++) { const y = src[i - nk + 1 + x]; if (y === null) { ok = false; break; } sy += y; }
        if (!ok) continue;
        const ym = sy / nk;
        for (let x = 0; x < nk; x++) sxy += (x - xm) * (src[i - nk + 1 + x] - ym);
        val[i] = ym + (sxy / den) * (nk - 1 - xm);
    }
    return { val, dot, sq };
}
function ciSqzColor(val, i) {
    const v = val[i], p = val[i - 1];
    if (v === null) return 'rgba(0,0,0,0)';
    const up = p === null || p === undefined || v >= p;
    return v >= 0 ? (up ? CL_UP : 'rgba(200,74,49,0.42)') : (up ? 'rgba(18,97,196,0.42)' : CL_DOWN);
}
function ciSqzDotColor(sq, i) { return sq[i] === 1 ? '#0f172a' : sq[i] === -1 ? '#94a3b8' : '#2563eb'; }
// 스마트 머니 구조 [LuxAlgo 방식]: n봉 뒤까지 확인된 고점 · 저점을 종가가 넘으면 BOS(추세와 같은 쪽) · CHoCH(반대 쪽)
function ciSmcScan(H, L, C, size) {
    const ev = [];
    let os = 0, top = null, bot = null, trend = 0;
    for (let i = size; i < C.length; i++) {
        let hi = -Infinity, lo = Infinity;
        for (let j = i - size + 1; j <= i; j++) { if (H[j] > hi) hi = H[j]; if (L[j] < lo) lo = L[j]; }
        const prev = os;
        if (H[i - size] > hi) os = 0; else if (L[i - size] < lo) os = 1;
        if (os === 0 && prev !== 0) top = { v: H[i - size], at: i - size, x: false };
        if (os === 1 && prev !== 1) bot = { v: L[i - size], at: i - size, x: false };
        if (top && !top.x && C[i] > top.v && C[i - 1] <= top.v) { ev.push({ i, up: true, kind: trend === -1 ? 'CHoCH' : 'BOS', level: top.v, from: top.at }); top.x = true; trend = 1; }
        if (bot && !bot.x && C[i] < bot.v && C[i - 1] >= bot.v) { ev.push({ i, up: false, kind: trend === 1 ? 'CHoCH' : 'BOS', level: bot.v, from: bot.at }); bot.x = true; trend = -1; }
    }
    return ev;
}
function ciSmc(H, L, C, internal, swing) {
    return { events: [...ciSmcScan(H, L, C, internal).map(e => ({ ...e, big: false })), ...ciSmcScan(H, L, C, swing).map(e => ({ ...e, big: true }))] };
}
function ciSmcLast(v, k) {
    const e = (v.events || []).filter(x => x.i <= k).sort((a, b) => a.i - b.i).at(-1);
    return e ? `${e.big ? '큰 흐름 ' : ''}${e.up ? '위로' : '아래로'} ${e.kind}` : '-';
}
// 캔들 위 표시: 위로 = 봉 아래 빨강 ▲ · 아래로 = 봉 위 파랑 ▼ (큰 흐름은 글자를 굵게 보이도록 앞에 ■)
function ciSmcMarkers(cell) {
    const out = [];
    for (const ci of cell.inds || []) {
        if (!ci.def.marks || ci.inst.off) continue;
        for (const e of ci.vals.events || []) {
            const t = cell.data[e.i]?.time;
            if (t === undefined) continue;
            out.push({ time: t, position: e.up ? 'belowBar' : 'aboveBar', color: e.up ? CL_UP : CL_DOWN, shape: e.up ? 'arrowUp' : 'arrowDown',
                       text: `${e.big ? '■ ' : ''}${e.kind}`, size: e.big ? 1.4 : 0.8 });
        }
    }
    return out;
}
function clSmcApply(cell) {
    if (!cell.series) return;
    if (cell.ost) { cell.ost.extraMarkers = () => ciSmcMarkers(cell); setSzMarkers(cell.ost); return; }
    const m = ciSmcMarkers(cell).sort((a, b) => a.time - b.time);
    try { cell.series.setMarkers(m); } catch (e) { /* 다시 그리는 중 */ }
}
// 슈퍼트렌드 (트레이딩뷰와 같은 계산: ATR = 와일더 평활, 밴드는 추세 쪽으로만 좁혀짐) → v = 오름일 때 아래 밴드 · 내림일 때 위 밴드, dir = 1 / −1
function ciSupertrend(H, L, C, n, m) {
    const atr = ciWilder(ciTrueRange(H, L, C), n), v = ciNull(C.length), dir = ciNull(C.length);
    let upper = null, lower = null, d = 1;
    for (let i = 0; i < C.length; i++) {
        if (atr[i] === null) continue;
        const hl2 = (H[i] + L[i]) / 2, bu = hl2 + m * atr[i], bl = hl2 - m * atr[i], pc = i > 0 ? C[i - 1] : C[i];
        const nu = upper === null || bu < upper || pc > upper ? bu : upper;
        const nl = lower === null || bl > lower || pc < lower ? bl : lower;
        if (upper !== null) {
            if (d === -1 && C[i] > upper) d = 1;
            else if (d === 1 && C[i] < lower) d = -1;
        }
        upper = nu; lower = nl;
        v[i] = d === 1 ? lower : upper;
        dir[i] = d;
    }
    return { v, dir };
}
function ciTrueRange(H, L, C) { return C.map((_, i) => (i === 0 ? null : Math.max(H[i] - L[i], Math.abs(H[i] - C[i - 1]), Math.abs(L[i] - C[i - 1])))); }
const ciZip = (a, b, f) => a.map((x, i) => (x === null || b[i] === null || x === undefined || b[i] === undefined ? null : f(x, b[i])));

function ciRsi(C, n) {
    const g = ciNull(C.length), l = ciNull(C.length);
    for (let i = 1; i < C.length; i++) { const d = C[i] - C[i - 1]; g[i] = Math.max(d, 0); l[i] = Math.max(-d, 0); }
    const ag = ciWilder(g, n), al = ciWilder(l, n);
    return ag.map((x, i) => (x === null ? null : al[i] === 0 ? (x === 0 ? 50 : 100) : 100 - 100 / (1 + x / al[i])));
}
function ciDmi(H, L, C, n) {
    const pdm = ciNull(C.length), mdm = ciNull(C.length);
    for (let i = 1; i < C.length; i++) {
        const up = H[i] - H[i - 1], dn = L[i - 1] - L[i];
        pdm[i] = up > dn && up > 0 ? up : 0;
        mdm[i] = dn > up && dn > 0 ? dn : 0;
    }
    const tr = ciWilder(ciTrueRange(H, L, C), n), sp = ciWilder(pdm, n), sm = ciWilder(mdm, n);
    const pdi = ciZip(sp, tr, (a, t) => (t ? 100 * a / t : 0)), mdi = ciZip(sm, tr, (a, t) => (t ? 100 * a / t : 0));
    const dx = ciZip(pdi, mdi, (a, b) => (a + b ? 100 * Math.abs(a - b) / (a + b) : 0));
    return { pdi, mdi, adx: ciWilder(dx, n) };
}
function ciSar(H, L, acc, max) {
    const n = H.length, out = ciNull(n);
    if (n < 2) return out;
    let up = H[1] + L[1] >= H[0] + L[0], af = acc, ep = up ? H[1] : L[1], sar = up ? L[0] : H[0];
    out[1] = sar;
    for (let i = 2; i < n; i++) {
        sar = sar + af * (ep - sar);
        if (up) {
            sar = Math.min(sar, L[i - 1], L[i - 2]);
            if (L[i] < sar) { up = false; sar = ep; ep = L[i]; af = acc; }
            else if (H[i] > ep) { ep = H[i]; af = Math.min(max, af + acc); }
        } else {
            sar = Math.max(sar, H[i - 1], H[i - 2]);
            if (H[i] > sar) { up = true; sar = ep; ep = H[i]; af = acc; }
            else if (L[i] < ep) { ep = L[i]; af = Math.min(max, af + acc); }
        }
        out[i] = sar;
    }
    return out;
}

// ── 지표 정의: key · 이름 · 숫자 · 위(가격 위)/아래 칸 · 선들 · 기준선 · 계산 ──
// outs: [{k, name, kind: line|hist|dots, color: 'main' | 색, dash}] — 'main' 은 사용자가 고른 색
const CI_DEFS = [
    { key: 'sma', name: '단순 이동평균', en: 'Simple Moving Average (SMA)', short: 'SMA', params: [{ k: 'timeperiod', name: '기간', def: 20 }], color: '#7c3aed',
      outs: [{ k: 'v', name: 'SMA', color: 'main' }], calc: (d, p) => ({ v: ciSma(d.C, p.timeperiod) }) },
    { key: 'ema', name: '지수 이동평균', en: 'Exponential Moving Average (EMA)', short: 'EMA', params: [{ k: 'timeperiod', name: '기간', def: 20 }], color: '#f59e0b',
      outs: [{ k: 'v', name: 'EMA', color: 'main' }], calc: (d, p) => ({ v: ciEma(d.C, p.timeperiod) }) },
    { key: 'dema', name: '이중 지수 이동평균', en: 'Double Exponential Moving Average (DEMA)', short: 'DEMA', params: [{ k: 'timeperiod', name: '기간', def: 30 }], color: '#0ea5e9',
      outs: [{ k: 'v', name: 'DEMA', color: 'main' }], calc: (d, p) => { const e1 = ciEma(d.C, p.timeperiod), e2 = ciEma(e1, p.timeperiod); return { v: ciZip(e1, e2, (a, b) => 2 * a - b) }; } },
    { key: 'tema', name: '삼중 지수 이동평균', en: 'Triple Exponential Moving Average (TEMA)', short: 'TEMA', params: [{ k: 'timeperiod', name: '기간', def: 30 }], color: '#14b8a6',
      outs: [{ k: 'v', name: 'TEMA', color: 'main' }],
      calc: (d, p) => { const e1 = ciEma(d.C, p.timeperiod), e2 = ciEma(e1, p.timeperiod), e3 = ciEma(e2, p.timeperiod); return { v: e1.map((a, i) => (a === null || e2[i] === null || e3[i] === null ? null : 3 * a - 3 * e2[i] + e3[i])) }; } },
    { key: 'wma', name: '가중 이동평균', en: 'Weighted Moving Average (WMA)', short: 'WMA', params: [{ k: 'timeperiod', name: '기간', def: 30 }], color: '#db2777',
      outs: [{ k: 'v', name: 'WMA', color: 'main' }], calc: (d, p) => ({ v: ciWma(d.C, p.timeperiod) }) },
    { key: 'trima', name: '삼각 이동평균', en: 'Triangular Moving Average (TRIMA)', short: 'TRIMA', params: [{ k: 'timeperiod', name: '기간', def: 30 }], color: '#65a30d',
      outs: [{ k: 'v', name: 'TRIMA', color: 'main' }],
      calc: (d, p) => { const n = p.timeperiod, a = n % 2 ? (n + 1) / 2 : n / 2 + 1, b = n % 2 ? (n + 1) / 2 : n / 2; return { v: ciSma(ciSma(d.C, a), b) }; } },
    { key: 'sar', name: '파라볼릭 SAR', en: 'Parabolic SAR', short: 'SAR', params: [{ k: 'acceleration', name: '가속', def: 0.02, step: 0.01 }, { k: 'maximum', name: '최대', def: 0.2, step: 0.01 }], color: '#ea580c',
      outs: [{ k: 'v', name: 'SAR', color: 'main', kind: 'dots' }], calc: (d, p) => ({ v: ciSar(d.H, d.L, p.acceleration, p.maximum) }) },
    { key: 'supertrend', name: '슈퍼트렌드', en: 'Supertrend', short: 'ST', params: [{ k: 'timeperiod', name: 'ATR 기간', def: 10 }, { k: 'multiplier', name: '배수', def: 3, step: 0.1 }], color: '#c84a31',
      read: '선이 가격 아래(빨강) = 오르는 추세 · 가격 위(파랑) = 내리는 추세 · 색이 바뀌는 곳 = 추세가 바뀐 곳 (가격이 이미 움직인 뒤에 바뀝니다)',
      outs: [{ k: 'v', name: '슈퍼트렌드', kind: 'trend' }],
      calc: (d, p) => ciSupertrend(d.H, d.L, d.C, p.timeperiod, p.multiplier) },
    { key: 'sqzmom', name: '스퀴즈 모멘텀', en: 'Squeeze Momentum Indicator [LazyBear]', short: 'SQZ', params: [{ k: 'length', name: 'BB 기간', def: 20 }, { k: 'mult', name: 'BB 배수', def: 2, step: 0.1 }, { k: 'lengthKC', name: 'KC 기간', def: 20 }, { k: 'multKC', name: 'KC 배수', def: 1.5, step: 0.1 }], color: '#c84a31', pane: true, levels: () => [0],
      read: '막대 = 오르내리는 힘(빨강 위 · 파랑 아래, 진하면 세지는 중) · 가운데 점: 검정 = 스퀴즈(움직임이 잠잠) · 회색 = 막 풀림 · 파랑 = 스퀴즈 없음',
      outs: [{ k: 'val', name: '모멘텀', kind: 'sqz' }, { k: 'dot', name: '스퀴즈', kind: 'sqzdot', noLegend: true }],
      calc: (d, p) => ciSqueeze(d.H, d.L, d.C, p.length, p.mult, p.lengthKC, p.multKC) },
    { key: 'smc', name: '스마트 머니 구조 (BOS · CHoCH)', en: 'Smart Money Concepts [LuxAlgo] — 구조', short: 'SMC', params: [{ k: 'internal', name: '내부 구조 봉', def: 5 }, { k: 'swing', name: '큰 흐름 봉', def: 50 }], color: '#7c3aed',
      read: '종가가 최근 고점 · 저점을 넘으면 표시 — 추세와 같은 쪽 = BOS(이어짐), 반대 쪽 = CHoCH(바뀜) · 굵은 글씨 = 큰 흐름',
      outs: [], marks: true, legend: (v, k) => ciSmcLast(v, k),
      calc: (d, p) => ciSmc(d.H, d.L, d.C, p.internal, p.swing) },
    { key: 'bbands', name: '볼린저 밴드', en: 'Bollinger Bands (BBands)', short: 'BB', params: [{ k: 'timeperiod', name: '기간', def: 20 }, { k: 'nbdevup', name: '위 배수', def: 2, step: 0.1 }, { k: 'nbdevdn', name: '아래 배수', def: 2, step: 0.1 }], color: '#6366f1',
      outs: [{ k: 'upper', name: '위', color: 'main' }, { k: 'middle', name: '가운데', color: '#94a3b8', dash: true }, { k: 'lower', name: '아래', color: 'main' }],
      calc: (d, p) => {
          const n = p.timeperiod, mid = ciSma(d.C, n);
          const sd = d.C.map((_, i) => { if (mid[i] === null) return null; let v = 0; for (let j = i - n + 1; j <= i; j++) v += (d.C[j] - mid[i]) ** 2; return Math.sqrt(v / n); });
          return { upper: ciZip(mid, sd, (m, s) => m + p.nbdevup * s), middle: mid, lower: ciZip(mid, sd, (m, s) => m - p.nbdevdn * s) };
      } },
    { key: 'rsi', name: '상대강도지수', en: 'Relative Strength Index (RSI)', short: 'RSI', params: [{ k: 'timeperiod', name: '기간', def: 14 }], color: '#7c3aed', pane: true, range: [0, 100], levels: () => [70, 30],
      read: '70 위 = 많이 사짐(과열) · 30 아래 = 많이 팔림(침체)',
      outs: [{ k: 'v', name: 'RSI', color: 'main' }], calc: (d, p) => ({ v: ciRsi(d.C, p.timeperiod) }) },
    { key: 'macd', name: 'MACD', en: 'Moving Average Convergence Divergence (MACD)', short: 'MACD', params: [{ k: 'fastperiod', name: '빠른', def: 12 }, { k: 'slowperiod', name: '느린', def: 26 }, { k: 'signalperiod', name: '시그널', def: 9 }], color: '#0f766e', pane: true, levels: () => [0],
      read: 'MACD 선이 시그널 선을 위로 넘으면 오르는 힘, 아래로 넘으면 내리는 힘 · 막대 = 두 선의 차이',
      outs: [{ k: 'hist', name: '차이', kind: 'hist' }, { k: 'macd', name: 'MACD', color: 'main' }, { k: 'signal', name: '시그널', color: '#f59e0b' }],
      calc: (d, p) => { const f = ciEma(d.C, p.fastperiod), s = ciEma(d.C, p.slowperiod), m = ciZip(f, s, (a, b) => a - b), sig = ciEma(m, p.signalperiod); return { macd: m, signal: sig, hist: ciZip(m, sig, (a, b) => a - b) }; } },
    { key: 'stoch', name: '스토캐스틱', en: 'Stochastic (STOCH)', short: 'STOCH', params: [{ k: 'fastk_period', name: '%K 기간', def: 14 }, { k: 'slowk_period', name: '%K 평활', def: 3 }, { k: 'slowd_period', name: '%D 평활', def: 3 }], color: '#2563eb', pane: true, range: [0, 100], levels: () => [80, 20],
      read: '80 위 = 과열 · 20 아래 = 침체 · 바닥에서 %K 가 %D 를 위로 넘으면 반등 신호',
      outs: [{ k: 'k', name: '%K', color: 'main' }, { k: 'd', name: '%D', color: '#f59e0b' }],
      calc: (d, p) => {
          const n = p.fastk_period, raw = d.C.map((c, i) => { if (i < n - 1) return null; const hh = ciHighest(d.H, n, i), ll = ciLowest(d.L, n, i); return hh > ll ? (c - ll) / (hh - ll) * 100 : 50; });
          const k = ciSma(raw, p.slowk_period);
          return { k, d: ciSma(k, p.slowd_period) };
      } },
    { key: 'willr', name: '윌리엄스 %R', en: "Williams' %R (WILLR)", short: '%R', params: [{ k: 'timeperiod', name: '기간', def: 14 }], color: '#9333ea', pane: true, range: [-100, 0], levels: () => [-20, -80],
      read: '−20 위 = 과열 · −80 아래 = 침체',
      outs: [{ k: 'v', name: '%R', color: 'main' }],
      calc: (d, p) => ({ v: d.C.map((c, i) => { if (i < p.timeperiod - 1) return null; const hh = ciHighest(d.H, p.timeperiod, i), ll = ciLowest(d.L, p.timeperiod, i); return hh > ll ? (hh - c) / (hh - ll) * -100 : -50; }) }) },
    { key: 'cci', name: '상품채널지수', en: 'Commodity Channel Index (CCI)', short: 'CCI', params: [{ k: 'timeperiod', name: '기간', def: 20 }], color: '#b45309', pane: true, levels: () => [100, -100],
      read: '+100 위 = 평소보다 많이 오름 · −100 아래 = 평소보다 많이 내림',
      outs: [{ k: 'v', name: 'CCI', color: 'main' }],
      calc: (d, p) => {
          const n = p.timeperiod, tp = d.C.map((c, i) => (d.H[i] + d.L[i] + c) / 3), m = ciSma(tp, n);
          return { v: tp.map((t, i) => { if (m[i] === null) return null; let md = 0; for (let j = i - n + 1; j <= i; j++) md += Math.abs(tp[j] - m[i]); md /= n; return md ? (t - m[i]) / (0.015 * md) : 0; }) };
      } },
    { key: 'adx', name: '평균방향지수', en: 'Average Directional Index (ADX)', short: 'ADX', params: [{ k: 'timeperiod', name: '기간', def: 14 }], color: '#0f172a', pane: true, range: [0, 100], levels: () => [25],
      read: '25 위 = 추세가 뚜렷함 (방향은 +DI · −DI 중 위에 있는 쪽)',
      outs: [{ k: 'adx', name: 'ADX', color: 'main' }, { k: 'pdi', name: '+DI', color: '#c84a31' }, { k: 'mdi', name: '−DI', color: '#1261c4' }],
      calc: (d, p) => ciDmi(d.H, d.L, d.C, p.timeperiod) },
    { key: 'aroon', name: '아룬', en: 'Aroon', short: 'AROON', params: [{ k: 'timeperiod', name: '기간', def: 14 }], color: '#c84a31', pane: true, range: [0, 100], levels: () => [70, 30],
      read: '아룬 위가 높으면 최근 신고가가 잦음(상승), 아룬 아래가 높으면 최근 신저가가 잦음(하락)',
      outs: [{ k: 'up', name: '아룬 위', color: 'main' }, { k: 'down', name: '아룬 아래', color: '#1261c4' }],
      calc: (d, p) => {
          const n = p.timeperiod, up = ciNull(d.C.length), dn = ciNull(d.C.length);
          for (let i = n; i < d.C.length; i++) {
              let hi = i - n, lo = i - n;
              for (let j = i - n; j <= i; j++) { if (d.H[j] >= d.H[hi]) hi = j; if (d.L[j] <= d.L[lo]) lo = j; }
              up[i] = 100 * (n - (i - hi)) / n; dn[i] = 100 * (n - (i - lo)) / n;
          }
          return { up, down: dn };
      } },
    { key: 'obv', name: '거래량 균형', en: 'On Balance Volume (OBV)', short: 'OBV', params: [], color: '#0d9488', pane: true,
      read: '오른 봉의 거래대금은 더하고 내린 봉은 빼서 쌓은 값 · 가격보다 먼저 오르면 돈이 들어온다고 봄',
      outs: [{ k: 'v', name: 'OBV', color: 'main' }],
      calc: (d) => { let s = 0; return { v: d.C.map((c, i) => { if (i > 0) s += c > d.C[i - 1] ? d.V[i] : c < d.C[i - 1] ? -d.V[i] : 0; return s; }) }; } },
    { key: 'mfi', name: '자금흐름지수', en: 'Money Flow Index (MFI)', short: 'MFI', params: [{ k: 'timeperiod', name: '기간', def: 14 }], color: '#15803d', pane: true, range: [0, 100], levels: () => [80, 20],
      read: '거래대금을 넣은 RSI · 80 위 = 과열 · 20 아래 = 침체',
      outs: [{ k: 'v', name: 'MFI', color: 'main' }],
      calc: (d, p) => {
          const n = p.timeperiod, tp = d.C.map((c, i) => (d.H[i] + d.L[i] + c) / 3), out = ciNull(d.C.length);
          for (let i = n; i < d.C.length; i++) {
              let pos = 0, neg = 0;
              for (let j = i - n + 1; j <= i; j++) { const f = tp[j] * (d.V[j] / (d.C[j] || 1)); if (tp[j] > tp[j - 1]) pos += f; else if (tp[j] < tp[j - 1]) neg += f; }
              out[i] = neg === 0 ? 100 : 100 - 100 / (1 + pos / neg);
          }
          return { v: out };
      } },
    { key: 'atr', name: '평균 진폭', en: 'Average True Range (ATR)', short: 'ATR', params: [{ k: 'timeperiod', name: '기간', def: 14 }], color: '#475569', pane: true,
      read: '봉마다 움직인 폭의 평균 · 클수록 가격이 크게 흔들림',
      outs: [{ k: 'v', name: 'ATR', color: 'main' }], calc: (d, p) => ({ v: ciWilder(ciTrueRange(d.H, d.L, d.C), p.timeperiod) }) },
    { key: 'trix', name: 'TRIX', en: 'Triple Exponential Average (TRIX)', short: 'TRIX', params: [{ k: 'timeperiod', name: '기간', def: 30 }], color: '#be185d', pane: true, levels: () => [0],
      read: '세 번 평활한 평균의 변화율(%) · 0 위 = 오르는 흐름 · 0 아래 = 내리는 흐름',
      outs: [{ k: 'v', name: 'TRIX', color: 'main' }],
      calc: (d, p) => { const e = ciEma(ciEma(ciEma(d.C, p.timeperiod), p.timeperiod), p.timeperiod); return { v: e.map((x, i) => (x === null || !e[i - 1] ? null : (x / e[i - 1] - 1) * 100)) }; } },
    { key: 'roc', name: '변화율', en: 'Rate of Change (ROC)', short: 'ROC', params: [{ k: 'timeperiod', name: '기간', def: 10 }], color: '#0369a1', pane: true, levels: () => [0],
      read: 'N봉 전보다 몇 % 올랐는지 · 0 위 = 올랐음',
      outs: [{ k: 'v', name: 'ROC', color: 'main' }], calc: (d, p) => ({ v: d.C.map((c, i) => (i < p.timeperiod ? null : (c / d.C[i - p.timeperiod] - 1) * 100)) }) },
];
const CI_BY = Object.fromEntries(CI_DEFS.map(d => [d.key, d]));
const CI_PALETTE = ['#7c3aed', '#f59e0b', '#0ea5e9', '#db2777', '#16a34a', '#ea580c', '#6366f1', '#0f766e', '#b45309', '#475569'];

// 켠 지표 하나: {id, key, params, color}
function ciParams(inst) {
    const def = CI_BY[inst.key];
    return Object.fromEntries((def?.params || []).map(q => [q.k, Number.isFinite(Number(inst.params?.[q.k])) ? Number(inst.params[q.k]) : q.def]));
}
function ciLabel(inst) {
    const def = CI_BY[inst.key], p = ciParams(inst), v = Object.values(p);
    return `${def?.short || inst.key}${v.length ? ` ${v.join(' · ')}` : ''}`;
}
// 봉 배열 → 이 지표의 선들 {k: [값...]}
function ciCompute(inst, data, vals) {
    const def = CI_BY[inst.key];
    if (!def) return {};
    const d = { H: data.map(x => x.high), L: data.map(x => x.low), C: data.map(x => x.close), V: vals || data.map(() => 0) };
    try { return def.calc(d, ciParams(inst)); } catch (e) { return {}; }
}
// 슈퍼트렌드 선 색: 오름 빨강 · 내림 파랑 — 선 한 토막은 앞 점의 색이라, 추세가 바뀌기 바로 앞 봉을 투명으로 (밴드를 건너뛰는 선을 숨김)
function ciTrendColor(dir, i) { return dir[i + 1] !== undefined && dir[i + 1] !== null && dir[i + 1] !== dir[i] ? 'rgba(0,0,0,0)' : dir[i] === 1 ? CL_UP : CL_DOWN; }
function ciTrendPts(data, vals) { return data.map((x, i) => (vals.v[i] === null ? { time: x.time } : { time: x.time, value: vals.v[i], color: ciTrendColor(vals.dir, i) })); }
// 지표 선 두께 (지표마다 따로, 기본 1.5) — 2026-10-16
const CI_WIDTH_DEF = 1.5;
const CI_WIDTHS = [[1, '가늘게 (1)'], [1.5, '보통 (1.5)'], [2, '조금 굵게 (2)'], [3, '굵게 (3)'], [4, '아주 굵게 (4)']];
function ciColor(inst, out) { return out.color === 'main' || !out.color ? (inst.color || CI_BY[inst.key]?.color || '#7c3aed') : out.color; }
function ciFmt(def, v) {
    if (v === null || v === undefined || !Number.isFinite(v)) return '-';
    if (!def.pane) return formatPrice(v).replace('원', '');
    if (def.key === 'obv') {                          // 쌓인 거래대금: 억 · 만 (음수 가능)
        const a = Math.abs(v), sg = v < 0 ? '−' : '';
        return a >= 1e8 ? `${sg}${(a / 1e8).toLocaleString(undefined, { maximumFractionDigits: 1 })}억` : a >= 1e4 ? `${sg}${Math.round(a / 1e4).toLocaleString()}만` : `${sg}${Math.round(a)}`;
    }
    const a = Math.abs(v);
    return a >= 1000 ? Math.round(v).toLocaleString() : a >= 1 ? v.toFixed(2) : String(Number(v.toPrecision(3)));
}

// 처음 쓰는 사람: 지금까지의 '평균선 · 볼린저 · RSI · MACD' 단추 설정을 지표 목록으로 옮김
function ciMigrate(ind) {
    let n = 0;
    const id = () => `ci${Date.now().toString(36)}${n++}`;
    const out = [];
    const old = ind || { ma: true, rsi: true };
    if (old.ma) [[5, '#f59e0b'], [20, '#7c3aed'], [60, '#0ea5e9']].forEach(([p, c]) => out.push({ id: id(), key: 'sma', params: { timeperiod: p }, color: c }));
    if (old.bb) out.push({ id: id(), key: 'bbands', params: { timeperiod: 20, nbdevup: 2, nbdevdn: 2 }, color: '#6366f1' });
    if (old.rsi) out.push({ id: id(), key: 'rsi', params: { timeperiod: 14 }, color: '#7c3aed' });
    if (old.macd) out.push({ id: id(), key: 'macd', params: { fastperiod: 12, slowperiod: 26, signalperiod: 9 }, color: '#0f766e' });
    return out;
}

// ── 지표 고르기 · 숫자 · 색 창 (StraHub '전략 선택' 과 같은 흐름: 고르기 → 숫자 · 색 → 적용) ──
let ciEdit = null;            // 고치는 중인 지표 id (null = 새로 넣기)
function ciModal() {
    let el = document.getElementById('cl-ind-modal');
    if (el) return el;
    el = document.createElement('div');
    el.className = 'modal fade';
    el.id = 'cl-ind-modal';
    el.tabIndex = -1;
    el.innerHTML = `<div class="modal-dialog modal-dialog-scrollable cl-ind-dialog"><div class="modal-content">
        <div class="modal-header"><h5 class="modal-title" id="cl-ind-title">보조 지표 고르기</h5><button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="닫기"></button></div>
        <div class="modal-body" id="cl-ind-body"></div>
        <div class="modal-footer d-none" id="cl-ind-foot"></div></div></div>`;
    document.body.appendChild(el);
    el.addEventListener('input', (e) => { if (e.target.id === 'cl-ind-q') ciRenderList(e.target.value); });
    el.addEventListener('input', (e) => {                      // 색을 바꾸면 두께 견본 선도 같은 색으로
        if (e.target.matches('[data-ci-color]')) el.querySelectorAll('[data-ci-w]').forEach(b => b.style.setProperty('--c', e.target.value));
    });
    el.addEventListener('click', (e) => {
        const pick = e.target.closest('[data-ci-pick]');
        if (pick) { ciRenderForm({ key: pick.dataset.ciPick }); return; }
        if (e.target.closest('[data-ci-back]')) { ciEdit = null; ciRenderList(''); return; }
        if (e.target.closest('[data-ci-apply]')) { ciApplyForm(); return; }
        if (e.target.closest('[data-ci-del]')) { clIndRemove(ciEdit); bootstrap.Modal.getInstance(el)?.hide(); return; }
        const wb = e.target.closest('[data-ci-w]');               // 두께: 실제 굵기로 그린 선을 눌러 고름
        if (wb) {
            el.querySelectorAll('[data-ci-w]').forEach(b => { const on = b === wb; b.classList.toggle('is-on', on); b.setAttribute('aria-checked', String(on)); });
            const h = el.querySelector('[data-ci-width]'); if (h) h.value = wb.dataset.ciW;
            return;
        }
        const reset = e.target.closest('[data-ci-reset]');
        if (reset) { const def = CI_BY[reset.dataset.ciReset]; el.querySelectorAll('[data-ci-param]').forEach(i => { i.value = def.params.find(q => q.k === i.dataset.ciParam)?.def ?? i.value; }); el.querySelector('[data-ci-color]').value = def.color; const w = el.querySelector('[data-ci-width]'); if (w) w.value = String(CI_WIDTH_DEF);
            el.querySelectorAll('[data-ci-w]').forEach(b => { const on = Number(b.dataset.ciW) === CI_WIDTH_DEF; b.classList.toggle('is-on', on); b.setAttribute('aria-checked', String(on)); b.style.setProperty('--c', def.color); }); }
    });
    el.addEventListener('keydown', (e) => { if (e.key === 'Enter' && e.target.matches('[data-ci-param]')) { e.preventDefault(); ciApplyForm(); } });
    return el;
}
function ciOpenPicker() {
    ciEdit = null;
    const el = ciModal();
    ciRenderList('');
    bootstrap.Modal.getOrCreateInstance(el).show();
    el.addEventListener('shown.bs.modal', () => el.querySelector('#cl-ind-q')?.focus(), { once: true });
}
function ciOpenEdit(id) {
    const inst = (CL.inds || []).find(x => x.id === id);
    if (!inst) return;
    ciEdit = id;
    const el = ciModal();
    ciRenderForm(inst);
    bootstrap.Modal.getOrCreateInstance(el).show();
}
function ciRenderList(q) {
    const el = ciModal(), body = el.querySelector('#cl-ind-body');
    el.querySelector('#cl-ind-title').textContent = '보조 지표 고르기';
    el.querySelector('#cl-ind-foot').classList.add('d-none');
    const s = String(q || '').trim().toLowerCase();
    const list = CI_DEFS.filter(d => !s || `${d.name} ${d.en} ${d.short}`.toLowerCase().includes(s));
    const used = new Map();
    (CL.inds || []).forEach(x => used.set(x.key, (used.get(x.key) || 0) + 1));
    const row = (d) => `<button type="button" class="ci-row" data-ci-pick="${d.key}"><span class="ci-row-name"><b>${escapeHtml(d.name)}</b><small>${escapeHtml(d.en)}</small></span>`
        + `<span class="ci-row-where ${d.pane ? 'is-pane' : ''}">${d.pane ? '아래 칸' : '가격 위'}</span>${used.get(d.key) ? `<span class="ci-row-on">${used.get(d.key)}개 사용 중</span>` : ''}<i class="bi bi-chevron-right"></i></button>`;
    const over = list.filter(d => !d.pane), pane = list.filter(d => d.pane);
    if (!body.querySelector('#cl-ind-q')) {
        body.innerHTML = `<div class="ci-search"><i class="bi bi-search"></i><input type="search" id="cl-ind-q" class="form-control" placeholder="지표 이름 · 영문 이름 찾기" autocomplete="off"></div><div id="cl-ind-list"></div>`;
    }
    const box = body.querySelector('#cl-ind-list');
    box.innerHTML = !list.length ? '<div class="text-muted small p-3">찾는 지표가 없습니다.</div>'
        : (over.length ? `<div class="ci-group">가격 위에 그리는 지표</div>${over.map(row).join('')}` : '')
        + (pane.length ? `<div class="ci-group">아래 칸에 그리는 지표</div>${pane.map(row).join('')}` : '');
}
function ciRenderForm(inst) {
    const def = CI_BY[inst.key];
    if (!def) return;
    const el = ciModal(), body = el.querySelector('#cl-ind-body'), foot = el.querySelector('#cl-ind-foot');
    const p = ciParams(inst);
    // 같은 지표를 또 넣으면 아직 안 쓴 색으로 (SMA 20 · SMA 60 처럼)
    const usedColors = new Set((CL.inds || []).map(x => x.color));
    const color = inst.color || ((CL.inds || []).some(x => x.key === def.key) ? CI_PALETTE.find(c => !usedColors.has(c)) || def.color : def.color);
    const editing = !!ciEdit;
    el.querySelector('#cl-ind-title').textContent = editing ? `${def.name} 고치기` : `${def.name} 넣기`;
    body.innerHTML = `${editing ? '' : '<button type="button" class="btn btn-link btn-sm px-0 mb-2" data-ci-back><i class="bi bi-chevron-left"></i> 지표 목록</button>'}
        <div class="ci-form-head"><b>${escapeHtml(def.name)}</b><small>${escapeHtml(def.en)} · ${def.pane ? '아래 칸에 그립니다' : '가격 위에 그립니다'}</small></div>
        ${def.read ? `<div class="ci-read"><i class="bi bi-lightbulb"></i> ${escapeHtml(def.read)}</div>` : ''}
        <div class="ci-form" data-ci-key="${def.key}">
            ${def.params.map(q => `<label class="ci-field"><span>${escapeHtml(q.name)}</span><input type="number" class="form-control form-control-sm" data-ci-param="${q.k}" value="${p[q.k]}" step="${q.step || 1}" min="${q.step ? q.step : 1}"></label>`).join('')}
            <label class="ci-field"><span>색</span><input type="color" class="form-control form-control-color" data-ci-color value="${color}"></label>
            <div class="ci-field ci-field-wide"><span>두께</span><input type="hidden" data-ci-width value="${Number(inst.width || CI_WIDTH_DEF)}">
                <div class="ci-width-pick" role="radiogroup" aria-label="선 두께">${CI_WIDTHS.map(([v, n]) => `<button type="button" class="ci-w${Number(inst.width || CI_WIDTH_DEF) === v ? ' is-on' : ''}" data-ci-w="${v}" role="radio" aria-checked="${Number(inst.width || CI_WIDTH_DEF) === v}" title="${n}" style="--w:${v}px;--c:${color}"><i></i></button>`).join('')}</div></div>
        </div>
        ${def.params.length ? `<button type="button" class="btn btn-link btn-sm px-0" data-ci-reset="${def.key}">기본값으로</button>` : ''}`;
    foot.classList.remove('d-none');
    foot.innerHTML = `${editing ? '<button type="button" class="btn btn-outline-danger me-auto" data-ci-del><i class="bi bi-trash"></i> 지우기</button>' : ''}
        <button type="button" class="btn btn-light" data-bs-dismiss="modal">닫기</button>
        <button type="button" class="btn btn-primary" data-ci-apply>${editing ? '바꾸기' : '차트에 넣기'}</button>`;
    body.querySelector('[data-ci-param]')?.focus();
}
function ciApplyForm() {
    const el = ciModal(), form = el.querySelector('.ci-form');
    if (!form) return;
    const key = form.dataset.ciKey, def = CI_BY[key], params = {};
    for (const i of form.querySelectorAll('[data-ci-param]')) {
        const v = Number(i.value), q = def.params.find(x => x.k === i.dataset.ciParam);
        if (!Number.isFinite(v) || v <= 0 || (!q.step && (v < 1 || v > 500))) {
            showNotification(`${q.name} 숫자를 다시 정해 주십시오.`, 'warning');
            i.focus();
            return;
        }
        params[q.k] = q.step ? v : Math.round(v);
    }
    const color = form.querySelector('[data-ci-color]').value;
    const width = Number(form.querySelector('[data-ci-width]')?.value) || CI_WIDTH_DEF;
    if (ciEdit) {
        const inst = CL.inds.find(x => x.id === ciEdit);
        if (inst) Object.assign(inst, { params, color, width });
    } else {
        CL.inds = [...(CL.inds || []), { id: `ci${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`, key, params, color, width }];
    }
    bootstrap.Modal.getInstance(el)?.hide();
    clBuild();
}
function clIndRemove(id) {
    CL.inds = (CL.inds || []).filter(x => x.id !== id);
    clBuild();
}

const CL_KST = 9 * 3600;
const CL_UP = '#c84a31', CL_DOWN = '#1261c4';
const CL_TF_DEFAULT = { 4: ['15', '60', '240', 'day'], 9: ['1', '5', '15', '30', '60', '240', 'day', 'week', 'month'] };
const CL_UNIT_NAME = Object.fromEntries(SZ_UNITS);
const clUnitLabel = (u) => ({ day: '일봉', week: '주봉', month: '월봉' }[u] || `${CL_UNIT_NAME[u] || u}봉`);

const CL = Object.assign({
    mode: 'single',           // single 한 화면 · tf 분봉 비교 · coin 코인 비교
    split: 4,
    unit: '60',
    sym: 'KRW-BTC',
    coins: [],                // 코인 비교 칸마다 코인 (KRW-…)
    tfUnits: { 4: [...CL_TF_DEFAULT[4]], 9: [...CL_TF_DEFAULT[9]] },
    ind: { vol: true, ma: true, bb: false, rsi: true, macd: false },     // vol = 거래대금 막대 (나머지는 예전 단추 — 처음 한 번 inds 로 옮김)
    inds: null,               // 보조 지표 [{id, key, params, color}] (chart_lab_ind.js)
    active: 0,                // 코인 비교에서 목록을 누르면 바뀔 칸
    list: 'krw', sort: 'value', sortDir: 'desc',     // 코인 목록: 머리줄을 누르면 그 칸으로 정렬 (같은 칸을 또 누르면 반대로)
}, (() => { try { return JSON.parse(localStorage.getItem('strabot.chartLab') || '{}'); } catch (e) { return {}; } })());
if (!Array.isArray(CL.inds)) CL.inds = ciMigrate(CL.ind);
let clCells = [];
let clCoins = [];             // /api/markets 의 원화 코인
let clCoinsAt = 0;
let clHeld = new Set();
let clQuery = '';
let clTimer = null;
let clTickN = 0;
let clStarted = false;
let clFav = (() => { try { return new Set(JSON.parse(localStorage.getItem('strabot.chartLab.fav') || '[]')); } catch (e) { return new Set(); } })();

function clSave() {
    try {
        const { mode, split, unit, sym, coins, tfUnits, ind, inds, active, list, sort, sortDir } = CL;
        localStorage.setItem('strabot.chartLab', JSON.stringify({ mode, split, unit, sym, coins, tfUnits, ind, inds, active, list, sort, sortDir }));
    } catch (e) { /* 저장 불가: 무시 */ }
}
// 차트 주문 패널 접기 · 펴기 (접으면 차트가 그만큼 넓어짐 · 접혀 있어도 클릭 주문이 켜져 있으면 띠에 표시)
let clPat = { on: [], range: 'off' };      // 차트 분석 패턴 태그: 켠 것 · 예전 신호 기간 (코인 · 봉을 바꿔도 이어감)
let clOrdHidden = (() => { try { return localStorage.getItem('strabot.chartLab.ordHidden') === '1'; } catch (e) { return false; } })();
function renderClOrdToggle() {
    const b = document.querySelector('#chart-lab [data-cl-ord-toggle]');
    if (!b) return;
    const on = !!szPanel().on;
    b.closest('.cl-cell-row')?.classList.toggle('is-ord-hidden', clOrdHidden);
    b.classList.toggle('is-armed', on);
    b.setAttribute('aria-expanded', String(!clOrdHidden));
    b.title = clOrdHidden ? '차트 주문 패널을 펼칩니다.' : '차트 주문 패널을 접어 차트를 넓게 봅니다.';
    b.innerHTML = clOrdHidden
        ? `<i class="bi bi-chevron-double-right"></i><span>차트 주문</span>${on ? '<em><i class="bi bi-circle-fill"></i> 클릭 주문 켜짐</em>' : ''}`
        : '<i class="bi bi-chevron-double-left"></i>';
}
function clToggleOrd() {
    clOrdHidden = !clOrdHidden;
    try { localStorage.setItem('strabot.chartLab.ordHidden', clOrdHidden ? '1' : '0'); } catch (e) { /* 저장 불가 */ }
    renderClOrdToggle();
}
document.addEventListener('change', (e) => { if (e.target.matches?.('#chart-lab [data-sop-on]')) setTimeout(renderClOrdToggle, 0); });
function clSaveFav() { try { localStorage.setItem('strabot.chartLab.fav', JSON.stringify([...clFav])); } catch (e) { /* 저장 불가 */ } }
const clCoin = (sym) => clCoins.find(c => `KRW-${c.currency}` === sym);
const clName = (sym) => clCoin(sym)?.korean_name || sym.replace('KRW-', '');
const clLogo = (sym, cls = '') => `<img class="cl-logo ${cls}" src="https://static.upbit.com/logos/${encodeURIComponent(sym.replace('KRW-', ''))}.png" alt="" loading="lazy">`;

// ── 화면 뼈대 ──
function clCellCount() { return CL.mode === 'single' ? 1 : CL.split; }
function clCellSym(i) { return CL.mode === 'coin' ? CL.coins[i] : CL.sym; }
function clCellUnit(i) { return CL.mode === 'tf' ? (CL.tfUnits[CL.split] || CL_TF_DEFAULT[CL.split])[i] : CL.unit; }

function clFillCoins() {
    const top = [...clCoins].sort((a, b) => (b.trade_value_24h || 0) - (a.trade_value_24h || 0)).map(c => `KRW-${c.currency}`);
    const out = (CL.coins || []).slice(0, 9);
    for (const s of top) { if (out.length >= 9) break; if (!out.includes(s)) out.push(s); }
    while (out.length < 9) out.push('KRW-BTC');
    CL.coins = out;
}

function renderClBar() {
    const bar = document.getElementById('cl-bar');
    if (!bar) return;
    const seg = (items, cur, attr) => `<span class="tf-seg cl-seg" role="group">${items.map(([v, t]) =>
        `<button type="button" class="${String(v) === String(cur) ? 'active' : ''}" ${attr}="${v}" aria-pressed="${String(v) === String(cur)}">${t}</button>`).join('')}</span>`;
    const grid = CL.mode !== 'single';
    bar.innerHTML = `
        <div class="cl-bar-group"><span class="cl-bar-label">보기</span>${seg([['single', '<i class="bi bi-square"></i> 한 화면'], ['tf', '<i class="bi bi-grid"></i> 분봉 비교'], ['coin', '<i class="bi bi-grid-3x3"></i> 코인 비교']], CL.mode, 'data-cl-mode')}
            ${grid ? seg([[4, '4칸'], [9, '9칸']], CL.split, 'data-cl-split') : ''}</div>
        ${CL.mode === 'tf' ? `<div class="cl-bar-group cl-bar-note"><i class="bi bi-info-circle"></i> 칸마다 위쪽에서 봉을 바꾸실 수 있습니다 <button type="button" class="btn btn-link btn-sm p-0" data-cl-tf-reset>처음 봉으로</button></div>`
            : `<div class="cl-bar-group"><span class="cl-bar-label">봉</span>${seg(SZ_UNITS, CL.unit, 'data-cl-unit')}</div>`}
        <div class="cl-bar-group cl-bar-inds"><span class="cl-bar-label">보조 지표</span><span class="cl-inds">
            <button type="button" class="cl-ind${CL.ind.vol ? ' is-on' : ''}" data-cl-ind="vol" aria-pressed="${!!CL.ind.vol}" title="캔들 아래 거래대금 막대를 켜고 끕니다.">거래대금</button>
            ${(CL.inds || []).filter(x => CI_BY[x.key]).map(x => `<span class="ci-chip${x.off ? ' is-off' : ''}" style="--c:${x.color || CI_BY[x.key].color}"><button type="button" class="ci-chip-name" data-cl-ind-toggle="${x.id}" aria-pressed="${!x.off}" title="${escapeHtml(CI_BY[x.key].name)} — 눌러서 ${x.off ? '다시 보이게 합니다' : '잠깐 숨깁니다 (설정은 그대로)'}"><i></i>${escapeHtml(ciLabel(x))}</button><button type="button" class="ci-chip-set" data-cl-ind-edit="${x.id}" title="기간 · 색 · 두께 설정" aria-label="${escapeHtml(ciLabel(x))} 설정"><i class="bi bi-gear"></i></button><button type="button" class="ci-chip-x" data-cl-ind-del="${x.id}" title="이 지표를 뺍니다." aria-label="${escapeHtml(ciLabel(x))} 빼기"><i class="bi bi-x"></i></button></span>`).join('')}
            ${clCondInds().map(x => `<span class="ci-chip is-cond" style="--c:${x.color}" title="오른쪽 목록에서 고른 조건 '${escapeHtml(clCondPreset()?.title || '')}'이 보는 지표입니다. 조건을 풀면(✕) 사라집니다."><span class="ci-chip-name"><i></i>${escapeHtml(ciLabel(x))}</span><span class="ci-cond-tag">조건</span></span>`).join('')}
            <button type="button" class="ci-add" data-cl-ind-add><i class="bi bi-plus-lg"></i> 지표 넣기</button></span></div>`;
}

// 코인 · 봉을 바꿀 때: 새 캔들을 먼저 받아 둔 뒤 바꿔 그림 → 받는 동안 이전 차트가 그대로 보여 흰 화면 · '불러오는 중'이 번쩍이지 않음 (2026-10-16)
// 처음 열 때(그려 둔 차트가 없을 때)는 지금처럼 바로 칸을 만들고 '불러오는 중'을 보여 줌. 빨리 여러 번 누르면 마지막 것만 그림
let clBuildSeq = 0;
function clBuild() {
    const seq = ++clBuildSeq;
    if (!clCells.some(c => c.chart)) { clBuildNow(); return; }
    if (CL.mode === 'coin') clFillCoins();
    const jobs = [];
    for (let i = 0; i < clCellCount(); i++) jobs.push(clCandles(clCellSym(i), clCellUnit(i)));
    if (jobs.every(j => j.done)) { clBuildNow(); return; }      // 모두 기억칸에 있음: 바로
    renderClBar(); renderClList();                              // 누른 줄 · 단추 표시는 바로
    Promise.all(jobs.map(j => j.p)).then(() => { if (seq === clBuildSeq) clBuildNow(); });
}

// 캔들 기억칸 (코인|봉 → 15초): 서버 캐시와 같은 시간이라 오래된 봉을 그리지 않음. 받는 중인 요청도 같이 써서 두 번 보내지 않음. 실패는 기억하지 않음
const CL_CANDLE_TTL = 15000;
const clCandleMem = new Map();
function clCandles(sym, unit, fresh = false) {
    const k = `${sym}|${unit}`, hit = clCandleMem.get(k);
    if (!fresh && hit && Date.now() - hit.at < CL_CANDLE_TTL) return hit;
    const ent = { at: Date.now(), done: false, v: null };
    ent.p = fetch(`/api/candles?market=${encodeURIComponent(sym)}&unit=${unit}`).then(r => r.json())
        .then(r => (r.success && r.candles?.length ? r.candles : null)).catch(() => null)
        .then(v => { ent.done = true; ent.v = v; if (!v && clCandleMem.get(k) === ent) clCandleMem.delete(k); return v; });
    clCandleMem.set(k, ent);
    if (clCandleMem.size > 60) clCandleMem.delete(clCandleMem.keys().next().value);   // 오래된 것부터 정리
    return ent;
}

function clBuildNow() {
    const grid = document.getElementById('cl-grid');
    if (!grid) return;
    clCells.forEach(clRemoveCharts);
    clCells = [];
    if (CL.mode === 'coin') clFillCoins();
    const n = clCellCount();
    grid.className = `cl-grid is-${CL.mode === 'single' ? 'single' : `g${CL.split}`}`;
    grid.innerHTML = '';
    for (let i = 0; i < n; i++) {
        const el = document.createElement('div');
        el.className = `cl-cell${CL.mode === 'coin' && i === CL.active ? ' is-active' : ''}`;
        el.dataset.clCell = i;
        el.innerHTML = `<div class="cl-cell-head"></div><div class="cl-cell-legend"></div>${CL.mode === 'single'
            ? `<div class="sz-pat-tags cl-pat-tags"></div><div class="cl-cell-row${clOrdHidden ? ' is-ord-hidden' : ''}"><div class="cl-cell-chart"></div>
                <button type="button" class="cl-ord-toggle" data-cl-ord-toggle aria-controls="cl-ord-panel"></button>
                <aside class="sz-ord-panel cl-ord-panel" id="cl-ord-panel" aria-label="차트 주문"></aside></div>`
            : '<div class="cl-cell-chart"></div>'}`;
        grid.appendChild(el);
        const cell = { i, el, sym: clCellSym(i), unit: clCellUnit(i), head: el.querySelector('.cl-cell-head'), legend: el.querySelector('.cl-cell-legend'),
                       chartEl: el.querySelector('.cl-cell-chart'), panelEl: el.querySelector('.cl-ord-panel'), patBox: el.querySelector('.cl-pat-tags'), small: CL.mode !== 'single' };
        clCells.push(cell);
        renderClHead(cell);
        clLoad(cell);
        clLoadHold(cell);
    }
    renderClBar();
    renderClList();
    renderClOrdToggle();
    clSave();
}

function renderClHead(cell) {
    const c = clCoin(cell.sym);
    const price = cell.price ?? c?.current_price, chg = cell.chg ?? c?.change_rate;
    const cur = cell.sym.replace('KRW-', '');
    const unitSel = CL.mode === 'tf'
        ? `<select class="form-select form-select-sm cl-unit-sel" data-cl-cell-unit="${cell.i}" aria-label="봉">${SZ_UNITS.map(([v]) => `<option value="${v}"${v === cell.unit ? ' selected' : ''}>${clUnitLabel(v)}</option>`).join('')}</select>`
        : `<span class="cl-unit-tag">${clUnitLabel(cell.unit)}</span>`;
    // StraHub(MAYAi 그룹의 무료 정보 사이트)의 이 코인 지표 현황 — 새 창
    const hub = `<a class="cl-hub-link" href="https://app.strahub.com/coins/${encodeURIComponent(cur)}?ref=strabot-app" target="_blank" rel="noopener" title="StraHub에서 이 코인의 지표 현황과 계산식을 봅니다 (새 창)"><i class="bi bi-bar-chart-line"></i> StraHub에서 지표 보기 <i class="bi bi-box-arrow-up-right"></i></a>`;
    const tools = hub + (CL.mode === 'single' ? ''
        : `<button type="button" class="btn btn-sm pnl-expand cl-big-btn" data-cl-big="${cell.i}" title="이 차트를 한 화면으로 크게 봅니다." aria-label="한 화면으로 크게 보기"><i class="bi bi-arrows-angle-expand"></i></button>`);
    cell.head.innerHTML = `<span class="cl-coin">${clLogo(cell.sym)}<b>${escapeHtml(clName(cell.sym))}</b><small>${escapeHtml(cur)}</small></span>
        ${unitSel}
        <span class="cl-price">${price ? formatPrice(price) : ''}</span>
        <span class="cl-chg ${pnlClass(chg || 0)}">${chg === undefined || chg === null ? '' : formatChangeRate(chg)}</span>
        ${CL.mode === 'single' && c?.trade_value_24h ? `<span class="cl-val"><small>24시간 거래대금</small> ${formatTradeValue(c.trade_value_24h)}</span>` : ''}
        <span class="${CL.mode === 'single' ? 'cl-hold-box' : 'cl-hold'} d-none"></span>
        <span class="cl-head-tools">${tools}</span>`;
    cell.holdEl = cell.head.querySelector('.cl-hold-box, .cl-hold');     // 한 화면: 머리줄 오른쪽 보유 칸 · 나눈 칸: 수익률 배지
    clRenderHold(cell);
}

// ── 캔들 받기 · 그리기 ──
async function clLoad(cell, keep = false) {
    const my = cell.loadSeq = (cell.loadSeq || 0) + 1;      // 같은 칸의 마지막 요청만 그림 (봉을 빠르게 바꾸면 먼저 온 옛 봉이 덮어쓰던 것, 2026-10-17)
    cell.loading = true;
    if (!keep) {
        clRemoveCharts(cell);
        cell.data = null;
    }
    // 기억칸에 있으면 바로 (keep = 새 봉 때 다시 받기 → 늘 새로 받음)
    const job = clCandles(cell.sym, cell.unit, keep);
    if (!keep && !job.done) cell.chartEl.innerHTML = '<div class="cl-empty"><span class="spinner-border spinner-border-sm"></span> 캔들을 불러오는 중입니다</div>';
    let candles = job.done ? job.v : await job.p;
    if (cell.loadSeq !== my) return;                        // 그 사이 다른 봉 · 코인을 골랐으면 버림
    candles = candles ? candles.slice() : null;            // 기억칸 배열은 그대로 두고 복사본에 이어 붙임
    cell.loading = false;
    if (!clCells.includes(cell)) return;
    if (!candles?.length || typeof LightweightCharts === 'undefined') {
        if (!keep) cell.chartEl.innerHTML = '<div class="cl-empty">캔들을 받지 못했습니다. 잠시 뒤 다시 시도해 주십시오.</div>';
        return;
    }
    const range = keep && cell.chart ? cell.chart.timeScale().getVisibleLogicalRange() : null;
    if (keep && cell.data?.length) {                       // 화면에서 먼저 이어 그린 새 봉은 서버에 아직 없으면 그대로 둠
        const lastServer = candles[candles.length - 1][0] + CL_KST;
        cell.data.filter(b => b.time > lastServer).forEach(b => candles.push([b.time - CL_KST, b.open, b.high, b.low, b.close, 0]));
    }
    clDraw(cell, candles, range);
}

// 칸 안: 위 캔들(+거래대금) · 아래 RSI · MACD 를 따로 붙인 작은 차트로 (가로 움직임 · 십자선은 함께)
// 아래 칸 보이기 · 숨기기만 다시 맞춤: 칸 높이 비율 · 맨 아래 보이는 칸에만 시간 눈금 (차트는 다시 만들지 않음)
function clPaneLayout(cell) {
    const el = cell.chartEl;
    if (!el) return;
    const subs = [...el.querySelectorAll('.cl-pane-sub')];
    const offIds = new Set((CL.inds || []).filter(x => x.off).map(x => x.id));
    const vis = subs.filter(p => !offIds.has(p.dataset.pane));
    const basis = Math.min(cell.small ? 24 : 20, (cell.small ? 50 : 56) / Math.max(1, vis.length));
    subs.forEach(p => { p.style.display = offIds.has(p.dataset.pane) ? 'none' : ''; p.style.flexBasis = `${basis}%`; });
    const panesAll = [...el.querySelectorAll('.cl-pane')];
    const lastVis = vis[vis.length - 1] || panesAll[0];
    panesAll.forEach((p, i) => { try { cell.charts[i]?.applyOptions({ timeScale: { visible: p === lastVis } }); } catch (e) { /* 지운 차트 */ } });
}
function clChartOpts(el, small, showTime, intraday, fmt, sub = false) {     // sub: 아래 RSI · MACD 칸 (TradingView 표시는 위 칸에만)
    return {
        width: el.clientWidth || 600, height: el.clientHeight || 120,
        layout: { background: { color: '#ffffff' }, textColor: '#475569', fontSize: small ? 10 : 12, attributionLogo: !sub },
        grid: { vertLines: { color: '#f1f5f9' }, horzLines: { color: '#f1f5f9' } },
        timeScale: { visible: showTime, timeVisible: intraday, secondsVisible: false, borderColor: '#e2e8f0', rightOffset: 3 },
        rightPriceScale: { borderColor: '#e2e8f0', minimumWidth: small ? 70 : 96 },
        crosshair: { mode: 0 },
        localization: { priceFormatter: fmt },
    };
}
function clRemoveCharts(cell) {
    if (cell.ost) { szStopLive(cell.ost); if (clOrderSt === cell.ost) clOrderSt = null; cell.ost = null; }
    szCloseConfirm();
    (cell.ros || []).forEach(r => r.disconnect());
    (cell.charts || []).forEach(c => { try { c.remove(); } catch (e) { /* 이미 없음 */ } });
    cell.ros = []; cell.charts = []; cell.chart = null; cell.series = null;
}

function clDraw(cell, candles, range) {
    clRemoveCharts(cell);
    const el = cell.chartEl;
    const intraday = !['day', 'week', 'month'].includes(cell.unit);
    // 사용자 지표 + 고른 조건이 보는 지표(잠시). 가격 위 지표는 끈 것도 숨긴 채 만들어 둠 → 켜고 끌 때 그 선만 보였다 숨김 (2026-10-16)
    // 아래 칸 지표도 끈 것까지 만들어 두고 칸만 숨김 → 켜고 끌 때 차트를 다시 만들지 않고 높이만 바뀜 (2026-10-16)
    const insts = [...(CL.inds || []), ...clCondInds()].filter(x => CI_BY[x.key]);
    const panes = insts.filter(x => CI_BY[x.key].pane), overs = insts.filter(x => !CI_BY[x.key].pane);
    // 아래 칸 높이: 칸이 많으면 나눠서 (위 캔들 칸이 너무 작아지지 않게)
    const shown = panes.filter(x => !x.off);
    const basis = Math.min(cell.small ? 24 : 20, (cell.small ? 50 : 56) / Math.max(1, shown.length));
    el.innerHTML = `<div class="cl-pane cl-pane-main"></div>${panes.map(x => `<div class="cl-pane cl-pane-sub" data-pane="${x.id}" style="flex-basis:${basis}%${x.off ? ';display:none' : ''}"><span class="cl-pane-tag" style="color:${x.color || CI_BY[x.key].color}">${escapeHtml(ciLabel(x))}${cell.small || !CI_BY[x.key].read ? '' : ` <small>${escapeHtml(CI_BY[x.key].read)}</small>`}</span></div>`).join('')}`;
    const priceFmt = (p) => formatPrice(p).replace('원', '');
    const mainEl = el.querySelector('.cl-pane-main');
    const chart = LightweightCharts.createChart(mainEl, clChartOpts(mainEl, cell.small, !shown.length, intraday, priceFmt));
    cell.chart = chart;
    cell.charts = [chart];
    cell.data = candles.map(c => ({ time: c[0] + CL_KST, open: c[1], high: c[2], low: c[3], close: c[4] }));
    cell.vals = candles.map(c => Number(c[5]) || 0);
    const series = chart.addCandlestickSeries({ upColor: CL_UP, downColor: CL_DOWN, borderVisible: false, wickUpColor: CL_UP, wickDownColor: CL_DOWN });
    series.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: CL.ind.vol ? 0.2 : 0.05 } });
    series.setData(cell.data);
    cell.series = series;
    const lineOn = (c, color, extra = {}) => c.addLineSeries({ color, lineWidth: cell.small ? 1 : 1.5, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, ...extra });
    const offOpt = (inst) => (inst.off ? { visible: false } : {});
    // 값이 없는 봉도 자리(빈 점)를 넣어 모든 차트의 봉 순서를 같게
    const pts = (arr) => cell.data.map((x, i) => (arr?.[i] === null || arr?.[i] === undefined || !Number.isFinite(arr[i]) ? { time: x.time } : { time: x.time, value: arr[i] }));
    const histPts = (arr) => cell.data.map((x, i) => (arr?.[i] === null || arr?.[i] === undefined ? { time: x.time }
        : { time: x.time, value: arr[i], color: arr[i] >= 0 ? 'rgba(200,74,49,0.45)' : 'rgba(18,97,196,0.45)' }));
    if (CL.ind.vol) {
        const vol = chart.addHistogramSeries({ priceScaleId: 'vol', priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false });
        chart.priceScale('vol').applyOptions({ scaleMargins: { top: 0.83, bottom: 0 }, visible: false });
        vol.setData(cell.data.map((x, i) => ({ time: x.time, value: cell.vals[i], color: x.close >= x.open ? 'rgba(200,74,49,0.35)' : 'rgba(18,97,196,0.35)' })));
    }
    // 보조 지표: 가격 위 지표는 캔들 차트에, 아래 칸 지표는 칸마다 따로 붙인 차트에
    cell.inds = [];
    const subSeries = [];         // [chart, 십자선을 맞출 선, 그 값]
    const addOuts = (c, inst, def, vals, isPane) => {
        const ser = {};
        let first = null;
        for (const out of def.outs) {
            const color = ciColor(inst, out);
            if (out.kind === 'hist') { ser[out.k] = c.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false }); ser[out.k].setData(histPts(vals[out.k])); continue; }
            if (out.kind === 'sqz') {
                ser[out.k] = c.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
                ser[out.k].setData(cell.data.map((x, i) => (vals.val[i] === null ? { time: x.time } : { time: x.time, value: vals.val[i], color: ciSqzColor(vals.val, i) })));
                continue;
            }
            if (out.kind === 'sqzdot') {
                ser[out.k] = lineOn(c, '#2563eb', { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: cell.small ? 1.5 : 2.2, lastValueVisible: false });
                ser[out.k].setData(cell.data.map((x, i) => (vals.dot[i] === null ? { time: x.time } : { time: x.time, value: 0, color: ciSqzDotColor(vals.sq, i) })));
                if (isPane && !first) first = [ser[out.k], vals.dot];
                continue;
            }
            if (out.kind === 'trend') {
                const tw = Math.max(Number(inst.width) || CI_WIDTH_DEF, 2);
                ser[out.k] = lineOn(c, CL_UP, { lineWidth: cell.small ? Math.max(1.2, tw * 0.7) : tw, ...offOpt(inst) });
                ser[out.k].setData(ciTrendPts(cell.data, vals));
                continue;
            }
            const w = Number(inst.width) || CI_WIDTH_DEF, ws = cell.small ? Math.max(1, w * 0.7) : w;
            const extra = out.kind === 'dots' ? { lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: (cell.small ? 1.2 : 1.8) * Math.max(1, w / CI_WIDTH_DEF) }
                : { lineStyle: out.dash ? 2 : 0, lineWidth: out.dash ? Math.max(1, ws * 0.66) : ws };
            if (isPane && !first) extra.lastValueVisible = true;
            ser[out.k] = lineOn(c, color, { ...extra, ...offOpt(inst) });
            ser[out.k].setData(pts(vals[out.k]));
            if (!first) first = [ser[out.k], vals[out.k]];
        }
        return { ser, first };
    };
    for (const inst of overs) {
        const def = CI_BY[inst.key], vals = ciCompute(inst, cell.data, cell.vals);
        const { ser } = addOuts(chart, inst, def, vals, false);
        cell.inds.push({ inst, def, vals, ser });
    }
    clSmcApply(cell);
    panes.forEach((inst, j) => {
        const def = CI_BY[inst.key], pane = el.querySelector(`[data-pane="${inst.id}"]`);
        const c = LightweightCharts.createChart(pane, clChartOpts(pane, cell.small, inst === shown[shown.length - 1], intraday, (v) => ciFmt(def, v), true));
        cell.charts.push(c);
        const vals = ciCompute(inst, cell.data, cell.vals);
        const { ser, first } = addOuts(c, inst, def, vals, true);
        if (first) {
            const [s0] = first;
            if (def.range) s0.applyOptions({ autoscaleInfoProvider: () => ({ priceRange: { minValue: def.range[0], maxValue: def.range[1] } }) });
            s0.priceScale().applyOptions({ scaleMargins: { top: 0.1, bottom: 0.08 } });
            const lv = def.levels ? def.levels(ciParams(inst)) : [];
            lv.forEach((v, i) => s0.createPriceLine({ price: v, color: lv.length === 1 ? '#94a3b8' : i === 0 ? CL_UP : CL_DOWN, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: '' }));
            subSeries.push([c, s0, first[1]]);
        }
        cell.inds.push({ inst, def, vals, ser });
    });
    // 처음 보이는 봉 수: 한 화면 120개 · 나눈 칸 70개 (마우스 휠로 넓히기)
    const n = cell.data.length, bars = cell.small ? 70 : 120;
    const firstRange = range || { from: Math.max(0, n - bars) - 0.5, to: n + 2 };
    cell.charts.forEach(c => c.timeScale().setVisibleLogicalRange(firstRange));
    // 가로 움직임 맞추기
    let syncing = false;
    cell.charts.forEach(c => c.timeScale().subscribeVisibleLogicalRangeChange((r) => {
        if (syncing || !r || cell.chart !== chart) return;
        syncing = true;
        cell.charts.forEach(o => { if (o !== c) o.timeScale().setVisibleLogicalRange(r); });
        syncing = false;
        if (cell.ost) requestAnimationFrame(() => drawPatternOverlay(cell.ost));
    }));
    // 십자선 맞추기 (한 곳에 올리면 다른 칸에도 같은 봉 세로선) · 위 값 줄
    const all = [[chart, series, null], ...subSeries];
    all.forEach(([c]) => c.subscribeCrosshairMove((p) => {
        if (syncing || cell.chart !== chart) return;
        syncing = true;
        const k = p?.time ? cell.data.findIndex(x => x.time === p.time) : -1;
        for (const [o, s, vals] of all) {
            if (o === c || typeof o.setCrosshairPosition !== 'function') continue;
            const v = k < 0 ? null : vals ? vals[k] : cell.data[k].close;
            if (k < 0 || v === null || v === undefined) o.clearCrosshairPosition(); else o.setCrosshairPosition(v, p.time, s);
        }
        syncing = false;
        renderClLegend(cell, p?.time);
        if (c === chart && cell.ost) szGhost(cell.ost, p?.point || null);
    }));
    cell.ros = [...el.querySelectorAll('.cl-pane')].map((pane, i) => {
        const ro = new ResizeObserver(() => {
            if (pane.clientWidth && cell.charts[i]) cell.charts[i].applyOptions({ width: pane.clientWidth, height: pane.clientHeight });
            if (i === 0 && cell.ost) requestAnimationFrame(() => drawPatternOverlay(cell.ost));
            if (i === 0) clFitPanel(cell);
        });
        ro.observe(pane);
        return ro;
    });
    renderClLegend(cell, null);
    clAvgLine(cell);
    if (CL.mode === 'single') clSingleExtras(cell);
}
// 왼쪽 차트 주문 패널 · 접기 손잡이는 위 캔들 칸 높이까지만 (아래 지표 칸 이름표를 가리지 않게)
function clFitPanel(cell) {
    const panel = cell.panelEl, row = panel?.parentElement, main = cell.chartEl.querySelector('.cl-pane-main');
    if (!panel || !row || !main) return;
    const mainBottom = cell.chartEl.offsetTop + main.offsetTop + main.clientHeight;
    panel.style.bottom = `${Math.max(12, row.clientHeight - mainBottom + 6)}px`;
    const tg = row.querySelector('.cl-ord-toggle');
    if (tg) tg.style.top = `${Math.round((cell.chartEl.offsetTop + main.offsetTop + mainBottom) / 2)}px`;
}
// 실시간: 마지막 봉이 바뀌면 지표를 다시 계산해 끝 점만 고침
function clIndTick(cell) {
    const k = cell.data.length - 1, t = cell.data[k].time;
    for (const ci of cell.inds || []) {
        ci.vals = ciCompute(ci.inst, cell.data, cell.vals);
        for (const out of ci.def.outs) {
            const s = ci.ser[out.k], v = ci.vals[out.k]?.[k];
            if (s && out.kind === 'trend' && ci.vals.dir[k] !== ci.vals.dir[k - 1]) { try { s.setData(ciTrendPts(cell.data, ci.vals)); } catch (e) { /* 다시 그리는 중 */ } continue; }   // 막 바뀌면 앞 봉 색도 고쳐야 해서 전체
            if (!s || v === null || v === undefined || !Number.isFinite(v)) continue;
            try {
                s.update(out.kind === 'hist' ? { time: t, value: v, color: v >= 0 ? 'rgba(200,74,49,0.45)' : 'rgba(18,97,196,0.45)' }
                    : out.kind === 'trend' ? { time: t, value: v, color: ciTrendColor(ci.vals.dir, k) }
                    : out.kind === 'sqz' ? { time: t, value: v, color: ciSqzColor(ci.vals.val, k) }
                    : out.kind === 'sqzdot' ? { time: t, value: 0, color: ciSqzDotColor(ci.vals.sq, k) } : { time: t, value: v });
            } catch (e) { /* 다시 그리는 중 */ }
        }
    }
}

// 마우스를 올린 봉 (벗어나면 마지막 봉): 시가 · 고가 · 저가 · 종가 · 거래대금 · 지표 값
function renderClLegend(cell, time) {
    if (!cell.data?.length) return;
    let k = time === null || time === undefined ? -1 : cell.data.findIndex(x => x.time === time);
    if (k < 0) k = cell.data.length - 1;
    const b = cell.data[k], prev = cell.data[k - 1];
    const chg = prev ? (b.close / prev.close - 1) * 100 : null;
    const it = (name, v, cls = '', color = '') => `<span class="cl-lg"${color ? ` style="--c:${color}"` : ''}>${color ? '<i></i>' : ''}<small>${name}</small><b class="${cls}">${v}</b></span>`;
    const fmt = (v) => (v === null || v === undefined ? '-' : formatPrice(v).replace('원', ''));
    const parts = [];
    if (!cell.small) {
        parts.push(it('시가', fmt(b.open)), it('고가', fmt(b.high), 'pnl-up'), it('저가', fmt(b.low), 'pnl-down'),
                   it('종가', fmt(b.close) + (chg === null ? '' : ` <span class="${pnlClass(chg)}">${formatChangeRate(chg)}</span>`)));
        if (cell.vals[k]) parts.push(it('거래대금', formatTradeValue(cell.vals[k])));
    } else {
        parts.push(it('종가', fmt(b.close) + (chg === null ? '' : ` <span class="${pnlClass(chg)}">${formatChangeRate(chg)}</span>`)));
    }
    if (cell.avg > 0) {
        const last = cell.data.at(-1).close, rate = (last / cell.avg - 1) * 100;
        let where = '';
        try { const y = cell.series.priceToCoordinate(cell.avg); where = y === null ? '' : y < 0 ? ' · 화면 위' : y > cell.chartEl.querySelector('.cl-pane-main').clientHeight ? ' · 화면 아래' : ''; } catch (e) { where = ''; }
        parts.push(it('매수평균', `${fmt(cell.avg)} <span class="${pnlClass(rate)}">(지금 ${formatChangeRate(rate)})</span>${where}`, '', '#16a34a'));
    }
    for (const ci of cell.inds || []) {                 // 보조 지표: 이름(숫자) 값 (선이 여럿이면 / 로)
        if (ci.inst.off) continue;                        // 끈 지표는 범례에서도 뺌
        const outs = ci.def.outs.filter(o => o.kind !== 'hist' && !o.noLegend);
        const text = ci.def.legend ? escapeHtml(ci.def.legend(ci.vals, k)) : outs.map(o => ciFmt(ci.def, ci.vals[o.k]?.[k])).join(' / ');
        parts.push(it(escapeHtml(ciLabel(ci.inst)), text, '', ci.inst.color || ci.def.color));
    }
    if (cell.ost?.patOn?.size) parts.push(szPatternLegend(cell.ost));
    cell.legend.innerHTML = parts.join('');
    const h = cell.legend.offsetHeight;                 // 값 줄 높이는 늘어나기만 (줄 수가 오가며 아래 차트가 들썩이지 않게)
    if (h > (cell.legendMin || 0)) { cell.legendMin = h; cell.legend.style.minHeight = `${h}px`; }
}

// 보유한 코인이면 매수평균가 녹색 선 (모든 칸) — 지금 가격에서 30% 안이면 눈금을 넓혀서라도 늘 보이게,
// 그보다 멀면 차트가 납작해지지 않게 눈금은 그대로 두고 위 값 줄에 '화면 위/아래' 로 알림
function clAvgLine(cell) {
    const series = cell.series;
    const apply = (avg) => {
        cell.avg = avg;
        cell.avgLine = addAvgBuyLine(series, avg, cell.small ? '평균' : '매수평균');
        cell.avgLine?.applyOptions({ lineWidth: 2 });
        const now = cell.data?.at(-1)?.close || avg;
        series.applyOptions({ autoscaleInfoProvider: Math.abs(avg / now - 1) <= 0.3 ? (orig) => {
            const r = orig();
            if (!r?.priceRange) return r;
            return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, avg), maxValue: Math.max(r.priceRange.maxValue, avg) } };
        } : undefined });
    };
    const known = cell.avgSym === cell.sym ? (cell.avg || 0) : 0;     // 같은 코인을 다시 그릴 때(새 봉 · 봉 바꾸기)는 알던 값을 바로
    cell.avg = 0;
    if (known > 0) apply(known);
    cell.avgP = myAvgBuy(cell.sym).catch(() => 0);
    cell.avgP.then(avg => {
        if (cell.series !== series) return;
        cell.avgSym = cell.sym;
        if (avg === known) return;
        if (cell.avgLine) { try { series.removePriceLine(cell.avgLine); } catch (e) { /* 이미 없음 */ } cell.avgLine = null; }
        if (avg > 0) apply(avg);
        else { cell.avg = 0; series.applyOptions({ autoscaleInfoProvider: undefined }); }
        renderClLegend(cell, null);
    });
}

// 한 화면: 매수평균가 선 · 내 거래 ▲▼ · 차트 주문 (코인 차트 창과 같은 오른쪽 패널 · 클릭 예약 · 가로선 · 시장가 바로)
function clSingleExtras(cell) {
    const mainEl = cell.chartEl.querySelector('.cl-pane-main');
    const raw = cell.data.map((x, i) => ({ ...x, value: cell.vals[i] }));
    const st = { host: 'cl', view: 'candle', sym: cell.sym, series: cell.series, chart: cell.chart, data: cell.data, raw, candleEl: mainEl, panelEl: cell.panelEl,
                 patBox: cell.patBox, orders: [], orderLines: [], avgBuy: 0, cmp: null, trades: null, sel: null, patSeries: [],
                 patOn: new Set(clPat.on), patRange: clPat.range,
                 onTrades: () => clTradeMarkers(cell), onPat: () => { clPat = { on: [...st.patOn], range: st.patRange }; renderClLegend(cell, null); } };
    cell.ost = st;
    clOrderSt = st;
    st.extraMarkers = () => ciSmcMarkers(cell);           // 스마트 머니 구조 표시도 패턴 · 거래 표시와 함께
    // 패턴 · 신호 찾기 (코인 차트 창과 같은 계산 · 지금 = 맨 끝 봉에서 나온 것)
    try { st.patAll = scanPatterns(raw, SZ_SCAN_IDS()); } catch (e) { st.patAll = []; }
    st.patCur = new Set(st.patAll.filter(h => h.at === raw.length - 1).map(h => h.id));
    { const vis = new Set(szVisibleIds(st)); st.patOn = new Set([...st.patOn].filter(id => vis.has(id))); }
    renderSzPatBar(st);
    applySzPattern(st);
    cell.chart.subscribeClick((param) => {
        if (cell.ost !== st || !param?.point) return;
        if (String(param.hoveredObjectId || '').startsWith('pat:')) { openCondExplain(String(param.hoveredObjectId).split(':')[1]); return; }   // 패턴 표시(▲ ▼ ●) → 세부 설명
        const price = cell.series.coordinateToPrice(param.point.y);
        if (price > 0) szChartClick(st, param.point, price);
    });
    mainEl.addEventListener('mouseleave', () => szGhost(st, null));
    renderSzOrderPanel(st);
    if (clTimer) szStartLive(st);           // 3초마다 걸어 둔 주문 · 주문 가능 금액 · 현재가 (탭이 보일 때만)
    cell.avgP?.then(avg => {
        if (cell.ost !== st) return;
        st.avgBuy = avg;
        renderSzOrderPanel(st);
    });
    clTradeMarkers(cell);
}
async function clTradeMarkers(cell) {
    const st = cell.ost;
    if (!st) return;
    try {
        const d = await (await fetch(`/api/trade-history/coin?currency=${encodeURIComponent(cell.sym.replace('KRW-', ''))}&chunks=5`)).json();
        if (cell.ost !== st || !d.success) return;
        st.trades = d.rows || [];
        setSzMarkers(st);
        if (st.trades.length) { try { renderSzPatBar(st); } catch (e) { /* 막대 없음 */ } }   // '매수 · 매도 표시' 스위치가 나오게
    } catch (e) { /* API 키가 없는 등: 거래 표시만 빠짐 */ }
}

// ── 실시간: 1초마다 현재가로 마지막 봉 · 지표 끝 값 (새 봉 시간이 되면 캔들을 다시 받음) ──
function clTickCell(cell, price) {
    if (!cell.series || !cell.data?.length || !(price > 0) || cell.loading) return;
    let last = cell.data[cell.data.length - 1];
    const unitSec = SZ_UNIT_SEC[cell.unit] || 86400;
    const nowK = Date.now() / 1000 + CL_KST;
    if (nowK >= last.time + unitSec) {
        // 새 봉 시간: 서버에서 다시 받기는 봉마다 한 번만. 거래가 없는 분은 업비트가 봉을 만들지 않아 다시 받아도 그대로라,
        // 전에는 1초마다 다시 받으며 차트를 통째로 다시 그려 '심장 뛰듯' 출렁였음 (2026-10-15)
        const t = last.time + unitSec * Math.floor((nowK - last.time) / unitSec);
        if (cell.reloadedFor !== t) {
            if (document.querySelector('.sz-ord-confirm')) return;    // 「걸기」확인 상자를 보는 중이면 다시 그리기를 미룸 (상자가 사라지던 것, 2026-10-17)
            cell.reloadedFor = t; clLoad(cell, true); return;
        }
        if (!['day', 'week', 'month'].includes(String(cell.unit))) {     // 분 · 시간봉: 새 봉을 화면에서 먼저 이어 그림 (다음 확인 때 서버 값으로)
            last = { time: t, open: price, high: price, low: price, close: price };
            cell.data.push(last);
            try { cell.series.update(last); } catch (e) { /* 다시 그리는 중 */ }
            clIndTick(cell);
            renderClLegend(cell, null);
            return;
        }
    }
    if (last.close === price) return;
    last.close = price; last.high = Math.max(last.high, price); last.low = Math.min(last.low, price);
    cell.series.update(last);
    const r = cell.ost?.raw?.at(-1);
    if (r) Object.assign(r, { close: last.close, high: last.high, low: last.low });
    clIndTick(cell);
    renderClLegend(cell, null);
}
let clTicking = false;
async function clTick() {
    if (!clCells.length || document.hidden || clTicking) return;
    clTicking = true;
    try { await clTickOnce(); } finally { clTicking = false; }
}
// ── 보유 코인이면: 보유 수량 · 투자금(매수 금액) · 현재 가치 · 손익 (지금 모드 계좌, 현재가가 바뀔 때마다 다시 셈) ──
async function clLoadHold(cell) {
    const cur = cell.sym.replace('KRW-', '');
    let a = null;
    try {
        const d = await fetchHoldingsShared(15000);                // 보유코인 분석과 같은 자료 (15초 안에는 다시 묻지 않음)
        if (d?.success) a = (d.holdings?.assets || []).find(x => x.currency === cur && !x.is_delisted && Number(x.balance) > 0);
    } catch (e) { a = null; }
    if (!clCells.includes(cell)) return;
    cell.hold = a ? { qty: Number(a.balance), avg: Number(a.avg_buy_price) || 0 } : null;
    clRenderHold(cell);
}
function clRenderHold(cell) {
    const el = cell.holdEl;
    if (!el) return;
    const h = cell.hold;
    const price = Number(cell.price ?? cell.data?.at(-1)?.close ?? clCoin(cell.sym)?.current_price) || 0;
    if (!h || !(h.qty > 0) || !price) { el.classList.add('d-none'); el.innerHTML = ''; return; }
    const buy = h.qty * h.avg, now = h.qty * price, pnl = now - buy, rate = buy > 0 ? pnl / buy * 100 : 0, cls = pnlClass(rate);
    const sign = pnl > 0 ? '+' : '';
    el.classList.remove('d-none');
    el.title = `${IS_SIM ? '가상 계좌' : EX_NAME} 보유 · 매수평균가 ${formatPrice(h.avg)} 기준 (수수료 빼기 전, 보유코인 분석과 같은 계산)`;
    el.innerHTML = cell.small
        ? `<i class="bi bi-wallet2"></i><b class="${cls}">${formatChangeRate(rate)}</b>`
        : `<span class="clh-tag"><i class="bi bi-wallet2"></i> 내 보유</span>`
          + `<span class="clh-stat"><small>보유 수량</small><b>${szQtyFmt(h.qty)}</b></span>`
          + `<span class="clh-stat"><small>투자금</small><b>${formatKrw(buy)}</b></span>`
          + `<span class="clh-stat"><small>현재 가치</small><b>${formatKrw(now)}</b></span>`
          + `<span class="clh-stat clh-pnl"><small>손익</small><b class="${cls}">${sign}${formatKrw(pnl)} <span class="clh-rate">${formatChangeRate(rate)}</span></b></span>`;
}

// 오른쪽 코인 목록 중 지금 화면에 보이는 줄 (1초마다 이 코인들 시세도 함께 받음)
function clVisibleListSyms(limit = 50) {
    const box = document.getElementById('cl-list');
    if (!box) return [];
    const top = box.getBoundingClientRect().top, bottom = top + box.clientHeight, out = [];
    for (const row of box.children) {
        if (!row.dataset?.clPick) continue;
        const r = row.getBoundingClientRect();
        if (r.bottom < top) continue;
        if (r.top > bottom || out.length >= limit) break;
        out.push(row.dataset.clPick);
    }
    return out;
}
// 목록 한 줄의 숫자만 바꿔 넣음 (목록 전체를 다시 그리지 않음) · 바뀐 가격은 잠깐 빨강(오름) · 파랑(내림) 테두리
function clUpdateListRow(sym, t) {
    const c = clCoin(sym);
    if (c) { c.current_price = t[0]; c.change_rate = t[1]; if (t[2]) c.trade_value_24h = t[2]; }
    const row = document.querySelector(`#cl-list .cl-row[data-cl-pick="${sym}"]`);
    if (!row) return;
    const price = row.querySelector('.cl-row-price'), chg = row.querySelector('.cl-row-chg'), val = row.querySelector('.cl-row-val');
    const text = formatPrice(t[0]).replace('원', ''), cls = pnlClass(t[1]);
    if (price && price.textContent !== text) {
        const before = Number(price.dataset.p || 0);
        price.textContent = text;
        price.dataset.p = t[0];
        if (before) {
            price.classList.remove('is-flash-up', 'is-flash-down');
            void price.offsetWidth;                         // 같은 표시를 다시 켜기 위해
            price.classList.add(t[0] > before ? 'is-flash-up' : 'is-flash-down');
        }
    } else if (price && !price.dataset.p) price.dataset.p = t[0];
    if (price) price.className = price.className.replace(/\bpnl-(up|down|flat)\b/g, '').trim() + ` ${cls}`;
    if (chg) { chg.textContent = formatChangeRate(t[1]); chg.className = `cl-row-chg ${cls}`; }
    if (val && t[2]) { const v = t[2] / 1e8; val.textContent = `${v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1)}억원`; }
}

async function clTickOnce() {
    clTickN++;
    const chartSyms = [...new Set(clCells.map(c => c.sym))];
    const syms = [...new Set([...chartSyms, ...clVisibleListSyms(60 - chartSyms.length)])];
    let d = null;
    try { d = await (await fetch(`/api/tickers?markets=${syms.join(',')}`)).json(); } catch (e) { d = null; }
    if (!d?.success) return;
    for (const [sym, t] of Object.entries(d.tickers || {})) clUpdateListRow(sym, t);
    for (const cell of clCells) {
        const t = d.tickers[cell.sym];
        if (!t) continue;
        cell.price = t[0]; cell.chg = t[1];
        const p = cell.head.querySelector('.cl-price'), c = cell.head.querySelector('.cl-chg');
        if (p) p.textContent = formatPrice(t[0]);
        if (c) { c.className = `cl-chg ${pnlClass(t[1])}`; c.textContent = formatChangeRate(t[1]); }
        clTickCell(cell, t[0]);
        clRenderHold(cell);
        if (clTickN % 15 === 0) clLoadHold(cell);              // 사고판 뒤 수량 · 평균가도 따라가게 (15초마다)
    }
    if (Date.now() - clCoinsAt > 30000) clLoadCoins();
}

function clStart() {
    clStop();
    clTimer = setInterval(clTick, 1000);          // 1초마다 (업비트는 실시간 체결가 · 빗썸은 2초 캐시 시세)
    clCells.forEach(c => { if (c.ost) szStartLive(c.ost); });
}
function clStop() {
    if (clTimer) { clearInterval(clTimer); clTimer = null; }
    clCells.forEach(c => { if (c.ost) szStopLive(c.ost); });
    if (typeof szCloseConfirm === 'function') szCloseConfirm();
}

// ── 오른쪽 코인 목록 (원화 · 보유 · 관심, 찾기, 거래대금 · 등락률 · 이름 순) ──
async function clLoadCoins() {
    clCoinsAt = Date.now();
    try {
        const [d, held] = await Promise.all([(await fetch('/api/markets')).json(), getHeldCurrencies().catch(() => new Set())]);
        if (d.success) clCoins = d.coins || [];
        clHeld = held || new Set();
    } catch (e) { /* 다음 번에 다시 */ }
    renderClList();
    clCells.forEach(c => { if (c.price === undefined) renderClHead(c); });
}
function renderClList() {
    const box = document.getElementById('cl-list');
    if (!box) return;
    document.querySelectorAll('[data-cl-list]').forEach(b => {
        const on = b.dataset.clList === CL.list;
        b.classList.toggle('active', on);
        b.setAttribute('aria-selected', String(on));
    });
    document.querySelectorAll('[data-cl-sort]').forEach(b => {
        const on = b.dataset.clSort === CL.sort;
        b.classList.toggle('active', on);
        b.setAttribute('aria-sort', on ? (CL.sortDir === 'asc' ? 'ascending' : 'descending') : 'none');
        b.querySelector('i').className = `bi ${on ? (CL.sortDir === 'asc' ? 'bi-caret-up-fill' : 'bi-caret-down-fill') : 'bi-chevron-expand'}`;
    });
    if (!clCoins.length) { box.innerHTML = '<div class="cl-empty small"><span class="spinner-border spinner-border-sm"></span> 코인 목록을 불러오는 중입니다</div>'; return; }
    const q = clQuery.trim().toLowerCase();
    let rows = clCoins.filter(c => (CL.list === 'held' ? clHeld.has(c.currency) : CL.list === 'fav' ? clFav.has(c.currency) : true)
        && (!q || c.currency.toLowerCase().includes(q) || (c.korean_name || '').toLowerCase().includes(q)));
    // 고른 조건 · 패턴에 맞는 코인만 (자료가 아직이면 받는 동안 그대로 두고 받으면 다시 그림)
    const condP = clCondPreset();
    const condHits = condP ? clCondHits() : null;
    if (condP && !condHits && !clCondLoading && !clCondTimer) clCondEnsure();
    if (condHits) rows = rows.filter(c => condHits.has(c.currency));
    const sortVal = { chg: c => c.change_rate || 0, price: c => c.current_price || 0, value: c => c.trade_value_24h || 0 }[CL.sort];
    const dir = CL.sortDir === 'asc' ? 1 : -1;
    rows.sort(CL.sort === 'name' ? (a, b) => dir * (a.korean_name || '').localeCompare(b.korean_name || '', 'ko')
        : (a, b) => dir * (sortVal(a) - sortVal(b)));
    const shown = new Set(clCells.map(c => c.sym));
    const target = CL.mode === 'coin' ? `${CL.active + 1}번 칸` : '';
    document.getElementById('cl-list-hint').innerHTML = CL.mode === 'coin'
        ? `<i class="bi bi-hand-index"></i> 누르면 <b>${target}</b>에 넣습니다 · 칸을 눌러 바꿀 칸을 고르실 수 있습니다`
        : `<i class="bi bi-hand-index"></i> ${condHits ? `조건에 맞는 코인 ${rows.length}개 · ` : ''}누르면 그 코인 차트로 바뀝니다`;
    if (!rows.length) {
        box.innerHTML = `<div class="cl-empty small">${condHits ? `'${escapeHtml(condP.title)}'에 맞는 ${CL.list === 'held' ? '보유 ' : CL.list === 'fav' ? '관심 ' : ''}코인이 없습니다.`
            : CL.list === 'fav' ? '관심 코인이 없습니다. 목록의 ☆를 누르면 관심 코인이 됩니다.' : CL.list === 'held' ? '보유 코인이 없습니다.' : q && clCondMatches(q).length ? '이름이 맞는 코인은 없습니다. 위 조건 · 패턴을 눌러 보십시오.' : '찾는 코인이 없습니다.'}</div>`;
        return;
    }
    box.innerHTML = rows.map(c => {
        const sym = `KRW-${c.currency}`, fav = clFav.has(c.currency);
        const chg = c.change_rate || 0, v = (c.trade_value_24h || 0) / 1e8;
        return `<div class="cl-row${shown.has(sym) ? ' is-shown' : ''}" data-cl-pick="${sym}" role="button" tabindex="0">
            <button type="button" class="cl-star${fav ? ' is-on' : ''}" data-cl-fav="${c.currency}" title="${fav ? '관심 코인에서 뺍니다' : '관심 코인에 넣습니다'}" aria-label="관심 코인"><i class="bi bi-star-fill"></i></button>
            ${clLogo(sym)}<span class="cl-row-name"><b>${escapeHtml(c.korean_name || c.currency)}</b><small>${escapeHtml(c.currency)}/KRW${clHeld.has(c.currency) ? ' <em>보유</em>' : ''}</small></span>
            <span class="cl-row-price ${pnlClass(chg)}">${formatPrice(c.current_price).replace('원', '')}</span>
            <span class="cl-row-chg ${pnlClass(chg)}">${formatChangeRate(chg)}</span>
            <span class="cl-row-val">${v >= 100 ? Math.round(v).toLocaleString() : v.toFixed(1)}억원</span></div>`;
    }).join('');
}

function clPick(sym) {
    if (CL.mode === 'coin') {
        CL.coins[CL.active] = sym;
        CL.active = (CL.active + 1) % CL.split;
    } else {
        CL.sym = sym;
    }
    clBuild();
}

// ── 단추 ──
document.addEventListener('click', (e) => {
    if (!e.target.closest('#chart-lab')) return;
    const b = (sel) => e.target.closest(sel);
    let x;
    if ((x = b('[data-cl-mode]'))) { CL.mode = x.dataset.clMode; CL.active = 0; clBuild(); return; }
    if ((x = b('[data-cl-split]'))) { CL.split = Number(x.dataset.clSplit); CL.active = Math.min(CL.active, CL.split - 1); clBuild(); return; }
    if ((x = b('[data-cl-unit]'))) { CL.unit = x.dataset.clUnit; clBuild(); return; }
    if ((x = b('[data-cl-tf-reset]'))) { CL.tfUnits[CL.split] = [...CL_TF_DEFAULT[CL.split]]; clBuild(); return; }
    if ((x = b('[data-cl-ind]'))) { CL.ind[x.dataset.clInd] = !CL.ind[x.dataset.clInd]; clBuild(); return; }
    if (b('[data-cl-ind-add]')) { ciOpenPicker(); return; }
    if ((x = b('[data-cl-ind-edit]'))) { ciOpenEdit(x.dataset.clIndEdit); return; }
    if ((x = b('[data-cl-ind-del]'))) { clIndRemove(x.dataset.clIndDel); return; }
    if ((x = b('[data-cl-ind-toggle]'))) {                         // 켜기 · 끄기: 지우지 않고 잠깐 숨김 (기억)
        const inst = (CL.inds || []).find(i => i.id === x.dataset.clIndToggle);
        if (!inst) return;
        inst.off = !inst.off;
        // 차트는 그대로 두고: 가격 위 선은 그 선만, 아래 칸 지표는 그 칸만 숨김 (위 차트 높이는 칸 크기 감시가 맞춤)
        for (const cell of clCells) {
            for (const ci of cell.inds || []) {
                if (ci.inst.id !== inst.id) continue;
                Object.values(ci.ser || {}).forEach(sr => { try { sr.applyOptions({ visible: !inst.off }); } catch (e) { /* 이미 지운 선 */ } });
            }
            if (CI_BY[inst.key]?.pane) clPaneLayout(cell);
            if (inst.key === 'smc') clSmcApply(cell);
            try { renderClLegend(cell, null); } catch (e) { /* 범례 없음 */ }
        }
        const chip = x.closest('.ci-chip');
        chip?.classList.toggle('is-off', !!inst.off);
        x.setAttribute('aria-pressed', String(!inst.off));
        x.title = `${CI_BY[inst.key].name} — 눌러서 ${inst.off ? '다시 보이게 합니다' : '잠깐 숨깁니다 (설정은 그대로)'}`;
        clSave();
        return;
    }
    if ((x = b('[data-cl-list]'))) { CL.list = x.dataset.clList; clSave(); renderClList(); return; }
    if ((x = b('[data-cl-sort]'))) {
        const k = x.dataset.clSort;
        CL.sortDir = CL.sort === k ? (CL.sortDir === 'asc' ? 'desc' : 'asc') : (k === 'name' ? 'asc' : 'desc');
        CL.sort = k;
        clSave(); renderClList(); return;
    }
    if ((x = b('[data-cl-fav]'))) {
        const cur = x.dataset.clFav;
        if (clFav.has(cur)) clFav.delete(cur); else clFav.add(cur);
        clSaveFav(); renderClList(); return;
    }
    if ((x = b('[data-cl-big]'))) {
        const cell = clCells[Number(x.dataset.clBig)];
        if (cell) { CL.sym = cell.sym; CL.unit = cell.unit; CL.mode = 'single'; clBuild(); }
        return;
    }
    if ((x = b('[data-cl-ord-toggle]'))) { clToggleOrd(); return; }
    if ((x = b('[data-cl-pick]'))) { clPick(x.dataset.clPick); return; }
    if (b('.cl-unit-sel') || b('.cl-ord-panel')) return;
    if ((x = b('[data-cl-cell]')) && CL.mode === 'coin') {
        CL.active = Number(x.dataset.clCell);
        document.querySelectorAll('#cl-grid .cl-cell').forEach(c => c.classList.toggle('is-active', Number(c.dataset.clCell) === CL.active));
        clSave(); renderClList();
    }
});
document.addEventListener('keydown', (e) => {
    const row = e.target.closest?.('#chart-lab [data-cl-pick]');
    if (row && (e.key === 'Enter' || e.key === ' ') && e.target === row) { e.preventDefault(); clPick(row.dataset.clPick); }
});
document.addEventListener('change', (e) => {
    const s = e.target.closest?.('[data-cl-cell-unit]');
    if (!s) return;
    const i = Number(s.dataset.clCellUnit);
    const list = [...(CL.tfUnits[CL.split] || CL_TF_DEFAULT[CL.split])];
    list[i] = s.value;
    CL.tfUnits[CL.split] = list;
    const cell = clCells[i];
    if (cell) { cell.unit = s.value; clLoad(cell); }
    clSave();
});
document.addEventListener('input', (e) => {
    if (e.target.id !== 'cl-search') return;
    clQuery = e.target.value;
    renderClSuggest();
    renderClList();
});
// 코인 로고를 못 받으면 자리만 비움
document.addEventListener('error', (e) => { if (e.target?.classList?.contains('cl-logo')) e.target.classList.add('is-broken'); }, true);

// ── 오른쪽 코인 목록을 조건 · 패턴으로 거르기 ──
// 찾기 칸에 조건 이름 · 설명 낱말을 치면 코인과 함께 조건 · 패턴을 제안하고, 고르면 그 조건에 맞는 코인만 남김 (⚙ 설정 · ✕ 풀기)
// 계산 · 설정값은 전체코인 분석과 같은 것 (runScreen · screenParams) — 한쪽에서 숫자를 바꾸면 다른 쪽도 같은 값
let clCond = (() => { try { return localStorage.getItem('strabot.chartLab.cond') || ''; } catch (e) { return ''; } })();
let clCondOpen = false;        // ⚙ 설정 칸 펼침
let clCondLoading = false;
let clCondTimer = null;
function clCondPreset() {
    if (!clCond || typeof SCREEN_PRESETS === 'undefined') return null;
    return SCREEN_PRESETS.find(p => p.id === clCond) || null;
}
function clCondSave() { try { localStorage.setItem('strabot.chartLab.cond', clCond); } catch (e) { /* 저장 불가: 무시 */ } }
// 그 조건을 계산할 자료(일봉 · 고른 봉)가 다 왔나
function clCondReady(p) {
    if (!p || !marketCoins.length) return false;
    const tf = tfOf(screenParamsOf(p));
    const have = marketCoins.filter(c => periodLows[`KRW-${c.currency}`]?.day_highs).length;
    return have >= marketCoins.length * 0.9 && (tf === 'day' || !!tfBarsAt[tf]);
}
async function clCondEnsure() {
    clearTimeout(clCondTimer);
    clCondTimer = null;
    const p = clCondPreset();
    if (!p || clCondLoading || clCondReady(p)) return;
    clCondLoading = true;
    renderClCond();
    try {
        if (!clCondReady({ ...p, params: [] }) || !marketCoins.length) await loadScreen();
        const tf = tfOf(screenParamsOf(p));
        if (tf !== 'day') await ensureTfBars(tf);
    } catch (e) { /* 다음 번에 다시 */ }
    clCondLoading = false;
    // 다른 화면이 받는 중이라 아직이면 3초 뒤 다시 확인 (차트 분석을 보고 있는 동안만)
    if (!clCondReady(clCondPreset()) && document.getElementById('chart-lab')?.classList.contains('active')) clCondTimer = setTimeout(clCondEnsure, 3000);
    renderClCond();
    renderClList();
}
function clCondHits() {
    const p = clCondPreset();
    if (!p || !clCondReady(p)) return null;
    try { return new Set(runScreen(p).map(c => c.currency)); } catch (e) { return new Set(); }
}
function clCondMatches(q) {
    q = (q || '').trim().toLowerCase();
    if (!q || typeof SCREEN_PRESETS === 'undefined') return [];
    return SCREEN_PRESETS.filter(p => p.id !== 'all_coins' && !p.botOnly && !p.ruleOnly).filter(p => condSearchText(p).includes(q)).slice(0, 8);
}
function clCondMount() {
    const s = document.querySelector('#chart-lab .cl-search');
    if (!s || document.getElementById('cl-cond')) return;
    s.insertAdjacentHTML('afterend', '<div class="cl-cond-suggest" id="cl-cond-suggest" hidden></div><div class="cl-cond" id="cl-cond" hidden></div>');
    const input = document.getElementById('cl-search');
    if (input) input.placeholder = '코인명/심볼 · 조건 · 패턴 검색';
}
function renderClSuggest() {
    clCondMount();
    const box = document.getElementById('cl-cond-suggest');
    if (!box) return;
    const m = clCondMatches(clQuery);
    if (!m.length) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    const en = /^[ -~]+$/.test(clQuery.trim());
    box.innerHTML = `<div class="cl-sg-head">${en ? `'${escapeHtml(clQuery.trim())}' 관련 조건 · 패턴` : '조건 · 패턴'} <small>누르면 맞는 코인만 봅니다</small></div>` + m.map(p => {
        let n = '';
        if (clCondReady(p)) { try { n = `${runScreen(p).length}개`; } catch (e) { n = ''; } }
        const k = clCondKind(p);
        let d = '';
        try { d = String(typeof p.desc === 'function' ? p.desc(screenParamsOf(p)) : (p.desc || '')).replace(/<[^>]*>/g, ''); } catch (e) { d = ''; }
        d = d.replace(/^(사는|파는) 쪽 · /, '').replace(/\s*\((하루에 한 번|봇은)[^)]*\)\s*$/, '');
        return `<button type="button" class="cl-sg-item" data-cl-cond-pick="${p.id}" title="${escapeHtml(`${p.title}
${d}`)}">
            <span class="cl-sg-dir is-${k.dir}" aria-hidden="true">${k.dir === 'up' ? '▲' : k.dir === 'down' ? '▼' : '◆'}</span>
            <span class="cl-sg-body"><span class="cl-sg-title"><b>${escapeHtml(p.title)}</b><small class="is-${k.dir}">${escapeHtml(k.label)}</small></span><span class="cl-sg-desc">${escapeHtml(d)}</span></span>
            ${n ? `<em>${n}</em>` : ''}</button>`;
    }).join('');
}
// 제안 칸에 붙일 '어느 쪽 신호인가' — 묶음(오르는 흐름 · 많이 내려온 코인 …)과 패턴 설명의 사는 쪽 · 파는 쪽으로
function clCondKind(p) {
    const g = (typeof COND_GROUPS !== 'undefined' ? COND_GROUPS : []).find(x => x.ids.includes(p.id));
    let d = '';
    try { d = String(typeof p.desc === 'function' ? p.desc(screenParamsOf(p)) : (p.desc || '')); } catch (e) { d = ''; }
    if (d.startsWith('사는 쪽')) return { dir: 'up', label: `${g ? g.title : '패턴'} · 오름 신호` };
    if (d.startsWith('파는 쪽')) return { dir: 'down', label: `${g ? g.title : '패턴'} · 내림 신호` };
    if (g?.key === 'up' || g?.key === 'jump') return { dir: 'up', label: g.title };
    if (g?.key === 'down') return { dir: 'down', label: g.title };
    return { dir: 'flat', label: g ? g.title : '기타' };
}
function renderClCond() {
    clCondMount();
    const box = document.getElementById('cl-cond');
    if (!box) return;
    const p = clCondPreset();
    if (!p) { box.hidden = true; box.innerHTML = ''; return; }
    box.hidden = false;
    box.classList.toggle('is-open', clCondOpen);          // 펼치면 칩과 설정 칸이 한 카드로 붙음
    const prm = screenParamsOf(p), tf = tfOf(prm);
    const hits = clCondHits();
    const sum = p.params.map(q => `${q.label ? `${q.label} ` : ''}${prm[q.key]}${tfUnit(q.unit, tf)}`).join(' · ');
    const tfName = TF_PRESETS.has(p.id) ? TF_LABEL[tf] : '';
    const changed = !!screenParams[p.id] && Object.keys(screenParams[p.id]).length > 0;
    box.innerHTML = `<div class="cl-cond-chip">
            <i class="bi ${p.icon}" aria-hidden="true"></i>
            <span class="t"><b></b><small></small></span>
            <span class="n">${!hits ? '<span class="spinner-border spinner-border-sm" title="일봉 자료를 받는 중입니다"></span>' : `${hits.size}개`}</span>
            <button type="button" class="${clCondOpen ? 'is-on' : ''}" data-cl-cond-set title="설정값 바꾸기" aria-label="설정값 바꾸기" aria-expanded="${clCondOpen}"><i class="bi bi-sliders"></i></button>
            <button type="button" data-cl-cond-off title="조건 풀기 (모든 코인 보기)" aria-label="조건 풀기"><i class="bi bi-x-lg"></i></button>
        </div>
        ${clCondOpen ? `<div class="cl-cond-form" role="group" aria-label="이 조건의 설정값">
            ${TF_PRESETS.has(p.id) ? `<label class="cl-cf"><span>봉</span><select class="form-select form-select-sm" data-cl-cond-tf ${screenTfAll ? 'disabled' : ''}>${SCREEN_TFS.map(t => `<option value="${t}"${t === tf ? ' selected' : ''}>${TF_LABEL[t]}</option>`).join('')}</select></label>
                ${screenTfAll ? `<small class="cl-cf-note">전체코인 분석의 '봉 단위'가 ${TF_LABEL[screenTfAll]}으로 묶여 있습니다</small>` : ''}` : ''}
            ${p.params.map(q => `<label class="cl-cf" title="${q.min !== undefined || q.max !== undefined ? `${q.min ?? ''} ~ ${q.max ?? ''}` : ''}"><span></span><input type="number" class="form-control form-control-sm text-end" data-cl-cond-param="${q.key}" value="${prm[q.key]}" step="${q.step || 1}"${q.min !== undefined ? ` min="${q.min}"` : ''}${q.max !== undefined ? ` max="${q.max}"` : ''}><em></em></label>`).join('')}
            <div class="cl-cf-foot"><small><i class="bi bi-link-45deg"></i> 전체코인 분석과 같은 값</small><button type="button" class="btn btn-link btn-sm p-0" data-cl-cond-reset ${changed ? '' : 'disabled'}>기본값으로</button></div>
        </div>` : ''}`;
    box.querySelector('.t b').textContent = p.title;
    box.querySelector('.t small').textContent = [tfName, sum].filter(Boolean).join(' · ');
    box.querySelectorAll('[data-cl-cond-param]').forEach(inp => {
        const q = p.params.find(x => x.key === inp.dataset.clCondParam);
        inp.closest('.cl-cf').querySelector('span').textContent = q.label || '';
        inp.closest('.cl-cf').querySelector('em').textContent = tfUnit(q.unit, tf);
    });
}
function clCondSetParams(next) {
    const p = clCondPreset();
    if (!p) return;
    if (next) screenParams[p.id] = next; else delete screenParams[p.id];
    try { localStorage.setItem('screenParams', JSON.stringify(screenParams)); } catch (e) { /* 저장 불가: 무시 */ }
    const ed = document.getElementById('screen-params');
    if (ed) ed.dataset.preset = '';                    // 전체코인 분석 설정 칸도 다음에 새 값으로
    renderClCond();
    renderClList();
    clCondEnsure();
    clCondRedraw();
}
document.addEventListener('click', (e) => {
    let x;
    if ((x = e.target.closest('[data-cl-cond-pick]'))) {
        clCond = x.dataset.clCondPick;
        clCondOpen = false;
        clCondSave();
        clQuery = '';
        const s = document.getElementById('cl-search');
        if (s) s.value = '';
        renderClSuggest(); renderClCond(); renderClList();
        clCondEnsure();
        clCondRedraw();
        return;
    }
    if (e.target.closest('[data-cl-cond-off]')) { clCond = ''; clCondOpen = false; clCondSave(); renderClCond(); renderClList(); clCondRedraw(); return; }
    if (e.target.closest('[data-cl-cond-set]')) { clCondOpen = !clCondOpen; renderClCond(); return; }
    if (e.target.closest('[data-cl-cond-reset]')) { clCondSetParams(null); }
});
document.addEventListener('change', (e) => {
    const p = clCondPreset();
    if (!p) return;
    const inp = e.target.closest?.('[data-cl-cond-param]');
    if (inp) {
        let v = Number(inp.value);
        if (!Number.isFinite(v)) return;
        const q = p.params.find(x => x.key === inp.dataset.clCondParam) || {};
        if (q.min !== undefined) v = Math.max(q.min, v);      // 최소 · 최대 밖이면 맞춰 넣음 (넘으면 계산이 안 돼 0개가 됨)
        if (q.max !== undefined) v = Math.min(q.max, v);
        clCondSetParams({ ...(screenParams[p.id] || {}), [inp.dataset.clCondParam]: v });
        return;
    }
    const sel = e.target.closest?.('[data-cl-cond-tf]');
    if (sel) {
        const saved = { ...(screenParams[p.id] || {}) };
        if (sel.value === 'day') delete saved.tf; else saved.tf = sel.value;
        clCondSetParams(saved);
    }
});
document.addEventListener('keydown', (e) => {
    if (e.target.id === 'cl-search' && e.key === 'Escape' && e.target.value) {
        e.target.value = ''; clQuery = ''; renderClSuggest(); renderClList();
    }
});

// 고른 조건이 보는 지표를 차트에 잠시 겹쳐 그림 — 사용자 지표(CL.inds)는 건드리지 않고, 조건을 풀면 사라짐
// 조건 숫자(전체코인 분석과 같은 값)를 그대로 지표 숫자로 씀. 이미 같은 지표 · 같은 숫자를 켜 두었으면 다시 그리지 않음
const CL_COND_INDS = {
    rsi_low: () => [['rsi', { timeperiod: 14 }]], rsi_high: () => [['rsi', { timeperiod: 14 }]],
    rsi_cross_up: () => [['rsi', { timeperiod: 14 }]], rsi_cross_down: () => [['rsi', { timeperiod: 14 }]],
    golden_cross: (p) => [['sma', { timeperiod: p.short }], ['sma', { timeperiod: p.long }]],
    dead_cross: (p) => [['sma', { timeperiod: p.short }], ['sma', { timeperiod: p.long }]],
    bb_lower: (p) => [['bbands', { timeperiod: p.days, nbdevup: p.k, nbdevdn: p.k }]],
    bb_upper: (p) => [['bbands', { timeperiod: p.days, nbdevup: p.k, nbdevdn: p.k }]],
    bb_lower_up: (p) => [['bbands', { timeperiod: p.days, nbdevup: p.k, nbdevdn: p.k }]],
    bb_upper_down: (p) => [['bbands', { timeperiod: p.days, nbdevup: p.k, nbdevdn: p.k }]],
    squeeze_break: (p) => [['bbands', { timeperiod: p.days, nbdevup: 2, nbdevdn: 2 }]],
    ma_cross_up: (p) => [['sma', { timeperiod: p.days }]], ma_cross_down: (p) => [['sma', { timeperiod: p.days }]],
    above_ma_close: (p) => [['sma', { timeperiod: p.days }]], below_ma_close: (p) => [['sma', { timeperiod: p.days }]],
    above_ema_close: (p) => [['ema', { timeperiod: p.days }]], below_ema_close: (p) => [['ema', { timeperiod: p.days }]],
    disparity_low: (p) => [['sma', { timeperiod: p.days }]], disparity_high: (p) => [['sma', { timeperiod: p.days }]],
    ma_aligned: (p) => [['sma', { timeperiod: p.short }], ['sma', { timeperiod: p.mid }], ['sma', { timeperiod: p.long }]],
    aligned_pullback: (p) => [['sma', { timeperiod: p.short }], ['sma', { timeperiod: p.mid }], ['sma', { timeperiod: p.long }]],
    macd_cross_up: () => [['macd', { fastperiod: 12, slowperiod: 26, signalperiod: 9 }]],
    macd_cross_down: () => [['macd', { fastperiod: 12, slowperiod: 26, signalperiod: 9 }]],
    stoch_up: () => [['stoch', { fastk_period: 14, slowk_period: 3, slowd_period: 3 }]],
};
function clCondInds() {
    const pr = clCondPreset(), f = pr && CL_COND_INDS[pr.id];
    if (!f) return [];
    const mine = (CL.inds || []).filter(x => CI_BY[x.key]);
    const used = new Set(mine.map(x => x.color || CI_BY[x.key].color));
    const out = [];
    for (const [key, params] of f(screenParamsOf(pr))) {
        if (!CI_BY[key]) continue;
        const inst = { key, params };
        const same = (x) => x.key === key && JSON.stringify(ciParams(x)) === JSON.stringify(ciParams(inst));
        if (mine.some(same) || out.some(same)) continue;
        const color = CI_PALETTE.find(c => !used.has(c)) || CI_BY[key].color;
        used.add(color);
        out.push({ id: `cond${out.length}`, key, params, color, cond: true });
    }
    return out;
}
function clCondRedraw() { if (clStarted && clCells.length) clBuild(); }

function setupChartLab() {
    const tab = document.getElementById('chart-lab-tab');
    if (!tab) return;
    const show = async () => {
        renderClCond();
        if (!clStarted) {
            clStarted = true;
            await clLoadCoins();
            clBuild();
        } else {
            clCells.forEach(c => c.chartEl.querySelectorAll('.cl-pane').forEach((p, i) => c.charts?.[i]?.applyOptions({ width: p.clientWidth, height: p.clientHeight })));
            if (Date.now() - clCoinsAt > 30000) clLoadCoins();
        }
        clStart();
    };
    tab.addEventListener('shown.bs.tab', show);
    tab.addEventListener('hidden.bs.tab', clStop);
    if (document.getElementById('chart-lab')?.classList.contains('active')) show();
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupChartLab);
else setupChartLab();

// 코인 목록 접기 · 펼치기 (이 PC에 기억) — 접으면 차트 칸이 넓어지고, 차트 크기는 ResizeObserver 가 맞춤
(function () {
    const wrap = document.querySelector('#chart-lab .cl-wrap');
    if (!wrap) return;
    const handle = document.getElementById('cl-side-handle');
    const set = (off, save = true) => {
        wrap.classList.toggle('is-side-off', off);
        if (handle) {
            handle.innerHTML = `<i class="bi bi-chevron-double-${off ? 'left' : 'right'}"></i>`;
            handle.title = off ? '코인 목록 펼치기' : '코인 목록 접기 (차트를 넓게)';
        }
        if (save) { try { localStorage.setItem('strabot.chartLab.sideOff', off ? '1' : '0'); } catch (e) { /* 저장 불가 */ } }
    };
    let off0 = false;
    try { off0 = localStorage.getItem('strabot.chartLab.sideOff') === '1'; } catch (e) { /* 저장소 없음 */ }
    set(off0, false);
    handle?.addEventListener('click', () => set(!wrap.classList.contains('is-side-off')));
    document.getElementById('cl-side-open')?.addEventListener('click', () => set(false));
})();
