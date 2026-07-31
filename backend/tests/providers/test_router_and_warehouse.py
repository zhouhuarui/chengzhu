import json
import os
import sys
from datetime import date, datetime

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.providers.datayes.errors import ServiceUnavailableError
from app.providers.datayes.client import DatayesApiClient
from app.config import Config
from app.providers.datayes.warehouse import DatayesWarehouse
from app.providers.router import ProviderRouter


class FakeWarehouse:
    def __init__(self, rows=None, *, complete=False, fresh=False, watermark='2026-06-24'):
        self.rows = rows or []
        self.complete = complete
        self.is_fresh = fresh
        self._watermark = watermark
        self.queries = []

    def available(self, api):
        return bool(self.rows)

    def query(self, api, params, limit=5000):
        self.queries.append((api, params, limit))
        return list(self.rows)

    def covers(self, api, end_date):
        return self.complete

    def fresh(self, api, latest=False):
        return self.is_fresh

    def watermark(self, api):
        return self._watermark


class FakeApi:
    def __init__(self, rows=None, *, configured=True, error=None):
        self.rows = rows or []
        self.configured = configured
        self.error = error
        self.calls = []

    def call(self, api, params):
        self.calls.append((api, params))
        if self.error:
            raise self.error
        return list(self.rows)

    def close(self):
        pass


LOCAL_ROW = {
    'sec_id': '000001.XSHE', 'ticker': '000001', 'trade_date': '2026-06-24',
    'close_price': 10.0,
}


def test_router_uses_warehouse_for_covered_history_without_api():
    warehouse = FakeWarehouse([LOCAL_ROW], complete=True)
    api = FakeApi([dict(LOCAL_ROW, trade_date='2026-06-25')])
    router = ProviderRouter(enabled=True, warehouse=warehouse, api_client=api)
    result = router.fetch(
        'getMktEqud', {'ticker': '000001', 'endDate': '20260624'},
        end_date='2026-06-24', latest=False,
    )
    assert result.provider == 'datayes_warehouse'
    assert result.rows == [LOCAL_ROW]
    assert api.calls == []


def test_router_supplements_stale_warehouse_with_api():
    remote = dict(LOCAL_ROW, trade_date='2026-06-25', close_price=11.0)
    warehouse = FakeWarehouse([LOCAL_ROW], fresh=False)
    api = FakeApi([remote])
    router = ProviderRouter(enabled=True, warehouse=warehouse, api_client=api)
    result = router.fetch('getMktEqud', {'ticker': '000001'}, latest=True)
    assert {row['trade_date'] for row in result.rows} == {'2026-06-24', '2026-06-25'}
    assert result.provider == 'datayes_api'
    assert api.calls[0][1]['beginDate'] == '20260625'


def test_financial_overlap_preserves_time_and_is_exactly_72_hours():
    assert ProviderRouter._overlap_72h('2026-07-10T15:30:45+08:00') == '20260707153045'


def test_latest_never_returns_unverified_stale_warehouse():
    warehouse = FakeWarehouse([LOCAL_ROW], fresh=False)
    no_token = FakeApi(configured=False)
    router = ProviderRouter(enabled=True, warehouse=warehouse, api_client=no_token)
    result = router.fetch('getMktEqud', {'ticker': '000001'}, latest=True)
    assert result.rows == []
    assert result.provider == 'public_fallback'
    assert 'warehouse_stale' in result.degradation_reasons

    failing = FakeApi(error=ServiceUnavailableError('down'))
    result2 = ProviderRouter(enabled=True, warehouse=warehouse, api_client=failing).fetch(
        'getMktEqud', {'ticker': '000001'}, latest=True
    )
    assert result2.rows == []
    assert result2.provider == 'public_fallback'


def test_disabled_router_is_keyless_public_fallback():
    result = ProviderRouter(
        enabled=False, warehouse=FakeWarehouse(), api_client=FakeApi(configured=False)
    ).fetch('getMktEqud', {'ticker': '000001'}, latest=True)
    assert result.rows == []
    assert result.degradation_reasons == ['datayes_disabled']


def test_duckdb_warehouse_is_read_only_and_filters_dates(tmp_path):
    duckdb = pytest.importorskip('duckdb')
    folder = tmp_path / 'mkt_equd'
    folder.mkdir()
    parquet = folder / 'data.parquet'
    con = duckdb.connect()
    safe = str(parquet).replace("'", "''")
    con.execute(
        "COPY (SELECT * FROM (VALUES "
        "('000001.XSHE','000001','平安银行','XSHE',DATE '2026-06-23',9.0,10.0),"
        "('000001.XSHE','000001','平安银行','XSHE',DATE '2026-06-24',10.0,11.0)) "
        "t(sec_id,ticker,sec_short_name,exchange_cd,trade_date,act_pre_close_price,close_price)) "
        f"TO '{safe}' (FORMAT parquet)"
    )
    con.close()
    (tmp_path / '_watermark.json').write_text(json.dumps({'mkt_equd': '2026-06-24'}))
    (tmp_path / '_status.json').write_text(json.dumps({
        'mkt_equd': {
            'data_end': '2026-06-24', 'date_col': 'trade_date',
            'last_run': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    }))
    before = parquet.stat().st_mtime_ns
    warehouse = DatayesWarehouse(str(tmp_path))
    rows = warehouse.query(
        'getMktEqud',
        {'ticker': '000001', 'beginDate': '20260624', 'endDate': '20260624'},
    )
    assert len(rows) == 1
    assert rows[0]['close_price'] == 11.0
    assert warehouse.covers('getMktEqud', '2026-06-24')
    assert parquet.stat().st_mtime_ns == before


def test_financial_publish_date_watermark_is_not_misused_as_update_time(tmp_path):
    (tmp_path / '_watermark.json').write_text(json.dumps({'fdmt_is': '2026-06-24'}))
    (tmp_path / '_status.json').write_text(json.dumps({
        'fdmt_is': {
            'data_end': '2026-06-24', 'date_col': 'publish_date',
            'last_run': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
    }))
    warehouse = DatayesWarehouse(str(tmp_path))
    assert warehouse.watermark('getFdmtIS') is None
    warehouse.available = lambda api: True
    assert not warehouse.fresh('getFdmtIS', latest=True)


def test_financial_watermark_without_field_metadata_is_not_trusted(tmp_path):
    (tmp_path / '_watermark.json').write_text(json.dumps({'fdmt_is': '2026-06-24'}))
    warehouse = DatayesWarehouse(str(tmp_path))

    assert warehouse.watermark('getFdmtIS') is None


def test_stale_trade_calendar_cannot_prove_latest_market_day(tmp_path):
    duckdb = pytest.importorskip('duckdb')
    for table in ('trade_cal', 'mkt_equd'):
        (tmp_path / table).mkdir()
    con = duckdb.connect()
    trade_path = str(tmp_path / 'trade_cal' / 'data.parquet').replace("'", "''")
    market_path = str(tmp_path / 'mkt_equd' / 'data.parquet').replace("'", "''")
    con.execute(
        f"COPY (SELECT 'XSHG' exchange_cd, DATE '2026-06-24' calendar_date, "
        f"'1' is_open) TO '{trade_path}' (FORMAT parquet)"
    )
    con.execute(
        f"COPY (SELECT '000001.XSHE' sec_id, '000001' ticker, "
        f"DATE '2026-06-24' trade_date, 10.0 close_price) "
        f"TO '{market_path}' (FORMAT parquet)"
    )
    con.close()
    (tmp_path / '_watermark.json').write_text(json.dumps({
        'trade_cal': '2026-06-24', 'mkt_equd': '2026-06-24',
    }))
    (tmp_path / '_status.json').write_text(json.dumps({
        'trade_cal': {'data_end': '2026-06-24', 'date_col': 'calendar_date'},
        'mkt_equd': {
            'data_end': '2026-06-24', 'date_col': 'trade_date',
            'last_run': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        },
    }))
    warehouse = DatayesWarehouse(str(tmp_path))
    assert warehouse.latest_open_trading_day(date(2026, 7, 27)) is None
    assert not warehouse.fresh('getMktEqud', latest=True, now=datetime(2026, 7, 27, 12))


def test_adjustment_factor_query_includes_factor_effective_before_window(tmp_path):
    duckdb = pytest.importorskip('duckdb')
    folder = tmp_path / 'mkt_adjf_af'
    folder.mkdir()
    con = duckdb.connect()
    path = str(folder / 'data.parquet').replace("'", "''")
    con.execute(
        f"COPY (SELECT '000001.XSHE' sec_id, '000001' ticker, 'XSHE' exchange_cd, "
        f"DATE '2025-12-19' ex_div_date, 8.5 accum_adj_factor, DATE '9999-12-31' end_date) "
        f"TO '{path}' (FORMAT parquet)"
    )
    con.close()
    (tmp_path / '_watermark.json').write_text(json.dumps({'mkt_adjf_af': '2025-12-19'}))
    (tmp_path / '_status.json').write_text(json.dumps({
        'mkt_adjf_af': {'data_end': '2025-12-19', 'date_col': 'ex_div_date'}
    }))
    rows = DatayesWarehouse(str(tmp_path)).query(
        'getMktAdjfAf',
        {'ticker': '000001', 'beginDate': '20260701', 'endDate': '20260727'},
    )
    assert len(rows) == 1
    assert rows[0]['ex_div_date'] == '2025-12-19'
    assert rows[0]['accum_adj_factor'] == 8.5


def test_warehouse_datetime_window_preserves_seconds_and_end_of_day(tmp_path):
    duckdb = pytest.importorskip('duckdb')
    folder = tmp_path / 'fdmt_is'
    folder.mkdir()
    con = duckdb.connect()
    path = str(folder / 'data.parquet').replace("'", "''")
    con.execute(
        f"COPY (SELECT * FROM (VALUES "
        f"('000001.XSHE','000001',DATE '2026-06-30','S1','1',TIMESTAMP '2026-07-01 00:00:01'),"
        f"('000001.XSHE','000001',DATE '2026-06-30','S1','1',TIMESTAMP '2026-07-01 23:59:59')) "
        f"t(sec_id,ticker,end_date,report_type,merged_flag,update_time)) "
        f"TO '{path}' (FORMAT parquet)"
    )
    con.close()
    rows = DatayesWarehouse(str(tmp_path)).query(
        'getFdmtIS',
        {
            'ticker': '000001',
            'updateTimeBegin': '20260701000001',
            'updateTimeEnd': '20260701',
        },
    )
    assert len(rows) == 2
    assert rows[0]['update_time'].startswith('2026-07-01T23:59:59')


@pytest.mark.network
@pytest.mark.skipif(
    not Config.DATAYES_TOKEN
    or not Config.DATAYES_NETWORK_TESTS
    or not Config.DATAYES_DATA_DIR,
    reason='需要 DATAYES_TOKEN、DATAYES_DATA_DIR 且 DATAYES_NETWORK_TESTS=true',
)
def test_warehouse_api_quote_parity_for_same_symbol_and_date():
    warehouse = DatayesWarehouse(Config.DATAYES_DATA_DIR)
    watermark = warehouse.watermark('getMktEqud')
    assert watermark
    trade_date = watermark.replace('-', '')
    params = {'ticker': '600519', 'tradeDate': trade_date}
    local = warehouse.query('getMktEqud', params, limit=2)
    if not local:
        pytest.skip('本地仓库该日无 600519 行情')
    remote = DatayesApiClient(
        token=Config.DATAYES_TOKEN,
        base_url=Config.DATAYES_BASE_URL,
        timeout_seconds=Config.DATAYES_TIMEOUT_SECONDS,
        page_size=10,
        max_rps=Config.DATAYES_MAX_RPS,
        max_concurrency=Config.DATAYES_MAX_CONCURRENCY,
    ).call('getMktEqud', params)
    assert remote
    for field in ('close_price', 'turnover_vol', 'market_value'):
        assert local[0][field] == pytest.approx(remote[0][field], rel=1e-9, abs=1e-6)
