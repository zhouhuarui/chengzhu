"""股票代码规范化。"""

from __future__ import annotations

from typing import Optional, Tuple


def normalize_symbol(symbol: str) -> str:
    """返回 6 位代码，如 '600519'。"""
    s = (symbol or '').strip().upper()
    for prefix in ('SH', 'SZ', 'BJ'):
        if s.startswith(prefix):
            s = s[len(prefix):]
    if '.' in s:
        s = s.split('.')[0]
    s = ''.join(ch for ch in s if ch.isdigit())
    if len(s) > 6:
        s = s[-6:]
    return s.zfill(6) if s else ''


def to_em_symbol(symbol: str) -> str:
    """东财财务接口前缀：SH600519 / SZ300750 / BJ830799。"""
    code = normalize_symbol(symbol)
    if not code:
        return ''
    if code.startswith(('5', '6', '9')) or code.startswith('688'):
        return f'SH{code}'
    if code.startswith(('4', '8')):
        return f'BJ{code}'
    return f'SZ{code}'


def to_market_symbol(symbol: str) -> str:
    """带交易所后缀：600519.SH / 300750.SZ / 830799.BJ。"""
    code = normalize_symbol(symbol)
    if not code:
        return ''
    em = to_em_symbol(code)
    return f'{code}.{em[:2]}'


def parse_symbol_pair(symbol: str) -> Tuple[str, Optional[str]]:
    code = normalize_symbol(symbol)
    return code, to_market_symbol(code) if code else None
