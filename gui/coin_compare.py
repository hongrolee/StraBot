"""
보유코인 분석 '다른 코인 비교' · '고른 코인에 봇을 적용했다면' 계산 (화면 · 서버 공통 규칙)

- 구간 = 보고 있는 수익률 그래프의 맨 왼쪽 시각 ~ 지금
- 그 시각의 코인 값 = 그때까지 끝난 봉의 종가 (보유코인 비교선과 같은 규칙: 1분 뒤 봉은 잡지 않음 · 거래 없던 봉은 바로 앞 값)
"""
import bisect
from typing import Dict, List, Optional

# 수익률 그래프 간격 → 봉 단위 (candle_store.UNITS 이름)
UNIT_OF = {'1m': '1', '10m': '10', '15m': '15', '30m': '30', '1h': '60', '4h': '240',
           '1d': 'day', '1w': 'week', '1M': 'week', '1y': 'week'}
CMP_MAX = 20                  # 다른 코인 비교: 한 번에 고를 수 있는 코인 수
BOT_MAX = 10                  # 봇 적용 비교: 계산할 코인 수 (코인마다 백테스트라 오래 걸림)
START_KRW = 1_000_000         # 봇 적용 비교: 코인마다 구간 처음에 산 금액 (같은 금액씩)
MAX_BACK_SEC = 3 * 366 * 86400  # 구간 시작은 최대 3년 전까지 (잘못된 값으로 수십 년 치 봉을 받지 않게)
SCAN_UNITS = [('1', 60), ('10', 600), ('15', 900), ('30', 1800), ('60', 3600), ('240', 14400), ('day', 86400), ('week', 604800)]


def start_ok(start: float, now: float) -> bool:
    return now - MAX_BACK_SEC <= start < now


def scan_unit(interval: str, start: float, now: float) -> str:
    """코인 고르기 창(코인 294개)용 봉: 그래프 간격의 봉이 기본, 구간이 길면(프로그램을 꺼 둔 틈 등) 구간을 400개 안쪽으로 덮는 더 큰 봉"""
    base = UNIT_OF[interval]
    secs = dict(SCAN_UNITS)
    for u, sec in SCAN_UNITS:
        if sec >= secs[base] and (now - start) / sec <= 400:
            return u
    return 'week'


def value_at(rows: List[list], sec: int, t: float) -> Optional[float]:
    """오래된 순 봉 [[t,o,h,l,c,v]] 에서 t 시각의 값 = t 까지 끝난 봉의 종가. 너무 오래 비면(거래 없음 · 상장 전) None"""
    if not rows:
        return None
    ends = [r[0] + sec for r in rows]
    k = bisect.bisect_right(ends, int(t) + 59) - 1
    if k < 0 or int(t) - ends[k] >= max(2 * sec, 3600):
        return None
    return float(rows[k][4])


def scan_stats(rows: List[list], sec: int, start: int, now_price: Optional[float]) -> Optional[Dict]:
    """구간 수익률 · 구간 최대 하락폭(그때까지 가장 높았던 종가에서 가장 많이 내려간 폭, %)
    구간 중에 상장한 코인은 첫 봉 시가에서 출발 (late=True)"""
    p0 = value_at(rows, sec, start)
    late = False
    inside = [r for r in rows if r[0] + sec > start]
    if p0 is None:
        if not inside:
            return None
        p0, late = float(inside[0][1]), True
    closes = [float(r[4]) for r in inside if r[0] + sec > start] + ([float(now_price)] if now_price else [])
    if not closes or p0 <= 0:
        return None
    peak, mdd = p0, 0.0
    for c in closes:
        peak = max(peak, c)
        mdd = max(mdd, (peak - c) / peak * 100)
    last = closes[-1]
    return {'ret': (last / p0 - 1) * 100, 'mdd': mdd, 'late': late, 'p0': p0, 'last': last}


def combine_curves(results: List[Dict]) -> List[Dict]:
    """코인마다 백테스트 곡선 [{'t', 'bot', 'hold'}] (원) → 같은 시각끼리 더한 수익률 곡선 [{'t', 'bot', 'hold'}] (%)
    어떤 코인의 값이 없는 시각은 그 코인의 바로 앞 값(처음 전이면 시작 금액)으로"""
    if not results:
        return []
    times = sorted({p['t'] for r in results for p in r['curve']})
    start = sum(r['start_krw'] for r in results)
    out = []
    idx = [0] * len(results)
    last = [(r['start_krw'], r['start_krw']) for r in results]
    for t in times:
        bot = hold = 0.0
        for i, r in enumerate(results):
            c = r['curve']
            while idx[i] < len(c) and c[idx[i]]['t'] <= t:
                last[i] = (c[idx[i]]['bot'], c[idx[i]]['hold'])
                idx[i] += 1
            bot += last[i][0]
            hold += last[i][1]
        out.append({'t': t, 'bot': (bot / start - 1) * 100, 'hold': (hold / start - 1) * 100})
    return out
