from app.tools.symbol import normalize_symbol, to_em_symbol, to_market_symbol


def test_normalize():
    assert normalize_symbol('600519') == '600519'
    assert normalize_symbol('SH600519') == '600519'
    assert normalize_symbol('300750.SZ') == '300750'


def test_em_prefix():
    assert to_em_symbol('600519') == 'SH600519'
    assert to_em_symbol('300750') == 'SZ300750'
    assert to_market_symbol('600519') == '600519.SH'
