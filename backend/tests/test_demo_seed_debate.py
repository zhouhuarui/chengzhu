"""Keyless competition demo generation, loading and API replay."""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..'))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from scripts.build_debate_demo_seed import (
    RUN_ID,
    TASK_ID,
    build_demo,
    validate_demo,
)
from scripts.load_demo import load_demo


def test_keyless_debate_demo_survives_load_and_all_read_apis(tmp_path):
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
