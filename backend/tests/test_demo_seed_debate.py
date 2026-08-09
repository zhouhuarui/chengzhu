"""Keyless competition demo generation, loading and API replay."""

from __future__ import annotations

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.services.artifact_store import LocalReplayArtifactStore
from scripts.build_debate_demo_seed import (
    RUN_ID,
    TASK_ID,
    build_demo,
    validate_demo,
)
from scripts.load_demo import load_demo


def test_keyless_debate_demo_survives_load_and_all_read_apis(tmp_path, monkeypatch):
    seed = tmp_path / 'seed'
    loaded_uploads = tmp_path / 'loaded_uploads'
    build_demo(seed)

    copied = load_demo(str(seed), str(loaded_uploads), force=True)
    result = validate_demo(loaded_uploads, api_check=True)

    assert {'tasks', 'chengzhu.db'}.issubset(set(copied))
    assert result['task_id'] == TASK_ID
    assert result['run_id'] == RUN_ID
    assert result['accepted'] == ['claim_same_period_h1']
    assert result['claims'] == 4
    assert result['challenges'] == 3
    assert result['llm_calls'] == 0
    assert set(result['api'].values()) == {200}

    task_dir = loaded_uploads / 'tasks' / TASK_ID
    task = json.loads((task_dir / 'task.json').read_text(encoding='utf-8'))
    run = json.loads(
        (task_dir / 'runs' / RUN_ID / 'run.json').read_text(encoding='utf-8')
    )
    assert task['execution_mode'] == 'replay'
    assert task['task_card']['execution_mode'] == 'replay'
    assert run['execution_mode'] == 'replay'
    assert run['task_card']['execution_mode'] == 'replay'

    with sqlite3.connect(loaded_uploads / 'chengzhu.db') as connection:
        database_cards = connection.execute(
            'SELECT task_card_json FROM task_run WHERE task_id = ?', (TASK_ID,)
        ).fetchall()
    assert len(database_cards) == 2
    assert all(
        json.loads(raw_card)['execution_mode'] == 'replay'
        for (raw_card,) in database_cards
    )

    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(loaded_uploads))
    replay_store = LocalReplayArtifactStore(TASK_ID, RUN_ID)
    assert replay_store.resolve('report.json').is_file()
    with pytest.raises(PermissionError, match='read_only'):
        replay_store.put_file('ignored')


def test_demo_loader_does_not_relabel_entries_it_skips(tmp_path):
    seed = tmp_path / 'seed'
    uploads = tmp_path / 'uploads'
    seed.mkdir()
    uploads.mkdir()
    (seed / 'tasks').mkdir()
    existing = uploads / 'tasks' / 'user_task'
    existing.mkdir(parents=True)
    existing_task = existing / 'task.json'
    existing_task.write_text(
        json.dumps({
            'task_card': {
                'deliverable': 'summary',
                'execution_mode': 'agentteams',
            },
        }),
        encoding='utf-8',
    )

    copied = load_demo(str(seed), str(uploads), force=False)

    assert copied == []
    payload = json.loads(existing_task.read_text(encoding='utf-8'))
    assert payload['task_card']['execution_mode'] == 'agentteams'
