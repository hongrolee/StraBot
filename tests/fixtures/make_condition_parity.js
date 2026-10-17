// 화면(JS) 조건 계산 결과를 뽑아 파이썬 조건 계산(bot_core/conditions.py)과 비교할 기준 파일을 만든다.
// 실행: node tests/fixtures/make_condition_parity.js  → tests/fixtures/condition_parity.json
const fs = require('fs');
const path = require('path');

const src = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'bot_dashboard.js'), 'utf8').split('\r\n').join('\n');
const cut = (from, to) => {
    const a = src.indexOf(from);
    const b = to ? src.indexOf(to, a) : src.length;
    if (a < 0 || b < 0) throw new Error(`표시를 찾지 못함: ${from}`);
    return src.slice(a, b);
};

// 화면 코드에서 조건 계산 부분만 가져와 실행 (전역 상태는 흉내)
globalThis.SCREEN_FORMAT = {};
globalThis.formatTradeValue = (v) => v;
globalThis.periodLows = {};
globalThis.hourlyData = {};
let code = cut('// 평소 하루 변동', '// ── 조건 검색 표의')
    + '\n' + cut('const LOW_PERIODS', '\n', true)
    + '\n' + cut('function computeLowRates', '\n}\n') + '\n}\n'
    + '\n' + cut('function recentHourly', '\n}\n') + '\n}\n'
    + '\n' + cut('const STABLE_COINS', 'let screenIncludeStable')
    + '\n' + cut('// ── 내 조건: 사용자가 문장으로', '\nfunction rebuildCustomPresets');
code = code.replace('const SCREEN_PRESETS', 'globalThis.SCREEN_PRESETS')
    .replace(/^function (\w+)/gm, 'globalThis.$1 = function $1');
eval(code);

// 결정적인 가짜 코인 300개
function rng(seed) { let x = seed; return () => { x = (x * 1103515245 + 12345) % 2147483648; return x / 2147483648; }; }
const coins = [];
const lows = {};
for (let k = 0; k < 300; k++) {
    const r = rng(k + 7);
    let p = 50 + r() * 1000;
    const closes = [], highs = [], values = [];
    const drift = (r() - 0.5) * 0.04;
    for (let i = 0; i < 30; i++) {
        p *= 1 + drift + (r() - 0.5) * (0.05 + (k % 7) * 0.02);
        closes.push(p); highs.push(p * (1 + r() * 0.05)); values.push(1e8 + r() * 5e9);
    }
    const weekCloses = [], weekHighs = [];
    let wp = p * (0.5 + r() * 2);
    for (let i = 0; i < 104; i++) { wp *= 1 + (r() - 0.5) * 0.2; weekCloses.push(wp); weekHighs.push(wp * (1 + r() * 0.15)); }
    const prev = closes[29];
    const cur = prev * (1 + (r() - 0.45) * (0.1 + (k % 5) * 0.05));
    const todayLow = Math.min(cur, prev) * (1 - r() * 0.12);
    const currency = `C${k}`;
    const buy = r() * 8e7;
    coins.push({
        currency, korean_name: k % 50 === 3 ? '테더 달러' : `코인${k}`,
        current_price: cur, prev_close: prev, today_low: todayLow,
        change_rate: (cur / prev - 1) * 100, trade_value_24h: values[29] * (0.3 + r() * 8),
        held: k % 9 === 0 ? 1 : 0,
        flow_buy: buy, flow_mult: k % 4 === 0 ? null : r() * 25, flow_ratio: r() * 100,
    });
    // 순간 급등용 1분 값 (일부는 없음 = 켠 뒤 1시간 전)
    const spAvg = k % 6 === 0 ? null : 2e6 + r() * 1.5e7;
    Object.assign(coins[coins.length - 1], {
        spike_avg: spAvg, spike_value: spAvg === null ? null : spAvg * (0.5 + r() * 12),
        spike_open: cur / (1 + r() * 0.12), spike_close: cur, spike_close_1h: k % 11 === 0 ? null : cur / (1 + r() * 0.35),
    });
    // '급등이 잦던 코인'·'급등 직전 모습'용 (일부는 모름 = null)
    const tv = coins[coins.length - 1].trade_value_24h;
    Object.assign(coins[coins.length - 1], {
        listed_days: k % 10 === 0 ? null : Math.floor(5 + r() * 400),
        on_binance: k % 8 === 0 ? null : r() < 0.6,
        spike_value15: k % 6 === 0 ? null : tv * r() * 0.15,
        spike_close_last: k % 6 === 0 ? null : cur,
    });
    if (k % 23 === 0) coins[coins.length - 1].current_price = 0.5 + r() * 9;      // 10원 미만 코인 몇 개
    // 2026-10 추가 조건용: 일별 저가 · 긴 기간 120일 (앞 90일은 따로 만든 값 + 뒤 30일은 위와 같은 값)
    const r2 = rng(k + 5000);
    const dayLows = closes.map(x => x * (1 - r2() * 0.06));
    const preC = [], preH = [], preL = [], preV = [];
    let q = closes[0];
    for (let i = 0; i < 90; i++) {
        q /= 1 + drift + (r2() - 0.5) * (0.05 + (k % 7) * 0.02);
        preC.unshift(q); preH.unshift(q * (1 + r2() * 0.05)); preL.unshift(q * (1 - r2() * 0.06)); preV.unshift(1e8 + r2() * 5e9);
    }
    lows[`KRW-${currency}`] = {
        day_closes: closes, day_highs: highs, day_values: values, day_lows: dayLows,
        day_closes_long: [...preC, ...closes], day_highs_long: [...preH, ...highs], day_lows_long: [...preL, ...dayLows], day_values_long: [...preV, ...values],
        week_closes: weekCloses.slice(1), week_highs: weekHighs,
        d1: Math.min(...closes.slice(-1)) * (1 - r() * 0.05), w1: Math.min(...closes.slice(-6)) * 0.97,
        m1: Math.min(...closes) * 0.95, h6: Math.min(...weekCloses.slice(-26)) * 0.9, y1: Math.min(...weekCloses.slice(-52)) * 0.85,
    };
}
periodLows = lows;
// '거래대금 상위' 순위 · '비트코인보다 강한 코인' 용 비트코인 종가(+지금 가격): C1 을 비트코인으로 봄 (서버 get_market_data 와 같은 방식)
[...coins].filter(c => c.trade_value_24h).sort((x, y) => y.trade_value_24h - x.trade_value_24h).forEach((c, i) => { c.value_rank = i + 1; });
const btcSeries = [...lows['KRW-C1'].day_closes, coins[1].current_price];
for (const m of Object.keys(lows)) lows[m].btc_closes = btcSeries;

// '하루 급등'용 최근 1시간봉 (시각을 고정해 파이썬과 같은 '지금'으로 계산). 일부는 봉이 없거나 모자람
const NOW = 1800000000;
Date.now = () => NOW * 1000;
const hourly = {};
coins.forEach((c, k) => {
    if (k % 17 === 0) return;                                  // 1시간봉 없음
    const r = rng(k + 1001);
    const n = k % 13 === 0 ? 1 : k % 5 === 0 ? 26 : 24;       // 봉 하나뿐 · 24시간보다 오래된 봉 섞임
    const base = Math.floor(NOW / 3600) * 3600;
    let p = c.current_price / (0.7 + r() * 0.6);
    const h = { t: [], c: [], l: [] };
    for (let i = 0; i < n; i++) {
        h.t.push(base - (n - 1 - i) * 3600);
        h.c.push(p); h.l.push(p * (1 - r() * 0.03));
        p *= 1 + (r() - 0.45) * 0.04;
    }
    hourly[`KRW-${c.currency}`] = h;
});
hourlyData = hourly;

const customs = [
    { id: 'my_a', title: 'A', match: 'all', sort: { rule: 0, dir: 'asc' }, rules: [
        { metric: 'rsi', op: '<=', value: 45, period: 14 }, { metric: 'value_mult', op: '>=', value: 1.2, days: 20 }] },
    { id: 'my_b', title: 'B', match: 'any', sort: { rule: 1, dir: 'desc' }, rules: [
        { metric: 'held', op: '==', value: 1 }, { metric: 'from_high', op: 'between', value: -90, value2: -40, months: 6 },
        { metric: 'ma_cross', op: '==', value: 1, short: 5, long: 20, within: 5, dir: 'up' }] },
    { id: 'my_c', title: 'C', match: 'all', sort: { rule: 0, dir: 'desc' }, rules: [
        { metric: 'bb_pos', op: '>', value: 0, days: 20, k: 2 }, { metric: 'streak', op: '>=', value: 2, dir: 'up' },
        { metric: 'range_pct', op: '<=', value: 80, days: 14 }, { metric: 'from_low', op: '>=', value: 3, span: 'w1' },
        { metric: 'sma_gap', op: '>', value: 0, days: 10 }, { metric: 'move_mult', op: '>=', value: 0.5 }] },
    { id: 'my_d', title: 'D', match: 'any', sort: { rule: 0, dir: 'desc' }, rules: [
        { metric: 'change_rate', op: '>=', value: 15 }, { metric: 'rebound', op: '>=', value: 10 },
        { metric: 'trade_value', op: '>=', value: 3e10 }] },
];

// 코인 목록에 조건을 돌림 (화면의 runScreen과 같은 방식: 스테이블 제외, 기간 저점 계산)
const run = (preset, p) => {
    const hits = [];
    for (const c0 of coins) {
        if (isStableCoin(c0)) continue;
        const c = { ...c0 };
        computeLowRates(c);
        const m = preset.test(c, p, lows[`KRW-${c.currency}`] || {});
        if (m) hits.push({ ...c, ...m });
    }
    return hits;
};

const cases = [];
const shrink = (params) => Object.fromEntries(params.map(q => {
    let v = q.value;
    if (typeof v === 'number') v = q.key === 'months' ? 6 : q.key === 'short' ? 3 : q.key === 'within' ? 5 : Math.max(q.min ?? 0, q.step ? +(v * 0.7).toFixed(2) : Math.round(v * 0.7));  // 입력칸이 정수(step 1)면 정수로
    return [q.key, v];
}));
for (const preset of SCREEN_PRESETS) {
    for (const p of [Object.fromEntries(preset.params.map(q => [q.key, q.value])), shrink(preset.params)]) {
        const hits = run(preset, p);
        const key = preset.sortKey;
        cases.push({ id: preset.id, params: p, hits: hits.map(h => h.currency).sort(), sort_key: key,
                     values: Object.fromEntries(hits.map(h => [h.currency, h[key] ?? null])) });
    }
}
// 가짜 데이터에서 맞는 코인이 잘 안 나오는 조건은 느슨한 숫자로 한 번 더
const extra = [
    ['surge_pullback', { days: 7, surge: 3, mult: 1, pbMin: 0, pbMax: 60 }],
    ['surge_pullback', { days: 12, surge: 5, mult: 1.5, pbMin: 2, pbMax: 40 }],
    ['squeeze', { days: 5, range: 40 }],
    ['far_from_high', { months: 24, drop: 30 }],
    // 2026-10 추가 조건: 가짜 데이터에서도 맞는 코인이 나오게 느슨하게
    ['macd_cross_up', { within: 10 }], ['macd_cross_down', { within: 10 }],
    ['ma_aligned', { short: 3, mid: 10, long: 30 }], ['aligned_pullback', { short: 3, mid: 10, long: 30, near: 15 }],
    ['stoch_up', { level: 60, within: 10 }], ['squeeze_break', { days: 10, lookback: 30, within: 15 }],
    ['big_candle', { mult: 0.8 }], ['quiet_rise', { days: 3, rise: 1, vdays: 20, ratio: 3 }],
    ['new_listing', { days: 200 }], ['value_top', { top: 100 }], ['stronger_than_btc', { days: 3, gap: 0 }],
    ['disparity_low', { days: 10, pct: 3 }], ['disparity_high', { days: 10, pct: 3 }],
    // 2026-10 패턴: 느슨한 숫자로 (맞는 코인이 나오게)
    ['hammer', { days: 3, fall: 0, ratio: 1, within: 10 }], ['shooting_star', { days: 3, rise: 0, ratio: 1, within: 10 }],
    ['bull_engulf', { days: 3, fall: 0, within: 10 }], ['bear_engulf', { days: 3, rise: 0, within: 10 }],
    ['morning_star', { big: 1, within: 20 }], ['evening_star', { big: 1, within: 20 }],
    ['three_white', { big: 0.5, within: 20 }], ['three_black', { big: 0.5, within: 20 }],
    ['double_bottom', { days: 100, tol: 10, depth: 2, within: 10 }], ['double_top', { days: 100, tol: 10, depth: 2, within: 10 }],
    ['triangle_break', { days: 60, within: 10 }], ['bull_flag', { pole: 10, rise: 10, rest: 20 }],
    ['cup_handle', { days: 100, depth: 5, handle: 30 }],
    ['rsi_div_up', { days: 100, level: 60, within: 20 }], ['rsi_div_down', { days: 100, level: 40, within: 20 }],
    ['value_breakout', { days: 5, mult: 1, within: 5 }],
    // 2차 패턴
    ['pennant_up', { pole: 10, rise: 10, rest: 20 }], ['bear_flag', { pole: 10, rise: 10, rest: 20 }], ['bear_pennant', { pole: 10, rise: 10, rest: 20 }],
    ['asc_triangle', { days: 100, tol: 8, within: 10 }], ['desc_triangle', { days: 100, tol: 8, within: 10 }], ['triangle_down', { days: 60, within: 10 }],
    ['measured_up', { days: 100, rise: 10, within: 10 }], ['measured_down', { days: 100, rise: 10, within: 10 }],
    ['asc_scallop', { days: 60, depth: 3, within: 10 }], ['desc_scallop', { days: 60, depth: 3, within: 10 }],
    ['three_rising_valleys', { days: 100, within: 10 }], ['three_desc_peaks', { days: 100, within: 10 }],
    ['inv_cup_handle', { days: 100, depth: 5, handle: 30 }],
    ['diamond_bottom', { days: 100, within: 10 }], ['diamond_top', { days: 100, within: 10 }],
    ['rect_top', { days: 10, range: 40, trend: 0, within: 10 }], ['rect_bottom', { days: 10, range: 40, trend: 0, within: 10 }],
    ['hs_top', { days: 100, tol: 15, head: 1, within: 10 }], ['hs_bottom', { days: 100, tol: 15, head: 1, within: 10 }],
];
for (const [id, p] of extra) {
    const preset = SCREEN_PRESETS.find(x => x.id === id);
    const hits = run(preset, p);
    cases.push({ id, params: p, hits: hits.map(h => h.currency).sort(), sort_key: preset.sortKey,
                 values: Object.fromEntries(hits.map(h => [h.currency, h[preset.sortKey] ?? null])) });
}
for (const spec of customs) {
    const preset = customToPreset(spec);
    const p = Object.fromEntries(preset.params.map(q => [q.key, q.value]));
    const hits = run(preset, p);
    cases.push({ id: spec.id, custom: spec, params: p, hits: hits.map(h => h.currency).sort(), sort_key: preset.sortKey,
                 values: Object.fromEntries(hits.map(h => [h.currency, h[preset.sortKey] ?? null])) });
}
fs.writeFileSync(path.join(__dirname, 'condition_parity.json'), JSON.stringify({ coins, lows, hourly, now: NOW, cases }));
console.log(`cases ${cases.length}, hits`, cases.map(c => `${c.id}:${c.hits.length}`).join(' '));
