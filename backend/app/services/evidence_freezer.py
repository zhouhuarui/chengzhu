"""Deterministic evidence-freeze domain service shared by both runtimes."""

from __future__ import annotations

import json
import os
from typing import Any, Dict, Optional, Tuple

from ..models.research_task import ResearchTask
from .evidence_store import EvidenceStore


def _load_evidence_index(path: str) -> Dict[str, Any]:
    with open(path, 'r', encoding='utf-8') as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError('冻结证据索引格式无效')
    return value


def freeze_evidence(
    task: ResearchTask,
    run_id: Optional[str],
) -> Tuple[EvidenceStore, Dict[str, Any]]:
    """Publish staging cards once, then reopen only the immutable index."""

    if not run_id:
        store = EvidenceStore(task.task_id)
        return store, {
            'schema_version': 1,
            'task_id': task.task_id,
            'run_id': None,
            'items': [
                {
                    'evidence_uid': card.evidence_uid,
                    'display_id': store.display_id(card),
                    'card': card.to_dict(),
                }
                for card in store.cards
            ],
        }

    run_folder = task.run_folder(run_id)
    index_path = os.path.join(run_folder, 'evidence_index.json')
    if not os.path.isfile(index_path):
        staging = EvidenceStore(task.task_id, run_id=run_id, allow_staging=True)
        if not staging.cards:
            raise ValueError('本次运行没有可冻结的证据，拒绝读取历史残留')
        staging.freeze_to_run(run_id)
    frozen = EvidenceStore(task.task_id, run_id=run_id)
    return frozen, _load_evidence_index(index_path)

