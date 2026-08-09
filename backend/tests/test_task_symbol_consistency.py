"""Task creation/confirmation must preserve one canonical security identity."""

from __future__ import annotations

import json
import os
import sys
from copy import deepcopy
from pathlib import Path

import duckdb
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, task_card_for_run
from app.models.task_card import SymbolRef, TaskCard
from app.services.planner import PlannerService
from app.services.security_master import reset_security_master
from app.utils import db as dbmod


class _NoopThread:
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def start(self):
        return None


def _write_security_master(data_dir: Path) -> None:
    folder = data_dir / 'sec_master'
    folder.mkdir(parents=True)
    parquet = folder / 'sec_master.parquet'
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
            [
                ('000001.XSHE', '000001', '平安银行', 'PAYH', 'XSHE', 'E', 'L'),
                ('688305.XSHG', '688305', '科德数控', 'KDSK', 'XSHG', 'E', 'L'),
            ],
        )
        safe_path = str(parquet).replace("'", "''")
        connection.execute(
            f"COPY securities TO '{safe_path}' (FORMAT PARQUET)"
        )
    finally:
        connection.close()
    (data_dir / '_status.json').write_text(
        json.dumps({'sec_master': {'last_run': '2026-08-09 12:00:00'}}),
        encoding='utf-8',
    )


def _close_test_db() -> None:
    connection = getattr(dbmod._local, 'conn', None)
    if connection:
        connection.close()
        dbmod._local.conn = None


@pytest.fixture
def symbol_consistency_client(tmp_path, monkeypatch):
    _close_test_db()
    data_dir = tmp_path / 'datayes'
    _write_security_master(data_dir)
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path / 'uploads'))
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'chengzhu.db'))
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)
    monkeypatch.setattr(Config, 'DATAYES_ENABLED', True)
    monkeypatch.setattr(Config, 'DATAYES_DATA_DIR', str(data_dir))
    reset_security_master()

    def plan_name_only(self, requirement, **kwargs):
        return TaskCard(
            deliverable='summary',
            symbols=[SymbolRef(code=None, name='科德数控')],
            time_window={'start': '2026-01-01', 'end': '2026-08-09'},
            info_types=['financial_report'],
        )

    monkeypatch.setattr(PlannerService, 'plan', plan_name_only)
    from app.api import task as task_api

    monkeypatch.setattr(task_api.threading, 'Thread', _NoopThread)
    from app import create_app

    app = create_app(Config)
    app.config['TESTING'] = True
    # This preference reproduces the old failure mode: a name-only Planner
    # result used to be overwritten with the unrelated watched code.
    dbmod.upsert_user_preference('watch_symbols', ['000001'])
    yield app.test_client()

    _close_test_db()
    reset_security_master()


def _create_name_only_task(client):
    response = client.post(
        '/api/task/create',
        data={'requirement': '请分析科德数控的成长性'},
    )
    assert response.status_code == 200, response.get_json()
    return response.get_json()['data']


def test_create_canonicalizes_name_only_symbol_before_watch_prefill(
    symbol_consistency_client,
):
    created = _create_name_only_task(symbol_consistency_client)

    symbol = created['task_card']['symbols'][0]
    assert symbol == {
        'code': '688305',
        'name': '科德数控',
        'sec_id': '688305.XSHG',
        'exchange': 'XSHG',
        'market': 'SH',
        'list_status': 'L',
    }
    assert [item['code'] for item in created['task_card']['symbols']] == ['688305']

    saved = ResearchTask.load(created['task_id'])
    assert saved is not None
    assert saved.task_card['symbols'] == [symbol]


def test_create_finds_explicit_name_before_watch_prefill_when_planner_is_blank(
    symbol_consistency_client,
    monkeypatch,
):
    def plan_blank(self, requirement, **kwargs):
        return TaskCard(
            deliverable='summary',
            symbols=[SymbolRef(code=None, name='')],
            time_window={'start': '2026-01-01', 'end': '2026-08-09'},
            info_types=['financial_report'],
            clarifications=['未识别到股票代码或公司简称，请补充标的'],
        )

    monkeypatch.setattr(PlannerService, 'plan', plan_blank)

    created = _create_name_only_task(symbol_consistency_client)

    assert created['task_card']['symbols'][0]['sec_id'] == '688305.XSHG'
    assert created['task_card']['symbols'][0]['code'] == '688305'
    assert all('常用标的' not in item for item in created['clarifications'])


def test_get_hydrates_matching_legacy_awaiting_confirm_symbol_without_persisting(
    symbol_consistency_client,
):
    created = _create_name_only_task(symbol_consistency_client)
    task_id = created['task_id']
    task = ResearchTask.load(task_id)
    assert task is not None
    assert task.status.value == 'awaiting_confirm'
    task.task_card['symbols'] = [{'code': '688305', 'name': '科德数控'}]
    task.save()
    task_path = Path(task.folder) / 'task.json'
    before_get = task_path.read_bytes()

    response = symbol_consistency_client.get(f'/api/task/{task_id}')

    assert response.status_code == 200, response.get_json()
    symbol = response.get_json()['data']['task_card']['symbols'][0]
    assert symbol == {
        'code': '688305',
        'name': '科德数控',
        'sec_id': '688305.XSHG',
        'exchange': 'XSHG',
        'market': 'SH',
        'list_status': 'L',
    }
    assert task_path.read_bytes() == before_get
    assert json.loads(before_get)['task_card']['symbols'] == [
        {'code': '688305', 'name': '科德数控'},
    ]


def test_get_does_not_hydrate_mismatched_legacy_awaiting_confirm_symbol_or_persist(
    symbol_consistency_client,
):
    created = _create_name_only_task(symbol_consistency_client)
    task_id = created['task_id']
    task = ResearchTask.load(task_id)
    assert task is not None
    assert task.status.value == 'awaiting_confirm'
    task.task_card['symbols'] = [{'code': '000001', 'name': '科德数控'}]
    task.save()
    task_path = Path(task.folder) / 'task.json'
    before_get = task_path.read_bytes()

    response = symbol_consistency_client.get(f'/api/task/{task_id}')

    assert response.status_code == 200, response.get_json()
    symbol = response.get_json()['data']['task_card']['symbols'][0]
    assert symbol == {'code': '000001', 'name': '科德数控'}
    assert 'sec_id' not in symbol
    assert task_path.read_bytes() == before_get
    assert json.loads(before_get)['task_card']['symbols'] == [
        {'code': '000001', 'name': '科德数控'},
    ]


def test_confirm_rejects_mismatch_then_persists_canonical_sec_id(
    symbol_consistency_client,
):
    created = _create_name_only_task(symbol_consistency_client)
    task_id = created['task_id']

    mismatch = deepcopy(created['task_card'])
    mismatch['symbols'] = [{'code': '000001', 'name': '科德数控'}]
    rejected = symbol_consistency_client.post(
        f'/api/task/{task_id}/confirm',
        json={'task_card': mismatch},
    )
    assert rejected.status_code == 400
    assert '不匹配' in rejected.get_json()['error']
    rejected_task = ResearchTask.load(task_id)
    assert rejected_task is not None
    assert rejected_task.current_run_id is None
    assert dbmod.list_task_runs(task_id) == []

    valid = deepcopy(created['task_card'])
    # The server, rather than a browser-supplied hidden value, must persist the
    # canonical sec_id and exchange metadata for the selected code/name pair.
    valid['symbols'] = [{'code': '688305', 'name': '科德数控'}]
    confirmed = symbol_consistency_client.post(
        f'/api/task/{task_id}/confirm',
        json={'task_card': valid},
    )
    assert confirmed.status_code == 200, confirmed.get_json()
    run_id = confirmed.get_json()['data']['run_id']

    expected = {
        'code': '688305',
        'name': '科德数控',
        'sec_id': '688305.XSHG',
        'exchange': 'XSHG',
        'market': 'SH',
        'list_status': 'L',
    }
    saved = ResearchTask.load(task_id)
    assert saved is not None
    assert saved.task_card['symbols'] == [expected]
    assert task_card_for_run(saved, run_id)['symbols'] == [expected]

    run_row = dbmod.get_task_run(run_id)
    assert run_row is not None
    assert json.loads(run_row['task_card_json'])['symbols'] == [expected]
