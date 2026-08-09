from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import duckdb
import pytest
from flask import Flask

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.api.security import security_bp
from app.config import Config
from app.services.security_master import (
    SecurityIdentityMismatchError,
    SecurityMaster,
    SecurityMasterUnavailableError,
    reset_security_master,
)


BASE_ROWS = [
    ('688305.XSHG', '688305', '科德数控', 'KDSK', 'XSHG', 'E', 'L'),
    ('300192.XSHE', '300192', '科德教育', 'KDJY', 'XSHE', 'E', 'L'),
    ('600519.XSHG', '600519', '贵州茅台', 'GZMT', 'XSHG', 'E', 'L'),
    ('920001.XBEI', '920001', '北交测试', 'BJCS', 'XBEI', 'E', 'L'),
]


def _write_master(root: Path, rows=BASE_ROWS) -> Path:
    folder = root / 'sec_master'
    folder.mkdir(parents=True, exist_ok=True)
    parquet = folder / 'sec_master.parquet'
    parquet.unlink(missing_ok=True)
    connection = duckdb.connect(database=':memory:')
    try:
        connection.execute(
            """
            CREATE TABLE securities (
              sec_id VARCHAR,
              ticker VARCHAR,
              sec_short_name VARCHAR,
              cn_spell VARCHAR,
              exchange_cd VARCHAR,
              asset_class VARCHAR,
              list_status_cd VARCHAR
            )
            """
        )
        connection.executemany(
            'INSERT INTO securities VALUES (?, ?, ?, ?, ?, ?, ?)',
            list(rows),
        )
        safe_path = str(parquet).replace("'", "''")
        connection.execute(
            f"COPY securities TO '{safe_path}' (FORMAT PARQUET)"
        )
    finally:
        connection.close()
    (root / '_status.json').write_text(
        json.dumps({'sec_master': {'last_run': '2026-08-09 09:30:00'}}),
        encoding='utf-8',
    )
    return parquet


def test_search_supports_code_name_pinyin_and_filters_to_active_a_shares(tmp_path):
    rows = [
        *BASE_ROWS,
        ('000001.XHKG', '000001', '境外股票', 'JWGP', 'XHKG', 'E', 'L'),
        ('000002.XSHE', '000002', '退市样本', 'TSYB', 'XSHE', 'E', 'DE'),
        ('000003.XSHE', '000003', '基金样本', 'JJYB', 'XSHE', 'F', 'L'),
        ('ABC.XSHE', 'ABC', '非法代码', 'FFDM', 'XSHE', 'E', 'L'),
    ]
    _write_master(tmp_path, rows)
    master = SecurityMaster(str(tmp_path))

    assert master.size == 4
    assert master.search('688305')[0]['name'] == '科德数控'
    assert master.search('６８８３０５')[0]['sec_id'] == '688305.XSHG'
    assert master.search('SH688305')[0]['sec_id'] == '688305.XSHG'
    assert master.search('科德数控')[0]['code'] == '688305'
    assert master.search('kdsk')[0]['code'] == '688305'
    assert master.search('科德', limit=1)[0]['name'] == '科德教育'
    assert master.search('样本') == []
    assert master.search("' OR 1=1 --") == []


def test_exact_lookup_returns_canonical_identity_and_rejects_mismatch(tmp_path):
    _write_master(tmp_path)
    master = SecurityMaster(str(tmp_path))

    expected = {
        'sec_id': '688305.XSHG',
        'code': '688305',
        'name': '科德数控',
        'exchange': 'XSHG',
        'market': 'SH',
        'market_symbol': '688305.SH',
        'list_status': 'L',
    }
    assert master.get_by_code('688305.SH') == expected
    assert master.get_by_sec_id('688305.xshg') == expected
    assert master.get_by_name('科德数控') == expected
    assert master.resolve_exact(code='688305', name='科德数控') == expected
    assert master.get_by_code('68830') is None
    assert master.get_by_name('不存在') is None
    with pytest.raises(SecurityIdentityMismatchError):
        master.resolve_exact(code='000001', name='科德数控')


def test_find_mentions_prefers_longest_overlapping_full_name(tmp_path):
    rows = [
        *BASE_ROWS,
        ('600001.XSHG', '600001', '科德', 'KD', 'XSHG', 'E', 'L'),
    ]
    _write_master(tmp_path, rows)
    master = SecurityMaster(str(tmp_path))

    matches = master.find_mentions('比较科德数控与贵州茅台的成长性')

    assert [item['name'] for item in matches] == ['科德数控', '贵州茅台']


def test_file_signature_refreshes_snapshot_and_keeps_last_good_on_failure(tmp_path):
    parquet = _write_master(tmp_path, BASE_ROWS[:1])
    master = SecurityMaster(str(tmp_path))
    assert master.get_by_code('600519') is None

    _write_master(tmp_path, [BASE_ROWS[0], BASE_ROWS[2]])
    assert master.get_by_code('600519')['name'] == '贵州茅台'

    parquet.write_bytes(b'not a parquet file')
    assert master.get_by_code('688305')['name'] == '科德数控'
    assert master.last_refresh_error == 'SecurityMasterUnavailableError'


def test_missing_local_master_raises_stable_unavailable_error(tmp_path):
    master = SecurityMaster(str(tmp_path))
    with pytest.raises(SecurityMasterUnavailableError):
        master.get_by_code('688305')


@pytest.fixture
def security_client(tmp_path, monkeypatch):
    # A quote in the configured directory verifies that the parquet path is a
    # bound DuckDB parameter, not SQL string interpolation.
    data_dir = tmp_path / "licensed'data"
    _write_master(data_dir)
    monkeypatch.setattr(Config, 'DATAYES_DATA_DIR', str(data_dir))
    reset_security_master()
    app = Flask(__name__)
    app.register_blueprint(security_bp, url_prefix='/api/security')
    app.config['TESTING'] = True
    yield app.test_client()
    reset_security_master()


def test_security_search_api_is_bounded_and_returns_only_public_fields(security_client):
    response = security_client.get('/api/security/search?q=科德&limit=999')

    assert response.status_code == 200
    payload = response.get_json()
    assert payload['success'] is True
    assert payload['data']['limit'] == 20
    assert payload['data']['as_of'] == '2026-08-09 09:30:00'
    assert payload['data']['items'][0]['name'] == '科德教育'
    assert set(payload['data']['items'][0]) == {
        'sec_id', 'code', 'name', 'exchange', 'market', 'market_symbol', 'list_status',
    }
    assert response.headers['Cache-Control'] == 'private, max-age=30'


def test_security_search_api_validates_query_and_limit(security_client):
    assert security_client.get('/api/security/search').status_code == 400
    assert security_client.get('/api/security/search?q=科德&limit=nope').status_code == 400
    assert security_client.get('/api/security/search?q=科德&limit=0').status_code == 400
    response = security_client.get('/api/security/search', query_string={'q': 'x' * 65})
    assert response.status_code == 400
    assert response.get_json()['code'] == 'invalid_security_query'


def test_security_search_api_returns_503_without_private_snapshot(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'DATAYES_DATA_DIR', str(tmp_path))
    reset_security_master()
    app = Flask(__name__)
    app.register_blueprint(security_bp, url_prefix='/api/security')

    response = app.test_client().get('/api/security/search?q=688305')

    assert response.status_code == 503
    assert response.get_json()['code'] == 'security_master_unavailable'
    reset_security_master()
