"""Transactional commit marker for the three-file report bundle.

Legacy tasks predate these markers and remain readable when no transaction
marker exists. Once a publication-start marker is present, however, readers
must see a matching commit manifest and all expected file digests before the
report is considered published.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Mapping, Optional


REPORT_FILES = ('report.json', 'report.md', 'full_report.md')
REPORT_PUBLISH_STARTED = 'report_publish_started.json'
REPORT_COMMIT = 'report_commit.json'


def text_sha256(value: str) -> str:
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def build_report_commit(
    *,
    task_id: str,
    run_id: Optional[str],
    transaction_id: str,
    contents: Mapping[str, str],
) -> Dict[str, Any]:
    if set(contents) != set(REPORT_FILES):
        raise ValueError('report commit requires the complete report file set')
    return {
        'schema_version': 1,
        'task_id': task_id,
        'run_id': run_id,
        'transaction_id': transaction_id,
        'files': {
            name: {
                'sha256': text_sha256(str(contents[name])),
                'bytes': len(str(contents[name]).encode('utf-8')),
            }
            for name in REPORT_FILES
        },
    }


def _read_object(path: str) -> Dict[str, Any]:
    try:
        with open(path, 'r', encoding='utf-8') as source:
            value = json.load(source)
    except (OSError, TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def report_bundle_is_committed(
    folder: str,
    *,
    task_id: Optional[str] = None,
    run_id: Optional[str] = None,
) -> bool:
    """Return whether a report is complete, preserving marker-less legacy data."""

    report_path = os.path.join(folder, 'report.json')
    if not os.path.isfile(report_path):
        return False

    started_path = os.path.join(folder, REPORT_PUBLISH_STARTED)
    if not os.path.isfile(started_path):
        # Reports created before the transaction-marker migration are valid
        # legacy artifacts. New publication always writes ``started`` first.
        return True

    started = _read_object(started_path)
    committed = _read_object(os.path.join(folder, REPORT_COMMIT))
    if not started or not committed:
        return False
    if started.get('transaction_id') != committed.get('transaction_id'):
        return False
    if started.get('task_id') != committed.get('task_id'):
        return False
    if started.get('run_id') != committed.get('run_id'):
        return False
    if task_id is not None and committed.get('task_id') != task_id:
        return False
    if run_id is not None and committed.get('run_id') != run_id:
        return False

    files = committed.get('files')
    if not isinstance(files, dict) or set(files) != set(REPORT_FILES):
        return False
    for name in REPORT_FILES:
        metadata = files.get(name)
        path = os.path.join(folder, name)
        if not isinstance(metadata, dict) or not os.path.isfile(path):
            return False
        try:
            expected_bytes = int(metadata.get('bytes'))
        except (TypeError, ValueError):
            return False
        if os.path.getsize(path) != expected_bytes:
            return False
        if file_sha256(path) != str(metadata.get('sha256') or ''):
            return False
    return True
