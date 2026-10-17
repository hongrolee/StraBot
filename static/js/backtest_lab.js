// ── 백테스트 메뉴: 규칙(대상 코인 · 매수 · 매도 · 위험 관리)을 저장하지 않고 여러 코인으로 과거에 돌려 봄 ──
// 왼쪽 규칙 · 패턴 목록에서 [매수] · [매도] · [대상]으로 넣고, 숫자는 칸 안에서 바꿈.
// 계산은 봇 백테스트와 같은 코드(bot_core/backtest.py) — /api/backtest/spec/check · /api/backtest/spec/run
// 결과: 코인별 요약 표 → 줄을 누르면 그 코인의 캔들(매수 ▲ · 매도 ▼) · 자산 변화 · 매매내역 표 (봇 화면 백테스트 결과 칸을 이 화면으로 옮겨 씀)
const BTL_KEY = 'strabot.btlab';
// 기본 거래 비용 (%): 수수료는 지금 모드 거래소 값(업비트 0.05% · 빗썸 기본 0.25% 또는 설정 값), 미끄럼 = bot_core/backtest.py SLIP
const BTL_FEE = Math.round((Number(document.body.dataset.exFee) || 0.0005) * 100000) / 1000, BTL_SLIP = 0.1;
const BTL_MAX = 20;
const BTL_RISK = [
    ['stop_loss_percent', '손절', '%', '산 값보다 이만큼 내리면 팜', 0.5],
    ['take_profit_percent', '익절', '%', '산 값보다 이만큼 오르면 팜', 0.5],
    ['trailing_stop_percent', '트레일링', '%', '가장 높았던 값에서 이만큼 내리면 팜', 0.5],
    ['position_size_percent', '1회 매수', '%', '굴리는 돈의 몇 %씩 살지', 1],
    ['order_krw', '코인당 금액', '원', '정하면 비율 대신 이 금액씩', 10000],
    ['max_positions', '최대 보유', '개', '1회 매수 금액 계산에 씀', 1],
];
const btl = Object.assign({
    rules: { target: [], buy: [], sell: [], buy_match: 'any', sell_match: 'any' },
    risk: { stop_loss_percent: 15, take_profit_percent: 50, trailing_stop_percent: 0, position_size_percent: 100, order_krw: 0, max_positions: 1 },
    coins: ['BTC'], unit: 'day', days: 365, start: '', end: '', krw: 1000000, tab: 'basic', q: '',
    fee: BTL_FEE, slip: BTL_SLIP,                                     // 수수료 · 미끄럼 (%) — 기본값 적용
}, (() => { try { return JSON.parse(localStorage.getItem(BTL_KEY) || '{}'); } catch (e) { return {}; } })());
let btlCheck = null, btlCheckTimer = null, btlJob = null, btlResult = null, btlSel = null, btlStarted = false;
let btlResultHome = null;              // 봇 화면 백테스트 결과 칸의 원래 자리 (이 화면을 떠나면 돌려 둠)

function btlSave() {
    try {
        const { rules, risk, coins, unit, days, start, end, krw, tab, fee, slip } = btl;
        localStorage.setItem(BTL_KEY, JSON.stringify({ rules, risk, coins, unit, days, start, end, krw, tab, fee, slip }));
    } catch (e) { /* 저장 불가: 무시 */ }
}
const btlPreset = (id) => SCREEN_PRESETS.find(p => p.id === id);
function btlParams(rule) {
    const p = btlPreset(rule.id);
    const out = Object.fromEntries((p?.params || []).map(q => [q.key, rule.params?.[q.key] ?? q.value]));
    if (rule.params?.tf && rule.params.tf !== 'day') out.tf = rule.params.tf;
    if (rule.params?.coins) out.coins = rule.params.coins;
    return out;
}

// ── 왼쪽: 규칙 · 패턴 목록 (검색 · 기본 / 패턴 / 나의 조건) ──
function btlRenderPalette() {
    const box = document.getElementById('btl-pal-list');
    if (!box) return;
    // 예전 화면 파일에는 '나의 봇' 탭이 없음 → 붙임
    const tabs = document.querySelector('#bt-lab .btl-tabs');
    if (tabs && !tabs.querySelector('[data-btl-tab="bots"]')) tabs.insertAdjacentHTML('beforeend', '<button type="button" data-btl-tab="bots" role="tab">나의 봇</button>');
    document.querySelectorAll('[data-btl-tab]').forEach(b => b.classList.toggle('active', b.dataset.btlTab === btl.tab && !btl.q));
    if (btl.tab === 'bots' && !btl.q.trim()) { btlRenderBots(box); return; }
    const q = btl.q.trim().toLowerCase();
    const all = SCREEN_PRESETS.filter(p => p.id !== 'all_coins' && p.id !== 'pick_coins' && !p.ruleOnly || ['hongro2_buy', 'hongro2_sell', 'held_days', 'above_ma_close', 'below_ma_close'].includes(p.id));
    const list = q ? all.filter(p => condSearchText(p).includes(q)) : all.filter(p => condTabOf(p) === btl.tab);
    const used = new Set([...btl.rules.buy, ...btl.rules.sell, ...btl.rules.target].map(r => r.id));
    const groups = [...COND_GROUPS, COND_OTHER_GROUP].map(g => ({ g, items: g === COND_OTHER_GROUP ? [] : g.ids.map(btlPreset).filter(p => p && list.includes(p)) }));
    const inGroups = new Set(groups.flatMap(x => x.items.map(p => p.id)));
    groups[groups.length - 1].items = list.filter(p => !inGroups.has(p.id));
    box.innerHTML = (q ? `<div class="btl-pal-note">'${escapeHtml(btl.q.trim())}' 검색 결과 ${list.length}개</div>` : '')
        + groups.filter(x => x.items.length).map(({ g, items }) => `<div class="btl-pal-group"><div class="btl-pal-head"><span>${g.icon}</span>${escapeHtml(g.title)}</div>
            ${items.map(p => {
                let d = '';
                try { d = String(typeof p.desc === 'function' ? p.desc(btlParams({ id: p.id })) : (p.desc || '')).replace(/<[^>]*>/g, ''); } catch (e) { d = ''; }
                const tgt = TARGET_IDS.has(p.id);
                return `<div class="btl-pal-item${used.has(p.id) ? ' is-used' : ''}" title="${escapeHtml(d)}"><i class="bi ${p.icon}"></i><span class="t">${escapeHtml(p.title)}${used.has(p.id) ? ' <i class="bi bi-check2 btl-used"></i>' : ''}</span>
                    <span class="btl-pal-add">${tgt ? `<button type="button" data-btl-add="target" data-id="${p.id}" title="대상 코인을 이 조건으로 고릅니다">대상</button>`
                        : `<button type="button" class="is-buy" data-btl-add="buy" data-id="${p.id}" title="매수 조건에 넣습니다">매수</button><button type="button" class="is-sell" data-btl-add="sell" data-id="${p.id}" title="매도 조건에 넣습니다">매도</button>`}</span></div>`;
            }).join('')}</div>`).join('')
        || '<div class="btl-pal-note">맞는 규칙이 없습니다</div>';
}

// ── 나의 봇: 봇의 규칙(지금 모드) · 위험 관리를 그대로 불러와 백테스트 ──
function btlRenderBots(box) {
    const bots = (typeof botsList !== 'undefined' ? botsList : []).filter(b => !b.example);
    const modeName = { upbit: '업비트', bithumb: '빗썸', sim: '시뮬레이션' }[APP_MODE] || '';
    box.innerHTML = `<div class="btl-pal-note">${modeName} 모드의 봇 규칙을 그대로 불러옵니다 · 불러오면 지금 칸의 규칙은 바뀝니다</div>`
        + (bots.map(b => `<div class="btl-bot-item${b.running ? ' is-on' : ''}"><span class="btl-bot-dot" title="${b.running ? '실행 중' : '멈춤'}"></span>
            <span class="t"><b>${escapeHtml(b.name || '봇')}</b><small>${b.running ? '실행 중' : '멈춤'}${b.holding ? ` · 보유 ${b.holding}개` : ''}</small></span>
            <button type="button" class="btl-pill" data-btl-load-bot="${b.id}">불러오기</button></div>`).join('')
        || '<div class="btl-pal-note">아직 만든 봇이 없습니다</div>');
}
async function btlLoadBot(id) {
    try {
        const d = await (await fetch(`/api/bots/${id}?mode=${APP_MODE}`)).json();
        const bot = d.bot || {};
        const per = (bot.by_mode || {})[APP_MODE] || {};
        const tr = per.trade_rules || bot.trade_rules || {};
        const rk = per.risk_config || bot.risk_config || {};
        const list = (x) => (Array.isArray(x) ? x : x ? [x] : []).filter(r => r && r.id);
        const target = list(tr.target);
        const picked = target.find(r => r.id === 'pick_coins');
        const coins = picked?.params?.coins?.length ? picked.params.coins : (tr.only || []).length ? tr.only : null;
        btl.rules = {
            target: target.filter(r => r.id !== 'pick_coins').map(r => ({ id: r.id, params: { ...(r.params || {}) } })),
            buy: list(tr.buy).map(r => JSON.parse(JSON.stringify(r))), sell: list(tr.sell).map(r => JSON.parse(JSON.stringify(r))),
            buy_match: tr.buy_match || 'any', sell_match: tr.sell_match || 'any', target_match: tr.target_match || 'all',
        };
        for (const k of ['stop_loss_percent', 'take_profit_percent', 'trailing_stop_percent', 'position_size_percent', 'order_krw', 'max_positions']) {
            if (rk[k] !== undefined && rk[k] !== null) btl.risk[k] = Number(rk[k]);
        }
        if (btl.risk.stop_loss_percent >= 100) btl.risk.stop_loss_percent = 0;            // 100 = 손절 없음과 같음
        if (coins) btl.coins = coins.map(c => String(c).toUpperCase().replace('KRW-', '')).slice(0, BTL_MAX);
        btlSave(); btlRenderCards();
        const notes = [];
        if (!coins && btl.rules.target.length) notes.push('대상 코인은 [지금 맞는 코인으로 채우기]로 채울 수 있습니다');
        if (!btl.rules.buy.length) notes.push('매수 조건이 없는 봇입니다');
        showNotification(`'${bot.name || '봇'}' 규칙을 불러왔습니다${notes.length ? ' — ' + notes.join(' · ') : ''}`, 'success');
    } catch (e) {
        showNotification('봇 규칙을 불러오지 못했습니다', 'error');
    }
}
document.addEventListener('click', (e) => {
    const b = e.target.closest('[data-btl-load-bot]');
    if (b) btlLoadBot(b.dataset.btlLoadBot);
});

// ── 가운데: 대상 코인 · 매수 · 매도 · 위험 관리 ──
const btlNum = (attrs, value, unit, step = 1, min) => `<div class="btl-num"><input type="number" class="form-control form-control-sm" ${attrs} value="${value}" step="${step}"${min !== undefined ? ` min="${min}"` : ''}><span>${escapeHtml(unit || '')}</span></div>`;
function btlRuleChip(side, rule, i) {
    const p = btlPreset(rule.id);
    if (!p) return '';
    const prm = btlParams(rule), tf = prm.tf || 'day';
    let d = '';
    try { d = String(typeof p.desc === 'function' ? p.desc(prm) : (p.desc || '')).replace(/<[^>]*>/g, ''); } catch (e) { d = ''; }
    if (tf !== 'day') d = `${TF_LABEL[tf]} 기준 · ${d.replace(/(\d+)일/g, '$1봉')}`;          // 계산 봉이 일봉이 아니면 설명의 '일'도 '봉'으로
    const fields = p.params.map(q => `<label class="btl-pf"><span>${escapeHtml(q.label || '값')}</span>${btlNum(`data-btl-param="${side}:${i}:${q.key}"${q.max !== undefined ? ` max="${q.max}"` : ''}`, prm[q.key], tfUnit(q.unit, tf), q.step || 1, q.min)}</label>`).join('');
    const tfs = TF_PRESETS.has(p.id) ? `<div class="btl-pf btl-tf" data-key="${side}:${i}" title="이 조건을 어떤 캔들로 계산할지 (확인 간격과 따로 정합니다)"><span>계산 봉</span>${tfSegHtml(tf, 'data-btl-tfv', BTL_TFS)}</div>` : '';
    return `<div class="btl-rule is-${side}"><div class="btl-rule-head"><span class="btl-rule-ic"><i class="bi ${p.icon}"></i></span><div class="btl-rule-t"><b>${escapeHtml(p.title)}</b><small>${escapeHtml(d)}</small></div>
            <button type="button" class="btl-x" data-btl-del="${side}:${i}" title="빼기" aria-label="${escapeHtml(p.title)} 빼기"><i class="bi bi-x-lg"></i></button></div>
        ${fields || tfs ? `<div class="btl-fields">${tfs}${fields}</div>` : ''}</div>`;
}
function btlMatchSeg(side) {
    if (btl.rules[side].length < 2) return '';
    const m = btl.rules[`${side}_match`] || 'any';
    return `<span class="tf-seg" role="group" aria-label="조건 묶기">${[['any', '하나라도'], ['all', '모두']].map(([v, t]) => `<button type="button" class="${m === v ? 'active' : ''}" data-btl-match="${side}:${v}">${t} 맞으면</button>`).join('')}</span>`;
}
const BTL_EMPTY = { buy: '왼쪽 목록에서 <b class="is-buy">매수</b>를 눌러 조건을 넣습니다', sell: '왼쪽 목록에서 <b class="is-sell">매도</b>를 눌러 조건을 넣습니다' };
function btlRenderCards() {
    const coinName = (c) => marketCoins.find(x => x.currency === c)?.korean_name || '';
    const tgt = document.getElementById('btl-target');
    if (tgt) {
        tgt.innerHTML = `<div class="btl-coins">${btl.coins.map(c => `<span class="btl-coin"><b>${escapeHtml(c)}</b><small>${escapeHtml(coinName(c))}</small><button type="button" data-btl-coin-del="${escapeHtml(c)}" aria-label="${escapeHtml(c)} 빼기"><i class="bi bi-x"></i></button></span>`).join('')
                || '<span class="btl-muted">아직 코인이 없습니다</span>'}<span class="btl-coin-count">${btl.coins.length} / ${BTL_MAX}</span></div>
            <div class="btl-coin-add"><div class="btl-search-in"><i class="bi bi-plus-lg"></i><input type="search" class="form-control form-control-sm" id="btl-coin-input" list="btl-coin-list" placeholder="코인 넣기 (BTC, 리플 …)" autocomplete="off"></div><datalist id="btl-coin-list"></datalist>
                <button type="button" class="btl-pill" data-btl-coins="top10"><i class="bi bi-bar-chart"></i> 거래대금 상위 10</button>
                <button type="button" class="btl-pill" data-btl-coins="held"><i class="bi bi-wallet2"></i> 보유 코인</button>
                ${btl.coins.length ? '<button type="button" class="btl-pill is-ghost" data-btl-coins="clear">비우기</button>' : ''}</div>
            ${btl.rules.target.length ? `<div class="btl-tgt-rules">${btl.rules.target.map((r, i) => btlRuleChip('target', r, i)).join('')}
                <div class="btl-tgt-go"><button type="button" class="btn btn-sm btn-primary" data-btl-coins="fill"><i class="bi bi-funnel"></i> 지금 맞는 코인으로 채우기</button>
                <small>거래대금 큰 순 최대 ${BTL_MAX}개 · 지금 시세 기준이라 과거 그때와는 다를 수 있습니다</small></div></div>`
                : '<small class="btl-muted">코인 성격 조건(거래대금 상위 · 새로 상장 …)의 <b>대상</b>을 누르면 그 조건으로 채울 수 있습니다</small>'}`;
        const dl = document.getElementById('btl-coin-list');
        if (dl && marketCoins.length) dl.innerHTML = marketCoins.map(c => `<option value="${escapeHtml(c.currency)}">${escapeHtml(c.korean_name || '')}</option>`).join('');
    }
    for (const side of ['buy', 'sell']) {
        const el = document.getElementById(`btl-${side}`);
        if (!el) continue;
        const tool = document.getElementById(`btl-${side}-tool`);
        if (tool) tool.innerHTML = btlMatchSeg(side);
        el.innerHTML = btl.rules[side].map((r, i) => btlRuleChip(side, r, i)).join('')
            || `<div class="btl-empty"><i class="bi bi-arrow-left-circle"></i><span>${BTL_EMPTY[side]}</span></div>`;
    }
    const rk = document.getElementById('btl-risk');
    if (rk) {
        rk.innerHTML = BTL_RISK.map(([k, name, unit, tip, step]) => `<label class="btl-rf" title="${escapeHtml(tip)}"><span>${name}</span>
            ${btlNum(`data-btl-risk="${k}"`, btl.risk[k] ?? 0, unit, step, 0)}<small>${escapeHtml(tip)}</small></label>`).join('')
            // 거래 비용 (수수료 · 미끄럼) — 사고팔 때마다, 기본값 적용
            + `<label class="btl-rf" title="거래 금액에서 떼는 수수료 (${escapeHtml(document.body.dataset.exName || '업비트')} 원화 ${BTL_FEE}%)"><span>수수료</span>${btlNum('id="btl-fee" max="5" aria-label="수수료 %"', btl.fee, '%', 0.01, 0)}<small>살 때 · 팔 때 각각</small></label>
            <label class="btl-rf" title="원하는 값보다 불리하게 체결되는 몫 (살 때 비싸게 · 팔 때 싸게)"><span>미끄럼</span>${btlNum('id="btl-slip" max="5" aria-label="미끄럼 %"', btl.slip, '%', 0.05, 0)}<small>불리하게 체결되는 몫</small></label>
            <div class="btl-cost-sum"><i class="bi bi-receipt"></i><span>거래 비용 · 한 번 사고팔면 <b id="btl-cost-round"></b></span><small id="btl-cost-note"></small>
                <button type="button" class="btl-pill is-ghost" id="btl-cost-reset" title="수수료 ${BTL_FEE}% · 미끄럼 ${BTL_SLIP}%로">기본값으로</button></div>`;
        btlRenderCosts();
    }
    btlRenderPalette();
    btlRenderTfWarn();
    btlScheduleCheck();
}

// ── 봉 · 기간 · 금액 · 시작 ──
function btlLimit() { return (btlCheck?.limits || {})[btl.unit] || 365; }
function btlSetDays(days) {
    btl.days = days;
    const end = new Date(Date.now() + 9 * 3600000), start = new Date(end.getTime() - Math.min(days, btlLimit()) * 86400000);
    btl.end = end.toISOString().slice(0, 10);
    btl.start = start.toISOString().slice(0, 10);
    btlRenderRun();
    btlSave();
}
function btlRenderRun() {
    const lim = btlLimit();
    const u = document.getElementById('btl-units');
    if (u) u.innerHTML = BT_UNITS.map(([v, t]) => `<button type="button" class="${v === btl.unit ? 'active' : ''}" data-btl-unit="${v}">${t}</button>`).join('');
    const pr = document.getElementById('btl-presets');
    if (pr) pr.innerHTML = BT_PRESETS.map(([d, t]) => `<button type="button" class="${d === btl.days ? 'active' : ''}" data-btl-days="${d}" ${d > lim ? 'disabled' : ''}>${t}</button>`).join('');
    const l = document.getElementById('btl-limit');
    if (l) l.textContent = `이 봉은 최대 ${lim >= 365 ? `${Math.round(lim / 365 * 10) / 10}년` : `${lim}일`}`;
    btlRenderTfWarn();
    if (!btl.start) { btlSetDays(btl.days || 365); return; }
    document.getElementById('btl-start').value = btl.start;
    document.getElementById('btl-end').value = btl.end;
    document.getElementById('btl-krw').value = btl.krw;
    btlRenderCosts();
}
// 거래 비용 칸 · 한 번 사고팔 때 빠지는 몫 안내
function btlRenderCosts() {
    const f = document.getElementById('btl-fee'), s = document.getElementById('btl-slip');
    if (!f || !s) return;
    if (document.activeElement !== f) f.value = btl.fee;
    if (document.activeElement !== s) s.value = btl.slip;
    const round = (Number(btl.fee) + Number(btl.slip)) * 2;
    const isDef = Number(btl.fee) === BTL_FEE && Number(btl.slip) === BTL_SLIP;
    document.getElementById('btl-cost-round').textContent = round ? `약 −${round.toFixed(2)}%` : '0%';
    document.getElementById('btl-cost-note').textContent = !round ? '비용 없이 계산 (실제보다 좋게 나옴)' : isDef ? '기본값 (업비트 기준)' : '바꾼 값';
    document.getElementById('btl-cost-reset').classList.toggle('d-none', isDef);
}
// 확인 간격이 조건의 계산 봉보다 길면 그 사이에 맞았다 풀린 때를 놓칠 수 있음 → 알림
const BTL_UNIT_SEC = { 1: 60, 5: 300, 10: 600, 15: 900, 30: 1800, 60: 3600, 240: 14400, day: 86400, week: 604800, month: 31 * 86400 };
const BTL_TFS = ['1m', '5m', '10m', '15m', '30m', '1h', '4h', 'day', 'w', 'M'];      // 규칙의 계산 봉 (백테스트 · 봇 모두 받을 수 있는 봉)
const BTL_TF_SEC = { '1m': 60, '5m': 300, '10m': 600, '15m': 900, '30m': 1800, '1h': 3600, '4h': 14400, day: 86400, w: 604800, M: 31 * 86400 };
function btlRenderTfWarn() {
    const box = document.getElementById('btl-tfwarn');
    if (!box) return;
    const step = BTL_UNIT_SEC[btl.unit] || 86400;
    const short = [];
    for (const side of ['buy', 'sell']) {
        for (const r of btl.rules[side]) {
            const p = btlPreset(r.id);
            if (!p || !TF_PRESETS.has(p.id)) continue;
            const tf = btlParams(r).tf || 'day';
            if (BTL_TF_SEC[tf] < step) short.push(`${p.title}(${TF_LABEL[tf]})`);
        }
    }
    box.innerHTML = short.length
        ? `<i class="bi bi-exclamation-triangle"></i> 확인 간격이 조건의 계산 봉보다 깁니다 — ${escapeHtml(short.join(', '))}. 그 사이에 조건이 맞았다 풀리면 놓칠 수 있으니 확인 간격을 같거나 짧게 고르시길 권합니다`
        : '';
}
function btlSpec() { return { name: '백테스트', trade_rules: btl.rules, risk_config: btl.risk }; }
function btlScheduleCheck() {
    clearTimeout(btlCheckTimer);
    btlCheckTimer = setTimeout(btlDoCheck, 300);
}
async function btlDoCheck() {
    const box = document.getElementById('btl-skip');
    try {
        const d = await (await fetch('/api/backtest/spec/check', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ spec: btlSpec() }) })).json();
        if (!d.success) throw new Error(d.error || '');
        btlCheck = d;
        const notes = (d.notes || []).length ? '<div class="bt-notes"><b>어림해서 돌리는 규칙</b><ul>' + d.notes.map(n => `<li>${escapeHtml(n.title)} — ${escapeHtml(n.note)}</li>`).join('') + '</ul></div>' : '';
        box.classList.toggle('is-ok', !d.skipped.length && d.buy.length > 0);
        box.innerHTML = !d.buy.length ? '<i class="bi bi-info-circle"></i> 매수 조건을 하나 이상 넣으면 시작할 수 있습니다'
            : d.skipped.length ? '<b>아래 규칙은 이번 백테스트에서 빠집니다</b> (나머지 규칙만으로 계산합니다)<ul>' + d.skipped.map(s => `<li>${s.side === 'buy' ? '매수' : '매도'} · ${escapeHtml(s.title)} — ${escapeHtml(s.why)}</li>`).join('') + '</ul>' + notes
            : `<i class="bi bi-check-circle"></i> 모든 규칙을 계산할 수 있습니다 <small>매수 ${d.buy.length}개 · 매도 ${d.sell.length}개 + 손절 · 익절 · 트레일링</small>` + notes;
        document.getElementById('btl-run').disabled = !d.buy.length || !btl.coins.length;
        btlRenderRun();
    } catch (e) {
        box.classList.remove('is-ok');
        box.innerHTML = '<i class="bi bi-exclamation-triangle"></i> StraBot을 껐다 켜야 백테스트 메뉴를 쓸 수 있습니다 (프로그램이 예전 것으로 돌고 있습니다)';
    }
}
async function btlRun() {
    btl.krw = Number(document.getElementById('btl-krw').value) || 1000000;
    btl.start = document.getElementById('btl-start').value;
    btl.end = document.getElementById('btl-end').value;
    btlSave();
    const btn = document.getElementById('btl-run'), prog = document.getElementById('btl-progress');
    const bar = document.getElementById('btl-progress-bar'), text = document.getElementById('btl-progress-text');
    btn.disabled = true;
    prog.classList.remove('d-none');
    bar.style.width = '0%';
    text.textContent = '시작하는 중…';
    try {
        const res = await fetch('/api/backtest/spec/run', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ spec: btlSpec(), coins: btl.coins, unit: btl.unit, start: btl.start, end: btl.end, krw: btl.krw,
                                   fee_pct: Number(btl.fee), slip_pct: Number(btl.slip) }) });
        const r = await res.json().catch(() => null);          // JSON 이 아니면 예전 프로그램 (백테스트 메뉴가 없는 서버)
        if (!r) throw new Error('StraBot을 껐다 켜야 백테스트 메뉴를 쓸 수 있습니다 (프로그램이 예전 것으로 돌고 있습니다)');
        if (!r.success) throw new Error(r.error || '시작하지 못했습니다');
        btlJob = r.job;
        for (;;) {
            await new Promise(res => setTimeout(res, 700));
            if (btlJob !== r.job) return;
            const j = await (await fetch(`/api/backtest/${r.job}`)).json();
            if (!j.success) throw new Error(j.error || '진행 상황을 받지 못했습니다');
            bar.style.width = `${Math.round((j.progress || 0) * 100)}%`;
            text.textContent = j.text || '';
            if (j.stage === 'error') throw new Error(j.error || '백테스트에 실패했습니다');
            if (j.stage === 'done') { btlShow(j.result); break; }
        }
        prog.classList.add('d-none');
    } catch (e) {
        prog.classList.add('d-none');
        showNotification('백테스트: ' + e.message, 'error');
    } finally {
        btn.disabled = false;
    }
}

// ── 결과: 코인별 요약 → 줄을 누르면 그 코인의 캔들 · 매매내역 ──
function btlKpi(r) {
    const k = r.kpis || {}, base = k.same_cost;
    const botPnl = (k.final || 0) - (r.start_krw || 0);
    return base ? { bot: botPnl / base * 100, hold: k.same_pnl / base * 100, botPnl, buys: k.buys, sells: k.sells, win: k.win_rate }
        : { bot: null, hold: k.hold_pct, botPnl: 0, buys: 0, sells: 0, win: null };
}
// 결과 칸: 왼쪽에 코인 목록(세로) · 오른쪽에 고른 코인의 숫자 · 자산 변화 · 캔들 · 매매내역
// 결과 카드: 백테스트 전에도 늘 보임 (빈 안내) — 안에 [왼쪽 코인 목록 | 오른쪽 고른 코인의 결과]
function btlResultsBox() {
    let wrap = document.getElementById('btl-results');
    if (wrap) return wrap;
    const sum = document.getElementById('btl-summary'), slot = document.getElementById('btl-result-slot');
    if (!sum || !slot) return null;
    const card = document.createElement('section');
    card.className = 'btl-out';
    card.id = 'btl-out';
    card.innerHTML = `<header class="btl-out-head"><span class="btl-no"><i class="bi bi-bar-chart-line"></i></span><div><h6>결과</h6><small>코인별 결과 · 고른 코인의 자산 변화 · 캔들(매수 ▲ · 매도 ▼) · 매매내역</small></div></header>
        <div class="btl-out-empty" id="btl-out-empty"><i class="bi bi-hourglass"></i><b>아직 돌린 백테스트가 없습니다</b><span>위에서 규칙을 고르고 오른쪽 [백테스트 시작]을 누르면 여기에 결과가 나옵니다</span></div>`;
    sum.parentElement.insertBefore(card, sum);
    wrap = document.createElement('div');
    wrap.id = 'btl-results';
    wrap.className = 'btl-results d-none';
    card.appendChild(wrap);
    wrap.appendChild(sum);
    wrap.appendChild(slot);
    return wrap;
}
function btlShow(res) {
    btlResult = res;
    const wrap = btlResultsBox();
    wrap.classList.remove('d-none');
    document.getElementById('btl-out-empty')?.classList.add('d-none');
    const ok = res.coins.filter(x => x.result);
    const box = document.getElementById('btl-summary');
    box.classList.remove('d-none');
    const pct = (v) => (v === null || v === undefined ? '-' : `<span class="${pnlClass(v)}">${v > 0 ? '+' : ''}${v.toFixed(2)}%</span>`);
    const items = res.coins.map((x, i) => {
        const sym = x.market.replace('KRW-', '');
        const name = marketCoins.find(c => c.currency === sym)?.korean_name || '';
        const head = `<span class="btl-ci-name"><b>${escapeHtml(sym)}</b><small>${escapeHtml(name)}</small></span>`;
        if (!x.result) return `<div class="btl-ci is-err">${head}<small class="btl-ci-err">계산하지 못했습니다 — ${escapeHtml(x.error || '')}</small></div>`;
        const k = btlKpi(x.result), gap = k.bot === null ? null : k.bot - k.hold;
        return `<div class="btl-ci${btlSel === i ? ' is-sel' : ''}" data-btl-coin="${i}" role="button" tabindex="0">
            <div class="btl-ci-top">${head}${gap === null ? '<span class="btl-ci-gap is-none">산 적 없음</span>' : `<span class="btl-ci-gap ${gap >= 0 ? 'is-good' : 'is-bad'}" title="봇 적용 − 봇 미적용">${gap > 0 ? '+' : ''}${gap.toFixed(2)}%p</span>`}</div>
            <div class="btl-ci-row"><span>봇 적용 ${k.bot === null ? '-' : pct(k.bot)}</span><span>미적용 ${pct(k.hold)}</span></div>
            <div class="btl-ci-row is-sub"><span>매수 ${k.buys} · 매도 ${k.sells}</span><span>이긴 매도 ${k.win === null || k.win === undefined ? '-' : `${Math.round(k.win)}%`}</span></div></div>`;
    }).join('');
    const ks = ok.map(x => btlKpi(x.result)).filter(k => k.bot !== null);
    const avg = (f) => ks.length ? ks.reduce((s, k) => s + f(k), 0) / ks.length : null;
    const better = ks.filter(k => k.bot > k.hold).length;
    const day = (t) => formatKstTime(new Date(t * 1000).toISOString()).slice(0, 10);
    box.innerHTML = `<header class="btl-sum-head"><h6>코인별 결과 <small>${res.coins.length}개</small></h6>
            <small>${day(res.start)} ~ ${day(res.end)} · ${(BT_UNITS.find(([u]) => u === res.unit) || [, res.unit])[1]}봉 · 코인마다 ${formatKrw(btl.krw)}씩</small></header>
        ${ks.length ? `<div class="btl-avg"><div><span>평균 봇 적용</span>${pct(avg(k => k.bot))}</div><div><span>평균 미적용</span>${pct(avg(k => k.hold))}</div><div><span>봇이 나았던 코인</span><b>${better}/${ks.length}</b></div></div>` : ''}
        <div class="btl-ci-list">${items}</div>
        <small class="btl-sum-foot">누르면 오른쪽에 그 코인의 캔들(매수 ▲ · 매도 ▼)과 매매내역이 나옵니다 · 과거 결과일 뿐 앞으로의 수익을 보장하지 않습니다</small>`;
    const first = res.coins.findIndex(x => x.result);
    if (first >= 0) btlPick(btlSel !== null && res.coins[btlSel]?.result ? btlSel : first);
    document.getElementById('btl-out').scrollIntoView({ behavior: 'smooth', block: 'start' });          // 끝나면 결과로 내려감
}
function btlPick(i) {
    const x = btlResult?.coins?.[i];
    if (!x?.result) return;
    btlSel = i;
    document.querySelectorAll('#btl-summary [data-btl-coin]').forEach(el => el.classList.toggle('is-sel', Number(el.dataset.btlCoin) === i));
    btlMountResult();
    btState.botId = null;                       // 봇 화면 백테스트 탭은 다음에 열 때 그 봇으로 다시 시작
    const cur = x.market.replace('KRW-', '');
    btRender(x.result, marketCoins.find(c => c.currency === cur) || { currency: cur, korean_name: cur });
}
function btlMountResult() {
    const res = document.getElementById('bt-result'), slot = document.getElementById('btl-result-slot');
    if (!res || !slot || res.parentElement === slot) return;
    if (!btlResultHome) btlResultHome = { parent: res.parentElement, next: res.nextSibling };
    slot.appendChild(res);
}
function btlUnmountResult() {
    const res = document.getElementById('bt-result');
    if (!res || !btlResultHome || res.parentElement === btlResultHome.parent) return;
    btlResultHome.parent.insertBefore(res, btlResultHome.next);
    res.classList.add('d-none');               // 봇 화면에서는 그 봇으로 다시 돌릴 때까지 숨김
}

// ── 이 규칙으로 봇 만들기 (켜지는 않음) — 머리줄 오른쪽 단추 · 누르면 위에 겹쳐 뜨는 작은 창 ──
function btlMountMakeButton() {
    document.querySelector('#bt-lab .btl-flow')?.remove();                    // 순서 표시는 단추와 헷갈려 뺌
    if (document.getElementById('btl-make-open')) return;
    document.querySelector('#bt-lab details.btl-make')?.remove();          // 예전 화면 파일: 실행 칸 아래 접는 칸
    const hero = document.querySelector('#bt-lab .btl-hero');
    if (!hero) return;
    hero.querySelector('.btl-flow')?.remove();                              // 예전 화면 파일의 순서 표시 (단추와 헷갈려 뺌)
    const side = document.createElement('div');
    side.className = 'btl-hero-side';
    side.insertAdjacentHTML('beforeend', `<button type="button" class="btn btn-sm btl-reset-btn" id="btl-reset" title="대상 코인 · 매수 · 매도 · 위험 관리 · 기간 · 결과를 처음 상태로"><i class="bi bi-arrow-counterclockwise"></i> 초기화</button><div class="btl-make-wrap">
        <button type="button" class="btn btn-sm btn-outline-primary btl-make-btn" id="btl-make-open" aria-haspopup="dialog" aria-expanded="false"><i class="bi bi-robot"></i> 이 규칙으로 봇 만들기</button>
        <div class="btl-make-pop" id="btl-make-pop" role="dialog" aria-label="이 규칙으로 봇 만들기" hidden>
            <b>이 규칙으로 봇 만들기</b>
            <p>대상 코인 · 매수 · 매도 · 위험 관리를 그대로 담은 봇을 만듭니다. 켜지는 않습니다.</p>
            <div class="btl-make-row"><input type="text" class="form-control form-control-sm" id="btl-bot-name" placeholder="봇 이름 (예: 볼린저 아래에서 사기)" maxlength="30" aria-label="봇 이름">
                <button type="button" class="btn btn-sm btn-primary" id="btl-make-bot">만들기</button></div>
            <small id="btl-made"></small></div></div>`);
    hero.appendChild(side);
}
// 초기화: 한 번 누르면 '한 번 더 누르면 초기화'로 바뀌고(4초), 한 번 더 누르면 처음 상태로 (실수로 지우지 않게)
const BTL_DEFAULT = () => ({
    rules: { target: [], buy: [], sell: [], buy_match: 'any', sell_match: 'any' },
    risk: { stop_loss_percent: 15, take_profit_percent: 50, trailing_stop_percent: 0, position_size_percent: 100, order_krw: 0, max_positions: 1 },
    coins: ['BTC'], unit: 'day', days: 365, start: '', end: '', krw: 1000000, fee: BTL_FEE, slip: BTL_SLIP, q: '',
});
let btlResetArmed = null;
function btlReset() {
    const b = document.getElementById('btl-reset');
    if (!btlResetArmed) {
        b.classList.add('is-armed');
        b.innerHTML = '<i class="bi bi-exclamation-circle"></i> 한 번 더 누르면 초기화';
        btlResetArmed = setTimeout(btlResetDisarm, 4000);
        return;
    }
    btlResetDisarm();
    Object.assign(btl, BTL_DEFAULT());
    btlJob = null; btlResult = null; btlSel = null;
    const s = document.getElementById('btl-search');
    if (s) s.value = '';
    document.getElementById('btl-results')?.classList.add('d-none');
    document.getElementById('btl-out-empty')?.classList.remove('d-none');
    document.getElementById('btl-progress')?.classList.add('d-none');
    btlSave(); btlRenderCards(); btlSetDays(365);
    showNotification('백테스트 설정을 처음 상태로 돌렸습니다', 'success');
}
function btlResetDisarm() {
    clearTimeout(btlResetArmed);
    btlResetArmed = null;
    const b = document.getElementById('btl-reset');
    if (b) { b.classList.remove('is-armed'); b.innerHTML = '<i class="bi bi-arrow-counterclockwise"></i> 초기화'; }
}
document.addEventListener('click', (e) => { if (e.target.closest('#btl-reset')) btlReset(); });
function btlMakePop(open) {
    const pop = document.getElementById('btl-make-pop'), btn = document.getElementById('btl-make-open');
    if (!pop || !btn) return;
    pop.hidden = !open;
    btn.setAttribute('aria-expanded', String(open));
    if (open) setTimeout(() => document.getElementById('btl-bot-name')?.focus(), 0);
}
document.addEventListener('click', (e) => {
    if (e.target.closest('#btl-make-open')) { btlMakePop(document.getElementById('btl-make-pop')?.hidden); return; }
    if (!e.target.closest('#btl-make-pop')) btlMakePop(false);                 // 바깥을 누르면 닫음
});
document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !document.getElementById('btl-make-pop')?.hidden) btlMakePop(false);
    if (e.key === 'Enter' && e.target.id === 'btl-bot-name') btlMakeBot();
});

async function btlMakeBot() {
    const name = (document.getElementById('btl-bot-name').value || '').trim() || '백테스트에서 만든 봇';
    const rules = JSON.parse(JSON.stringify(btl.rules));
    rules.exclude = [];
    if (!rules.target.length && btl.coins.length) rules.target = [{ id: 'pick_coins', params: { coins: [...btl.coins] } }];
    try {
        const d = await (await fetch('/api/bots', { method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name, budget: 0, budget_pct: Math.max(0, 100 - botsList.reduce((s, b) => s + (Number(b.budget_pct) || 0), 0)), trade_rules: rules, risk_config: btl.risk }) })).json();
        if (!d.success) throw new Error(d.error || '봇을 만들지 못했습니다');
        await loadBots(false);
        showNotification(`'${name}' 봇을 만들었습니다 (아직 켜지 않았습니다) — 봇 제작/관리에서 확인 후 시작해 주십시오`, 'success');
        document.getElementById('btl-made').innerHTML = `<i class="bi bi-check-circle"></i> 만들었습니다 · <a href="#" data-btl-open-bot="${d.bot.id}">봇 제작/관리에서 보기</a>`;
    } catch (e) {
        showNotification(e.message, 'error');
    }
}

// ── 이벤트 ──
function btlAddCoins(list) {
    const next = [...btl.coins];
    for (const c of list) if (!next.includes(c) && next.length < BTL_MAX) next.push(c);
    btl.coins = next;
    btlSave(); btlRenderCards();
}
document.addEventListener('click', async (e) => {
    if (!e.target.closest('#bt-lab')) return;
    let x;
    if ((x = e.target.closest('[data-btl-tab]'))) { btl.tab = x.dataset.btlTab; btl.q = ''; const s = document.getElementById('btl-search'); if (s) s.value = ''; btlSave(); btlRenderPalette(); return; }
    if ((x = e.target.closest('[data-btl-add]'))) {
        const side = x.dataset.btlAdd, id = x.dataset.id;
        if (!btl.rules[side].some(r => r.id === id)) btl.rules[side].push({ id, params: {} });
        btlSave(); btlRenderCards(); return;
    }
    if ((x = e.target.closest('[data-btl-del]'))) { const [side, i] = x.dataset.btlDel.split(':'); btl.rules[side].splice(Number(i), 1); btlSave(); btlRenderCards(); return; }
    if ((x = e.target.closest('[data-btl-match]'))) { const [side, v] = x.dataset.btlMatch.split(':'); btl.rules[`${side}_match`] = v; btlSave(); btlRenderCards(); return; }
    if ((x = e.target.closest('[data-btl-coin-del]'))) { btl.coins = btl.coins.filter(c => c !== x.dataset.btlCoinDel); btlSave(); btlRenderCards(); return; }
    if ((x = e.target.closest('[data-btl-coins]'))) {
        const k = x.dataset.btlCoins;
        if (k === 'clear') { btl.coins = []; btlSave(); btlRenderCards(); return; }
        if (!marketCoins.length) await loadMarkets(true);
        if (k === 'top10') btlAddCoins([...marketCoins].sort((a, b) => (b.trade_value_24h || 0) - (a.trade_value_24h || 0)).filter(c => !isStableCoin(c)).slice(0, 10).map(c => c.currency));
        if (k === 'held') { const h = await getHeldCurrencies().catch(() => new Set()); btlAddCoins([...h]); }
        if (k === 'fill') {
            x.disabled = true;
            try {
                await loadScreen();
                let hits = null;
                for (const r of btl.rules.target) {
                    const p = btlPreset(r.id);
                    if (!p) continue;
                    const prm = btlParams(r);
                    if (prm.tf && prm.tf !== 'day') await ensureTfBars(prm.tf);
                    const set = new Set(runScreen(p, prm).map(c => c.currency));
                    hits = hits === null ? set : (btl.rules.target_match === 'any' ? new Set([...hits, ...set]) : new Set([...hits].filter(c => set.has(c))));
                }
                const picked = marketCoins.filter(c => hits?.has(c.currency)).sort((a, b) => (b.trade_value_24h || 0) - (a.trade_value_24h || 0)).slice(0, BTL_MAX).map(c => c.currency);
                btl.coins = picked;
                btlSave(); btlRenderCards();
                showNotification(picked.length ? `지금 조건에 맞는 코인 ${picked.length}개로 채웠습니다` : '지금 조건에 맞는 코인이 없습니다', picked.length ? 'success' : 'info');
            } finally { x.disabled = false; }
        }
        return;
    }
    if ((x = e.target.closest('[data-btl-unit]'))) { btl.unit = x.dataset.btlUnit; btlSetDays(btl.days || 365); return; }
    if ((x = e.target.closest('[data-btl-days]')) && !x.disabled) { btlSetDays(Number(x.dataset.btlDays)); return; }
    if (e.target.closest('#btl-run')) { btlRun(); return; }
    if (e.target.closest('#btl-make-bot')) { btlMakeBot(); return; }
    if (e.target.closest('#btl-cost-reset')) { btl.fee = BTL_FEE; btl.slip = BTL_SLIP; btlSave(); btlRenderCosts(); return; }
    if ((x = e.target.closest('[data-btl-open-bot]'))) { e.preventDefault(); showBotsTab(); selectBot(x.dataset.btlOpenBot); return; }
    if ((x = e.target.closest('#btl-summary [data-btl-coin]'))) { btlPick(Number(x.dataset.btlCoin)); }
});
// 규칙 칸의 봉 단추 (1시간 · 4시간 · 일봉 — 봇 규칙과 같음)
document.addEventListener('click', (e) => {
    const b = e.target.closest('#bt-lab .btl-tf [data-btl-tfv]');
    if (!b) return;
    const seg = b.closest('.btl-tf'), key = seg?.dataset.key;
    if (!key) return;
    const [side, i] = key.split(':');
    const rule = btl.rules[side]?.[Number(i)];
    if (!rule) return;
    const tf = b.dataset.btlTfv;
    rule.params = { ...(rule.params || {}) };
    if (!tf || tf === 'day') delete rule.params.tf; else rule.params.tf = tf;
    btlSave(); btlRenderCards();
});
document.addEventListener('change', (e) => {
    if (!e.target.closest?.('#bt-lab')) return;
    const p = e.target.closest('[data-btl-param]');
    if (p) {
        const [side, i, key] = p.dataset.btlParam.split(':');
        const rule = btl.rules[side]?.[Number(i)], q = btlPreset(rule?.id)?.params.find(x => x.key === key);
        let v = Number(p.value);
        if (!rule || !q || !Number.isFinite(v)) return;
        if (q.min !== undefined) v = Math.max(q.min, v);
        if (q.max !== undefined) v = Math.min(q.max, v);
        rule.params = { ...(rule.params || {}), [key]: v };
        btlSave(); btlRenderCards(); return;
    }
    const r = e.target.closest('[data-btl-risk]');
    if (r) { const v = Math.max(0, Number(r.value) || 0); btl.risk[r.dataset.btlRisk] = r.dataset.btlRisk === 'max_positions' ? Math.max(1, Math.round(v)) : v; btlSave(); btlRenderCards(); return; }
    if (e.target.id === 'btl-coin-input') {
        const q = e.target.value.trim(), sym = q.toUpperCase().replace('KRW-', '');
        const hit = marketCoins.find(c => c.currency === sym) || marketCoins.find(c => matchesQuery(c, q));
        if (hit) btlAddCoins([hit.currency]); else if (q) showNotification('코인을 목록에서 골라 주십시오', 'error');
        e.target.value = '';
        return;
    }
    if (e.target.id === 'btl-start' || e.target.id === 'btl-end') { btl.start = document.getElementById('btl-start').value; btl.end = document.getElementById('btl-end').value; btl.days = null; btlSave(); btlRenderRun(); }
    if (e.target.id === 'btl-krw') { btl.krw = Math.max(10000, Number(e.target.value) || 1000000); btlSave(); }
    if (e.target.id === 'btl-fee' || e.target.id === 'btl-slip') {
        const v = Math.min(5, Math.max(0, Number(e.target.value) || 0));
        btl[e.target.id === 'btl-fee' ? 'fee' : 'slip'] = Math.round(v * 1000) / 1000;
        btlSave(); btlRenderCosts();
    }
});
document.addEventListener('input', (e) => {
    if (e.target.id !== 'btl-search') return;
    btl.q = e.target.value;
    btlRenderPalette();
});
document.addEventListener('keydown', (e) => {
    const tr = e.target.closest?.('#btl-summary [data-btl-coin]');
    if (tr && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); btlPick(Number(tr.dataset.btlCoin)); }
});

function setupBacktestLab() {
    const tab = document.getElementById('bt-lab-tab');
    if (!tab) return;
    tab.addEventListener('shown.bs.tab', async () => {
        if (!btlStarted) {
            btlStarted = true;
            if (!marketCoins.length) await loadMarkets(true).catch(() => {});
        }
        btlResultsBox();                             // 결과 카드는 돌리기 전에도 보이게
        const rh = document.querySelector('#bt-lab .btl-step.is-risk header');
        if (rh) { rh.querySelector('h6').textContent = '위험 관리 및 설정'; rh.querySelector('small').textContent = '얼마씩 사고 언제 끊을까 · 거래 비용 · 0 = 쓰지 않음'; }
        document.querySelector('#bt-lab .btl-run .btl-costs')?.closest('.btl-field')?.remove();   // 예전 화면 파일의 실행 칸 비용 칸
        btlMountMakeButton();
        btlRenderCards();
        btlRenderRun();
        if (btlResult && btlSel !== null) btlPick(btlSel);
    });
    tab.addEventListener('hidden.bs.tab', btlUnmountResult);
}
if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', setupBacktestLab);
else setupBacktestLab();
