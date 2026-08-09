"""Immutable AgentTeams artifact publishing with local replay compatibility."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import stat
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, List, Optional

from ..config import Config
from ..models.research_task import task_artifact_folder
from ..observability import traced_span


_ARTIFACT_ID_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.:-]{0,199}$')


def _artifact_identity(value: object, field: str, *, limit: int = 120) -> str:
    result = str(value or '').strip()
    if (
        not result
        or len(result) > limit
        or any(ord(character) < 32 for character in result)
    ):
        raise ValueError(f'invalid_{field}')
    return result


def _artifact_object_name(
    task_id: str,
    run_id: str,
    relative_path: str,
    *,
    digest: str,
    artifact_type: str,
    producer: str,
    schema_version: int,
) -> str:
    """Build an application-level immutable, content-addressed object key.

    MinIO's bucket versioning is an operational defence, but correctness must
    not depend on a mutable bucket setting.  A new byte sequence or provenance
    tuple therefore always resolves to a different key.  Concurrent retries of
    the same artifact resolve to the same key and are safe to replay.
    """

    if not _ARTIFACT_ID_RE.fullmatch(str(task_id or '')):
        raise ValueError('invalid_task_id')
    if not _ARTIFACT_ID_RE.fullmatch(str(run_id or '')):
        raise ValueError('invalid_run_id')
    canonical_provenance = json.dumps(
        {
            'artifact_type': artifact_type,
            'producer': producer,
            'schema_version': schema_version,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(',', ':'),
    ).encode('utf-8')
    provenance_digest = hashlib.sha256(canonical_provenance).hexdigest()[:20]
    return (
        f'chengzhu/{task_id}/{run_id}/sha256/{digest}/'
        f'{provenance_digest}/{relative_path}'
    )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_type: str
    uri: str
    sha256: str
    size_bytes: int
    producer: str
    schema_version: int = 1

    def to_dict(self):
        return asdict(self)


def file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _validated_artifact_path(task_id: str, run_id: str, path: str) -> tuple[str, str]:
    root = os.path.realpath(task_artifact_folder(task_id, run_id))
    resolved = os.path.realpath(path)
    if resolved == root or not resolved.startswith(root + os.sep):
        raise ValueError('artifact_path_outside_run')
    if os.path.islink(path) or not os.path.isfile(path):
        raise ValueError('artifact_must_be_regular_file')
    return resolved, os.path.relpath(resolved, root).replace(os.sep, '/')


def _read_artifact_snapshot(path: str) -> tuple[bytes, str]:
    """Read one coherent upload payload through a no-follow descriptor.

    The returned digest is computed from the exact in-memory bytes later sent
    to MinIO.  This removes the hash-then-reopen race of ``fput_object`` and
    ensures that an ArtifactRef can never name bytes different from its SHA.
    """

    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise ValueError('artifact_must_be_regular_file') from error
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError('artifact_must_be_regular_file')
        max_bytes = int(Config.MAX_CONTENT_LENGTH)
        if info.st_size < 0 or info.st_size > max_bytes:
            raise ValueError('artifact_size_limit_exceeded')
        payload = bytearray()
        digest = hashlib.sha256()
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
            digest.update(chunk)
            if len(payload) > max_bytes:
                raise ValueError('artifact_size_limit_exceeded')
        return bytes(payload), digest.hexdigest()
    finally:
        os.close(descriptor)


class LocalReplayArtifactStore:
    """Read-only adapter for demo_seed and legacy local bundles."""

    def __init__(self, task_id: str, run_id: str):
        self.root = Path(task_artifact_folder(task_id, run_id)).resolve()

    def resolve(self, relative_path: str) -> Path:
        candidate = (self.root / str(relative_path)).resolve()
        if candidate == self.root or self.root not in candidate.parents:
            raise ValueError('artifact_path_outside_run')
        if not candidate.is_file() or candidate.is_symlink():
            raise FileNotFoundError(str(relative_path))
        return candidate

    def put_file(self, *_args, **_kwargs):
        raise PermissionError('replay_artifact_store_is_read_only')


class AgentTeamsMinioArtifactStore:
    def __init__(self):
        endpoint = urllib.parse.urlparse(Config.AGENTTEAMS_FS_ENDPOINT)
        if endpoint.scheme not in {'http', 'https'} or not endpoint.netloc:
            raise ValueError('invalid_agentteams_fs_endpoint')
        if not Config.AGENTTEAMS_FS_ACCESS_KEY or not Config.AGENTTEAMS_FS_SECRET_KEY:
            raise RuntimeError('agentteams_fs_credentials_missing')
        try:
            from minio import Minio
        except ImportError as error:
            raise RuntimeError('minio_dependency_missing') from error
        self.bucket = Config.AGENTTEAMS_FS_BUCKET
        self.client = Minio(
            endpoint.netloc,
            access_key=Config.AGENTTEAMS_FS_ACCESS_KEY,
            secret_key=Config.AGENTTEAMS_FS_SECRET_KEY,
            secure=endpoint.scheme == 'https',
            region=None,
        )
        if not self.client.bucket_exists(self.bucket):
            self.client.make_bucket(self.bucket)

    def put_file(
        self,
        task_id: str,
        run_id: str,
        path: str,
        *,
        artifact_type: str,
        producer: str,
        schema_version: int = 1,
    ) -> ArtifactRef:
        artifact_type = _artifact_identity(artifact_type, 'artifact_type')
        producer = _artifact_identity(producer, 'producer')
        try:
            schema_version = int(schema_version)
        except (TypeError, ValueError):
            raise ValueError('invalid_schema_version') from None
        if schema_version < 1 or schema_version > 2_147_483_647:
            raise ValueError('invalid_schema_version')
        resolved, relative = _validated_artifact_path(task_id, run_id, path)
        payload, digest = _read_artifact_snapshot(resolved)
        size_bytes = len(payload)
        object_name = _artifact_object_name(
            task_id,
            run_id,
            relative,
            digest=digest,
            artifact_type=artifact_type,
            producer=producer,
            schema_version=schema_version,
        )
        try:
            existing = self.client.stat_object(self.bucket, object_name)
        except Exception as error:
            # minio.error.S3Error is imported lazily to keep replay dependency-free.
            if getattr(error, 'code', '') not in {'NoSuchKey', 'NoSuchObject', 'NoSuchBucket'}:
                raise
        else:
            metadata = {str(k).lower(): str(v) for k, v in (existing.metadata or {}).items()}
            prior = metadata.get('x-amz-meta-sha256') or metadata.get('sha256')
            prior_producer = (
                metadata.get('x-amz-meta-producer') or metadata.get('producer')
            )
            prior_schema = (
                metadata.get('x-amz-meta-schema-version')
                or metadata.get('schema-version')
            )
            prior_type = (
                metadata.get('x-amz-meta-artifact-type')
                or metadata.get('artifact-type')
            )
            if (
                prior == digest
                and int(existing.size) == size_bytes
                and prior_producer == producer
                and prior_schema == str(schema_version)
                and prior_type == artifact_type
            ):
                return ArtifactRef(
                    artifact_type=artifact_type,
                    uri=f's3://{self.bucket}/{object_name}',
                    sha256=digest,
                    size_bytes=int(existing.size),
                    producer=producer,
                    schema_version=schema_version,
                )
            raise FileExistsError('immutable_artifact_conflict')
        self.client.put_object(
            self.bucket,
            object_name,
            io.BytesIO(payload),
            size_bytes,
            metadata={
                'sha256': digest,
                'producer': producer,
                'schema-version': str(schema_version),
                'artifact-type': artifact_type,
            },
        )
        return ArtifactRef(
            artifact_type=artifact_type,
            uri=f's3://{self.bucket}/{object_name}',
            sha256=digest,
            size_bytes=size_bytes,
            producer=producer,
            schema_version=schema_version,
        )


def publish_artifacts(
    task_id: str,
    run_id: str,
    paths: Iterable[str],
    *,
    artifact_type: str,
    producer: str,
    schema_version: int = 1,
) -> tuple[List[ArtifactRef], bool]:
    """Publish files to MinIO, or return explicit local degraded refs.

    The boolean is true only when MinIO was unavailable and policy allowed the
    compatibility mirror to remain temporarily authoritative.
    """

    selected = [str(path) for path in paths if path and os.path.isfile(path)]
    with traced_span(
        'agentteams.artifact.publish',
        attributes={
            'task_id': task_id,
            'run_id': run_id,
            'artifact_type': artifact_type,
            'producer': producer,
            'artifact_count': len(selected),
        },
    ):
        try:
            store = AgentTeamsMinioArtifactStore()
            return [
                store.put_file(
                    task_id,
                    run_id,
                    path,
                    artifact_type=artifact_type,
                    producer=producer,
                    schema_version=schema_version,
                )
                for path in selected
            ], False
        except (FileExistsError, ValueError):
            raise
        except Exception:
            if Config.AGENTTEAMS_ARTIFACT_REQUIRED:
                raise
            refs: List[ArtifactRef] = []
            for path in selected:
                resolved, relative = _validated_artifact_path(task_id, run_id, path)
                refs.append(ArtifactRef(
                    artifact_type=artifact_type,
                    uri=f'local://{task_id}/{run_id}/{relative}',
                    sha256=file_sha256(resolved),
                    size_bytes=os.path.getsize(resolved),
                    producer=producer,
                    schema_version=int(schema_version),
                ))
            return refs, True
