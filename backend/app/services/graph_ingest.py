"""EvidenceCard → 图谱 episode 摄入（05§2.3）。"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models.research_task import task_artifact_folder
from ..tools.schema import EvidenceCard, PROVENANCE_FIELDS
from ..utils import db as dbutil
from ..utils.graph_client import get_graph_client, project_group_id
from .agent_logger import AgentLogger
from .evidence_store import EvidenceStore


def publish_latest_graph(task_id: str, run_id: str) -> None:
    """Update the legacy graph alias only after the run report is published."""

    source = os.path.join(task_artifact_folder(task_id, run_id), 'graph.json')
    if not os.path.isfile(source):
        return
    with open(source, 'rb') as handle:
        payload = handle.read()
    latest_path = os.path.join(Config.UPLOAD_FOLDER, 'tasks', task_id, 'graph.json')
    latest_tmp = latest_path + f'.tmp-{uuid.uuid4().hex}'
    try:
        with open(latest_tmp, 'xb') as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(latest_tmp, latest_path)
    finally:
        if os.path.exists(latest_tmp):
            os.unlink(latest_tmp)


def _run_visualization_payload(store: EvidenceStore) -> Dict[str, Any]:
    """Build a run-local graph view without leaking the cumulative task graph."""

    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, Any]] = []
    company_ids = set()
    type_map = {
        'announcement': 'Disclosure',
        'financial_report': 'FinancialMetric',
        'news': 'Event',
        'research_report': 'Opinion',
        'industry_data': 'Industry',
        'uploaded_document': 'Disclosure',
    }
    for card in store.cards:
        uid = str(card.evidence_uid or f'card_{card.card_id}')
        evidence_id = f'evidence_{uid}'
        nodes.append({
            'id': evidence_id,
            'name': card.title,
            'type': type_map.get(card.source_type, 'Disclosure'),
            'summary': (card.excerpt or '')[:200],
            'evidence_uid': card.evidence_uid,
            'display_id': store.display_id(card),
        })
        if card.symbol:
            company_id = f'company_{card.symbol}'
            if company_id not in company_ids:
                company_ids.add(company_id)
                nodes.append({
                    'id': company_id,
                    'name': card.symbol,
                    'type': 'Company',
                    'summary': f'标的 {card.symbol}',
                })
            edges.append({
                'source': company_id,
                'target': evidence_id,
                'name': 'DISCLOSES',
                'fact': card.title[:120],
                'valid': True,
                'evidence_uid': card.evidence_uid,
            })
    return {
        'nodes': nodes,
        'edges': edges,
        'statistics': {
            'nodes': len(nodes),
            'edges': len(edges),
            'evidence_cards': len(store.cards),
            'backend': 'run_snapshot',
        },
        'run_id': store.run_id,
    }


def _revision_identity(card: EvidenceCard) -> tuple[str, str, str]:
    provenance = card.provenance or {}
    business_key = str(provenance.get('business_key') or '')
    row_fingerprint = str(provenance.get('row_fingerprint') or '')
    version_time = str(
        provenance.get('update_time')
        or provenance.get('as_of')
        or card.publish_time
        or ''
    )
    return business_key, row_fingerprint, version_time


def _dedup_key(card: EvidenceCard, task_id: str = '') -> str:
    """任务内去重；Datayes 结构化事实按业务键和版本指纹区分。"""
    business_key, row_fingerprint, _ = _revision_identity(card)
    if business_key and row_fingerprint:
        identity = f'{business_key}|{row_fingerprint}'
    else:
        identity = f'{card.url or ""}|{card.title or ""}'
    raw = f'{task_id}|{identity}'
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def card_to_episode_text(card: EvidenceCard) -> str:
    prefix = f'[E{card.card_id}]' if card.card_id else ''
    structured = json.dumps(card.structured or {}, ensure_ascii=False)[:800]
    provenance = card.provenance or {}
    trace = {
        k: provenance.get(k)
        for k in PROVENANCE_FIELDS
        if provenance.get(k) not in (None, '')
    }
    trace_text = f'\n结构化溯源: {json.dumps(trace, ensure_ascii=False)[:600]}' if trace else ''
    return (
        f'{prefix}[{card.source_type}|{card.source_name}|{card.publish_time}] {card.title}\n'
        f'{card.excerpt}\n结构化字段: {structured}{trace_text}'
    )


def ingest_task_evidence(
    task_id: str,
    logger: Optional[AgentLogger] = None,
    *,
    force: bool = False,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    logger = logger or AgentLogger(task_id, agent='graph_ingest')
    store = EvidenceStore(task_id, run_id=run_id, allow_staging=bool(run_id))
    client = get_graph_client(project_group_id(task_id))
    ingested = 0
    skipped = 0
    new_facts = 0
    revised_facts = 0
    historical_versions = 0
    invalidated_facts = 0
    errors: List[str] = []

    def check_deadline(stage: str) -> None:
        if deadline_epoch is not None:
            from ..utils.run_limits import ensure_time_remaining
            ensure_time_remaining(deadline_epoch, stage=stage)

    for card in store.cards:
        check_deadline('graph_ingest')
        key = _dedup_key(card, task_id)
        business_key, row_fingerprint, version_time = _revision_identity(card)
        # SQLite 去重（force 时仍写入图谱，仅跳过 DB 重复记录更新）
        try:
            existing = dbutil.get_evidence_dedup(key)
            if existing and not force:
                skipped += 1
                continue
        except Exception:
            pass

        try:
            observed_at = datetime.now().astimezone().isoformat(timespec='microseconds')
            revision = {'status': 'new'}
            if business_key and row_fingerprint:
                revision = client.revision_status(business_key, row_fingerprint, version_time)
                if revision.get('status') == 'unchanged' and not force:
                    skipped += 1
                    continue

            revision_status = revision.get('status') or 'new'
            is_historical = revision_status == 'historical'
            provenance = card.provenance or {}
            safe_provenance = {
                k: provenance.get(k)
                for k in PROVENANCE_FIELDS
                if provenance.get(k) not in (None, '')
            }
            body = card_to_episode_text(card)
            client.add_episode(
                body=body,
                reference_time=card.publish_time or datetime.now().isoformat(timespec='seconds'),
                episode_id=f'{task_id}_{run_id or "legacy"}_{card.card_id}_{key[:12]}',
                meta={
                    'symbol': card.symbol,
                    'source_type': card.source_type,
                    'title': card.title,
                    'card_id': card.card_id,
                    'url': card.url,
                    'business_key': business_key or None,
                    'row_fingerprint': row_fingerprint or None,
                    'version_time': version_time or None,
                    'observed_at': observed_at,
                    'valid': not is_historical,
                    'invalid_at': revision.get('current_version_time') if is_historical else None,
                    'provenance': safe_provenance or None,
                },
            )
            check_deadline('graph_ingest_result')
            # 新版本已经可靠落盘后再让旧版本失效，避免写入失败造成当前事实丢失。
            if revision_status == 'revised':
                invalidated_facts += client.invalidate_revision(
                    business_key,
                    row_fingerprint,
                    version_time or observed_at,
                    observed_at,
                )
            try:
                dbutil.insert_evidence_dedup(key, task_id, card.to_dict())
            except Exception:
                pass
            ingested += 1
            if revision_status == 'revised':
                revised_facts += 1
            elif revision_status == 'historical':
                historical_versions += 1
            else:
                new_facts += 1
        except Exception as e:
            errors.append(str(e))

    check_deadline('graph_statistics')
    stats = client.get_graph_statistics()
    check_deadline('graph_snapshot_publish')
    logger.log('graph_ingest', 'ingesting', {
        'ingested': ingested,
        'skipped': skipped,
        'new_facts': new_facts,
        'revised_facts': revised_facts,
        'historical_versions': historical_versions,
        'invalidated_facts': invalidated_facts,
        'errors': errors[:5],
        'stats': stats,
    })
    # 落盘 graph snapshot
    folder = task_artifact_folder(task_id, run_id)
    os.makedirs(folder, exist_ok=True)
    payload = _run_visualization_payload(store) if run_id else client.visualization_payload()
    graph_path = os.path.join(folder, 'graph.json')
    tmp_path = graph_path + f'.tmp-{uuid.uuid4().hex}'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, graph_path)
    return {
        'ingested': ingested,
        'skipped': skipped,
        'new_facts': new_facts,
        'revised_facts': revised_facts,
        'historical_versions': historical_versions,
        'invalidated_facts': invalidated_facts,
        'stats': stats,
        'errors': errors,
    }
