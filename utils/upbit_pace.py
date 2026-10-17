"""업비트 시세 API 요청 속도 조절기 — 프로그램 안의 모든 요청이 한 줄로 지나가게 (2026-10-17, 429 요청 초과 정리)

업비트는 시세 API 를 그룹(candles · market · ticker · trades · orderbook)마다 초당 10회로 제한하고,
응답마다 'Remaining-Req: group=candles; min=…; sec=N' 머리말로 이번 초에 남은 횟수를 알려 준다.

전에는 캔들 요청만 두 줄(뒤쪽 0.13초 · 앞쪽 0.4초 간격 = 합쳐 초당 10.2회)로 조절하고 머리말은 읽지 않아
늘 한도에 걸쳐 있었고, 마켓 목록(market/all)은 조절 없이 봇 13개가 한꺼번에 받으러 갔다.

- wait(group, priority): 이 그룹의 다음 빈 자리까지 기다린 뒤 돌아옴. 초당 RATE 회(기본 8, 여유 2회).
  priority=True(사람이 기다리는 차트)는 줄 앞에 끼어들되, 뒤쪽 줄을 그만큼 밀어 합계는 그대로.
- note(group, resp): 응답 머리말의 남은 횟수가 1 이하면 다음 초까지 쉬고, 429 면 그룹 전체를 BACKOFF 만큼 쉬게 함.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Dict, Optional

logger = logging.getLogger(__name__)

RATE = 7.0                 # 그룹마다 초당 요청 수 (업비트 한도 10, 여유 3 — 직접 재 보니 8회/초로 고르게 보내도 서버 쪽 1초 창에 겹쳐 남은 횟수가 2까지 떨어짐)
HOLD_AT = 2                # 응답 머리말의 남은 횟수가 이 값 이하면 다음 초까지 쉼
BACKOFF = 1.0              # 429 를 받으면 그 그룹을 이만큼 쉼 (초) — 연속이면 배로
_HDR = re.compile(r'group=([a-z\-]+);(?:\s*min=(\d+);)?\s*sec=(\d+)')


class UpbitPacer:
    def __init__(self, rate: float = RATE, clock=time.monotonic, sleep=time.sleep):
        self.rate = float(rate)
        self.gap = 1.0 / self.rate
        self.clock, self.sleep = clock, sleep
        self._lock = threading.Lock()
        self._bg_next: Dict[str, float] = {}       # 그룹 → 뒤쪽 줄 다음 시작 시각
        self._prio_next: Dict[str, float] = {}     # 그룹 → 앞쪽 줄 다음 시작 시각
        self._hold: Dict[str, float] = {}          # 그룹 → 이 시각 전에는 보내지 않음 (머리말 · 429)
        self._strikes: Dict[str, int] = {}         # 그룹 → 연속 429 수
        self.stats = {'wait_sec': 0.0, 'requests': 0, 'throttled': 0, 'hold_sec': 0.0}

    def wait(self, group: str, priority: bool = False) -> float:
        """빈 자리까지 기다림. 돌려주는 값 = 기다린 초"""
        with self._lock:
            now = self.clock()
            floor = max(now, self._hold.get(group, 0.0))
            if priority:
                start = max(floor, self._prio_next.get(group, 0.0))
                self._prio_next[group] = start + self.gap
                # 뒤쪽 줄에서 자리 하나를 가져옴: 줄이 비어 있으면 이 요청 뒤 한 칸부터, 차 있으면 전부 한 칸씩 밀어 합계 유지
                self._bg_next[group] = max(self._bg_next.get(group, 0.0), start) + self.gap
            else:
                start = max(floor, self._bg_next.get(group, 0.0))
                self._bg_next[group] = start + self.gap
            self.stats['requests'] += 1
            delay = start - now
            if delay > 0:
                self.stats['wait_sec'] += delay
        if delay > 0:
            self.sleep(delay)
        return max(0.0, delay)

    def note(self, group: str, status: int, headers: Optional[dict] = None) -> None:
        """응답을 보고 다음 요청을 늦춤: 남은 횟수 1 이하 → 다음 초까지, 429 → BACKOFF (연속이면 배로, 최대 8초)"""
        with self._lock:
            now = self.clock()
            if status == 429:
                self.stats['throttled'] += 1
                if now < self._hold.get(group, 0.0):
                    return                            # 이미 쉬는 중에 날아온 429 (같이 나가 있던 요청들) — 더 늘리지 않음
                n = self._strikes.get(group, 0) + 1
                self._strikes[group] = n
                hold = min(BACKOFF * (2 ** (n - 1)), 8.0)
                self._hold[group] = now + hold
                self.stats['hold_sec'] += hold
                logger.info(f"업비트 요청 초과(429) [{group}] — {hold:.1f}초 쉼 (연속 {n}번)")
                return
            self._strikes[group] = 0
            raw = (headers or {}).get('Remaining-Req') if headers is not None else None
            if not raw:
                return
            m = _HDR.search(str(raw))
            if not m:
                return
            sec_left = int(m.group(3))
            if sec_left <= HOLD_AT:
                # 이번 초의 자리가 거의 없음: 다음 초 경계(최대 1초)까지 쉼 — 머리말의 초는 업비트 서버 시계라 넉넉히 1초
                self._hold[group] = max(self._hold.get(group, 0.0), now + 1.0)
                self.stats['hold_sec'] += 1.0

    def snapshot(self) -> Dict:
        with self._lock:
            return {**self.stats, 'wait_sec': round(self.stats['wait_sec'], 1), 'hold_sec': round(self.stats['hold_sec'], 1)}


def group_of(path: str) -> str:
    """업비트 경로 → 제한 그룹 이름 ('/v1/candles/minutes/60' → 'candles')"""
    p = path.split('/v1/', 1)[-1]
    return p.split('/', 1)[0].split('?', 1)[0] or 'default'
