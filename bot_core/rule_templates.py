"""바깥(블로그의 '내 봇에 적용하기')에서 가져올 수 있는 규칙 묶음 — 여기에 적힌 것만 가져올 수 있음

링크(strabot://apply?t=<id>)에는 템플릿 이름만 실리고, 규칙 내용은 프로그램 안의 이 표에서 꺼냄.
그래서 누가 이상한 링크를 만들어도 정해진 봇만, 멈춘 상태로만 만들어질 수 있음.
같은 규칙을 StraHub 백테스트(AutoTrading_Flask/data/rule_templates/<id>.json)도 씀 — 바꿀 때는 버전을 올리고 둘 다 고칠 것.
"""
import copy
from typing import Dict, Optional

TEMPLATES: Dict[str, Dict] = {
    'spike-prone-v1': {
        'title': '순간 급등 분석 규칙',
        'source': 'StraHub 블로그 11편 「순간 급등은 언제, 어떤 코인에서 났을까」',
        'summary': ['사는 조건: 급등이 잦던 코인(하루 거래대금 100억 원 이하 · 상장 90일 미만·10원 미만·바이낸스 없음 중 하나)'
                    ' 그리고 급등 직전 모습(직전 1시간 +3% · 15분 거래대금 평소의 5배)',
                    '파는 조건: 순간 급등 세트 — 가진 코인이 순간 급등하면 30%를 팔고 −3·−5·−8%에 나눠 다시 사기',
                    '안전장치: 손절 −5% · 익절 +10% · 최대 3개 · 한 번에 잔고의 10%'],
        'bot': {
            'name': '급등 분석 규칙 (가져옴)',
            'budget': 1_000_000,
            'trade_rules': {
                'buy': [{'id': 'spike_prone', 'params': {'max_value': 100, 'new_days': 90, 'max_price': 10}},
                        {'id': 'pre_spike', 'params': {'rise_1h': 3, 'mult': 5}}],
                'buy_match': 'all',
                'sell': [{'id': 'spike_pump', 'params': {'mult': 5, 'rise': 5, 'rise_1h': 10},
                          'sell_mode': 'partial', 'portion': 30, 'dips': [3, 5, 8]}],
                'exclude': [],
            },
            'risk_config': {'position_size_percent': 10.0, 'stop_loss_percent': 5.0, 'take_profit_percent': 10.0,
                            'max_positions': 3},
        },
    },
}


def get(template_id: str) -> Optional[Dict]:
    """템플릿 하나 (복사본). 없으면 None"""
    t = TEMPLATES.get(str(template_id or ''))
    return copy.deepcopy(t) if t else None


def bot_spec(template_id: str) -> Optional[Dict]:
    """BotManager.create 에 넘길 봇 설정 (가져온 표시 포함). 없으면 None"""
    t = get(template_id)
    if not t:
        return None
    spec = t['bot']
    spec['imported_from'] = template_id
    return spec
