"""Atomic SQLite budget reservation regression tests."""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from queue import Queue

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.utils import db as dbutil


def _close_local_connection() -> None:
    conn = getattr(dbutil._local, 'conn', None)
    if conn is not None:
        conn.close()
    dbutil._local.conn = None


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    _close_local_connection()
    path = str(tmp_path / 'budget-reservations.db')
    monkeypatch.setattr(Config, 'DB_PATH', path)
    dbutil.init_db()
    yield path
    _close_local_connection()


def _process_reserve(path: str, reservation_id: str, output) -> None:
    """Spawn-safe worker: each process opens its own SQLite connection."""

    Config.DB_PATH = path
    _close_local_connection()
    try:
        output.put(dbutil.reserve_llm_budget('run-process', reservation_id, 0.6, 1.0))
    finally:
        _close_local_connection()


def test_reservation_counts_settled_cost_and_supports_lifecycle(isolated_db):
    dbutil.insert_llm_call_log(
        'run-one',
        'deepseek',
        'deepseek-v4-pro',
        cost_cny=0.4,
    )

    assert dbutil.reserve_llm_budget('run-one', 'res-1', 0.6, 1.0)
    # Identical retries are idempotent and do not add another row.
    assert dbutil.reserve_llm_budget('run-one', 'res-1', 0.6, 1.0)
    assert not dbutil.reserve_llm_budget('run-one', 'res-2', 0.001, 1.0)
    assert len(dbutil.list_llm_budget_reservations('run-one')) == 1

    row = dbutil.get_llm_budget_reservation('res-1')
    assert row is not None
    assert row['status'] == 'active'
    assert row['amount_cny'] == pytest.approx(0.6)
    assert dbutil.release_llm_budget_reservation('res-1')
    assert dbutil.list_llm_budget_reservations('run-one', active_only=True) == []

    # Released reservations no longer consume the cap, but their immutable id
    # cannot silently be reused.
    assert not dbutil.reserve_llm_budget('run-one', 'res-1', 0.6, 1.0)
    assert dbutil.reserve_llm_budget('run-one', 'res-2', 0.6, 1.0)
    assert dbutil.delete_llm_budget_reservation('res-1')
    assert dbutil.get_llm_budget_reservation('res-1') is None


def test_threaded_reservations_cannot_both_cross_cap(isolated_db):
    barrier = threading.Barrier(2)
    results: Queue[bool] = Queue()

    def worker(reservation_id: str) -> None:
        barrier.wait()
        results.put(
            dbutil.reserve_llm_budget(
                'run-thread', reservation_id, 0.6, 1.0,
            )
        )
        _close_local_connection()

    threads = [
        threading.Thread(target=worker, args=(f'thread-{index}',))
        for index in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
        assert not thread.is_alive()

    assert sorted([results.get_nowait(), results.get_nowait()]) == [False, True]
    active = dbutil.list_llm_budget_reservations('run-thread', active_only=True)
    assert len(active) == 1


def test_process_reservations_cannot_both_cross_cap(isolated_db):
    context = multiprocessing.get_context('spawn')
    output = context.Queue()
    processes = [
        context.Process(
            target=_process_reserve,
            args=(isolated_db, f'process-{index}', output),
        )
        for index in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert process.exitcode == 0

    assert sorted([output.get(timeout=2), output.get(timeout=2)]) == [False, True]
    assert len(
        dbutil.list_llm_budget_reservations('run-process', active_only=True)
    ) == 1


def test_delete_task_runs_removes_real_and_legacy_reservations(isolated_db):
    dbutil.insert_task_run(
        run_id='run-delete',
        task_id='task-delete',
        task_card={'deliverable': 'summary'},
        status='running',
    )
    assert dbutil.reserve_llm_budget('run-delete', 'real-run', 0.2, 1.0)
    assert dbutil.reserve_llm_budget('task-delete', 'legacy-run', 0.2, 1.0)

    dbutil.delete_task_runs('task-delete')

    assert dbutil.get_llm_budget_reservation('real-run') is None
    assert dbutil.get_llm_budget_reservation('legacy-run') is None


def test_pending_planner_log_and_active_reservation_move_atomically_once(isolated_db):
    task_id = 'task-pending-planner'
    first_run = 'run-first'
    second_run = 'run-second'
    dbutil.insert_task_run(first_run, task_id, {'deliverable': 'summary'}, 'collecting')
    dbutil.insert_llm_call_log(
        task_id,
        'deepseek',
        'deepseek-v4-flash',
        agent='planner',
        cost_cny=0.01,
    )
    assert dbutil.reserve_llm_budget(task_id, 'planner-active', 1.99, 2.0)
    assert dbutil.reserve_llm_budget(task_id, 'planner-released', 0.0, 2.0)
    assert dbutil.release_llm_budget_reservation('planner-released')

    moved = dbutil.assign_pending_llm_logs(task_id, first_run)

    assert moved == {'logs': 1, 'active_reservations': 1}
    assert len(dbutil.list_llm_call_logs(first_run)) == 1
    assert dbutil.list_llm_call_logs(task_id) == []
    assert dbutil.get_llm_budget_reservation('planner-active')['run_id'] == first_run
    released = dbutil.get_llm_budget_reservation('planner-released')
    assert released['run_id'] == task_id
    assert released['status'] == 'released'
    assert dbutil.llm_budget_totals(first_run) == pytest.approx({
        'settled_cny': 0.01,
        'reserved_cny': 1.99,
        'committed_cny': 2.0,
    })

    dbutil.insert_task_run(second_run, task_id, {'deliverable': 'summary'}, 'collecting')
    assert dbutil.assign_pending_llm_logs(task_id, second_run) == {
        'logs': 0,
        'active_reservations': 0,
    }
    assert len(dbutil.list_llm_call_logs(first_run)) == 1
    assert dbutil.list_llm_call_logs(second_run) == []
    assert dbutil.get_llm_budget_reservation('planner-active')['run_id'] == first_run


def test_pending_planner_budget_migration_rolls_back_if_target_would_exceed_cap(
    isolated_db,
):
    task_id = 'task-pending-over-cap'
    run_id = 'run-target'
    dbutil.insert_task_run(run_id, task_id, {'deliverable': 'summary'}, 'collecting')
    dbutil.insert_llm_call_log(
        task_id,
        'deepseek',
        'deepseek-v4-flash',
        agent='planner',
        cost_cny=0.1,
    )
    assert dbutil.reserve_llm_budget(task_id, 'pending-half', 0.5, 1.0)
    assert dbutil.reserve_llm_budget(run_id, 'target-most', 0.7, 1.0)

    with pytest.raises(RuntimeError, match='exceeds run cap'):
        dbutil.assign_pending_llm_logs(task_id, run_id)

    assert len(dbutil.list_llm_call_logs(task_id)) == 1
    assert dbutil.list_llm_call_logs(run_id) == []
    assert dbutil.get_llm_budget_reservation('pending-half')['run_id'] == task_id
    assert dbutil.get_llm_budget_reservation('target-most')['run_id'] == run_id


@pytest.mark.parametrize('value', [-0.1, float('inf'), float('nan')])
def test_reservation_rejects_invalid_money(isolated_db, value):
    with pytest.raises(ValueError):
        dbutil.reserve_llm_budget('run-invalid', 'invalid', value, 1.0)
