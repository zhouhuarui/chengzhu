"""Immutable MinIO artifact provenance tests."""

from __future__ import annotations

import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.services.artifact_store import (
    AgentTeamsMinioArtifactStore,
    _artifact_object_name,
    file_sha256,
)


class _ExistingObjectClient:
    def __init__(self, metadata, size):
        self.metadata = metadata
        self.size = size

    def stat_object(self, _bucket, _name):
        return SimpleNamespace(metadata=self.metadata, size=self.size)


class _RecordingObjectClient:
    def __init__(self):
        self.names = []

    def stat_object(self, _bucket, _name):
        error = RuntimeError('missing')
        error.code = 'NoSuchKey'
        raise error

    def put_object(self, _bucket, name, stream, size, metadata):
        payload = stream.read()
        assert len(payload) == size
        self.names.append((name, metadata, payload))


def test_existing_minio_object_requires_matching_provenance(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    run_folder = tmp_path / 'tasks' / 'task-a' / 'runs' / 'run-a'
    run_folder.mkdir(parents=True)
    artifact = run_folder / 'evidence.json'
    artifact.write_text('{"evidence":[]}', encoding='utf-8')
    digest = file_sha256(str(artifact))
    store = object.__new__(AgentTeamsMinioArtifactStore)
    store.bucket = 'agentteams-storage'

    store.client = _ExistingObjectClient(
        {
            'x-amz-meta-sha256': digest,
            'x-amz-meta-producer': 'different-producer',
            'x-amz-meta-schema-version': '1',
            'x-amz-meta-artifact-type': 'frozen-context',
        },
        artifact.stat().st_size,
    )
    with pytest.raises(FileExistsError, match='immutable_artifact_conflict'):
        store.put_file(
            'task-a', 'run-a', str(artifact),
            artifact_type='frozen-context', producer='chengzhu-backend',
        )

    store.client = _ExistingObjectClient(
        {
            'x-amz-meta-sha256': digest,
            'x-amz-meta-producer': 'chengzhu-backend',
            'x-amz-meta-schema-version': '1',
            'x-amz-meta-artifact-type': 'frozen-context',
        },
        artifact.stat().st_size,
    )
    ref = store.put_file(
        'task-a', 'run-a', str(artifact),
        artifact_type='frozen-context', producer='chengzhu-backend',
    )
    assert ref.sha256 == digest
    assert ref.producer == 'chengzhu-backend'
    assert ref.schema_version == 1


def test_minio_keys_are_content_and_provenance_addressed(tmp_path, monkeypatch):
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    run_folder = tmp_path / 'tasks' / 'task-a' / 'runs' / 'run-a'
    run_folder.mkdir(parents=True)
    artifact = run_folder / 'evidence.json'
    artifact.write_text('{"evidence":[1]}', encoding='utf-8')
    store = object.__new__(AgentTeamsMinioArtifactStore)
    store.bucket = 'agentteams-storage'
    store.client = _RecordingObjectClient()

    first = store.put_file(
        'task-a', 'run-a', str(artifact),
        artifact_type='frozen-context', producer='chengzhu-backend',
    )
    artifact.write_text('{"evidence":[2]}', encoding='utf-8')
    second = store.put_file(
        'task-a', 'run-a', str(artifact),
        artifact_type='frozen-context', producer='chengzhu-backend',
    )

    assert first.uri != second.uri
    assert f'/sha256/{first.sha256}/' in first.uri
    assert f'/sha256/{second.sha256}/' in second.uri
    assert first.uri.endswith('/evidence.json')


def test_provenance_changes_content_address_even_for_same_bytes():
    common = {
        'task_id': 'task-a',
        'run_id': 'run-a',
        'relative_path': 'report.json',
        'digest': 'a' * 64,
        'artifact_type': 'report',
        'schema_version': 1,
    }
    writer = _artifact_object_name(producer='report-writer', **common)
    reviewer = _artifact_object_name(producer='compliance-reviewer', **common)
    assert writer != reviewer


@pytest.mark.parametrize('schema_version', [0, -1, 'invalid'])
def test_invalid_artifact_schema_is_rejected(tmp_path, monkeypatch, schema_version):
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    run_folder = tmp_path / 'tasks' / 'task-a' / 'runs' / 'run-a'
    run_folder.mkdir(parents=True)
    artifact = run_folder / 'report.json'
    artifact.write_text('{}', encoding='utf-8')
    store = object.__new__(AgentTeamsMinioArtifactStore)
    store.bucket = 'agentteams-storage'
    store.client = _RecordingObjectClient()
    with pytest.raises(ValueError, match='invalid_schema_version'):
        store.put_file(
            'task-a', 'run-a', str(artifact), artifact_type='report',
            producer='report-writer', schema_version=schema_version,
        )
