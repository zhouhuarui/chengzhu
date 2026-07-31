"""Datayes 溯源、任务级去重与修订追踪的离线测试。"""

from __future__ import annotations

import json
import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, ResearchTaskStatus
from app.services.evidence_store import EvidenceStore
from app.services.graph_ingest import ingest_task_evidence
from app.services.tracking_service import graph_changes_since
from app.tools.schema import EvidenceCard, reliability_for
from app.utils import db as dbmod
from app.utils import graph_client as graphmod


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'chengzhu.db'))
    monkeypatch.setattr(graphmod.GraphClient, '_try_init_neo4j', lambda self: None)
    graphmod._instances.clear()
    dbmod.init_db()
    yield tmp_path
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None
    graphmod._instances.clear()


def _card(fingerprint: str = 'fp-v1', update_time: str = '2026-01-01T09:00:00') -> EvidenceCard:
    return EvidenceCard(
        source_type='financial_report',
        title='贵州茅台 2025 年度利润表',
        url=None,
        publish_time='2026-01-01T08:00:00',
        source_name='Datayes',
        symbol='600519',
        excerpt='营业收入 100 元',
        structured={'revenue': 100},
        provenance={
            'provider': 'datayes',
            'api': 'getFdmtIS',
            'record_key': f'getFdmtIS:600519:2025:{fingerprint}',
            'business_key': 'getFdmtIS:600519:2025:A:1',
            'as_of': '2025-12-31',
            'update_time': update_time,
            'warehouse_watermark': '2026-01-01T00:00:00',
            'row_fingerprint': fingerprint,
            'upstream_source': '上市公司公告',
            'license_scope': 'private_derived_only',
        },
        reliability=5,
        fetch_tool='fetch_financial_statements',
    )


def _write_card(root, task_id: str, card: EvidenceCard) -> None:
    folder = root / 'tasks' / task_id / 'evidence'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'financial.jsonl').write_text(
        json.dumps(card.to_dict(), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )


def test_nullable_url_and_provenance_roundtrip(isolated_runtime):
    card = _card()
    card.provenance['authorization'] = 'Bearer must-not-leak'
    _write_card(isolated_runtime, 'task_provenance', card)
    store = EvidenceStore('task_provenance')

    assert len(store.cards) == 1
    saved = store.cards[0].to_dict()
    assert saved['url'] is None
    assert saved['card_id'] == 1
    assert saved['provenance']['api'] == 'getFdmtIS'
    assert saved['provenance']['license_scope'] == 'private_derived_only'
    assert 'authorization' not in saved['provenance']
    assert reliability_for('Datayes', 'financial_report') == 5
    assert reliability_for('Datayes', 'industry_data') == 4


def test_evidence_card_keeps_legacy_positional_argument_order():
    card = EvidenceCard(
        'announcement', '旧调用', 'https://example.test/1', '2026-01-01',
        '测试来源', '600519', '摘录', {'legacy': True}, 5, 'legacy_tool', 7,
    )

    assert card.reliability == 5
    assert card.fetch_tool == 'legacy_tool'
    assert card.card_id == 7
    assert card.provenance is None

    from app.services.report_assembler import _integrity_note

    assert '失败或超时' in _integrity_note('datayes_api_failed:retCode=-7')
    assert '权限校验失败' in _integrity_note('datayes_api_failed:HTTP 403')
    assert '权限校验失败' in _integrity_note('datayes_api_failed:鉴权或接口权限不足')
    assert '触发限流' in _integrity_note('datayes_api_failed:retCode=-16 调用频率超限')


def test_private_datayes_config_rejects_wildcard_cors(monkeypatch):
    monkeypatch.setattr(Config, 'DATAYES_ENABLED', True)
    monkeypatch.setattr(Config, 'DATAYES_LICENSE_MODE', 'private_derived_only')
    monkeypatch.setattr(Config, 'DATAYES_PUBLIC_EXPORT', False)
    monkeypatch.setattr(Config, 'CORS_ALLOWED_ORIGINS', ['*'])

    errors = Config.validate(strict=False)

    assert any('CORS_ALLOWED_ORIGINS=*' in error for error in errors)

    from app import create_app

    with pytest.raises(RuntimeError, match=r'CORS_ALLOWED_ORIGINS=\*'):
        create_app(Config)


def test_private_mode_meta_uses_authorized_data_wording(isolated_runtime, monkeypatch):
    monkeypatch.setattr(Config, 'DATAYES_ENABLED', True)
    monkeypatch.setattr(Config, 'DATAYES_DATA_DIR', str(isolated_runtime))
    monkeypatch.setattr(Config, 'DATAYES_LICENSE_MODE', 'private_derived_only')
    monkeypatch.setattr(Config, 'CORS_ALLOWED_ORIGINS', ['http://127.0.0.1:3000'])
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)

    from app import create_app

    response = create_app(Config).test_client().get('/api/meta/disclaimer')
    payload = response.get_json()['data']

    assert response.status_code == 200
    assert '基于已授权数据与公开信息' in payload['disclaimer']
    assert '已授权 Datayes 派生数据及公开渠道' in payload['data_notice']


def test_evidence_api_serializes_provenance(isolated_runtime, monkeypatch):
    folder = isolated_runtime / 'tasks' / 'task_api' / 'evidence'
    folder.mkdir(parents=True, exist_ok=True)
    raw = _card().to_dict()
    # 模拟 Provider 误带请求凭证：Evidence API 必须以 allowlist 再过滤。
    raw['provenance']['authorization'] = 'Bearer must-not-leak'
    raw['provenance']['token'] = 'must-not-leak'
    raw['provenance']['provider'] = 'public_fallback'
    raw['structured'].update({
        'degraded': True,
        'datayes_degradation_reasons': [
            'warehouse_stale',
            'datayes_token_missing',
            'datayes_api_failed:Authorization: Bearer must-not-leak',
            'public_fallback',
        ],
    })
    (folder / 'financial.jsonl').write_text(
        json.dumps(raw, ensure_ascii=False) + '\n',
        encoding='utf-8',
    )
    monkeypatch.setattr(Config, 'TRACKING_CRON_ENABLED', False)

    from app import create_app

    app = create_app(Config)
    app.config['TESTING'] = True
    response = app.test_client().get('/api/task/task_api/evidence')
    payload = response.get_json()

    assert response.status_code == 200
    assert payload['data']['items'][0]['url'] is None
    assert payload['data']['items'][0]['card_id'] == 1
    assert payload['data']['items'][0]['provenance']['row_fingerprint'] == 'fp-v1'
    public_provenance = payload['data']['items'][0]['provenance']
    assert 'authorization' not in public_provenance
    assert 'token' not in public_provenance
    serialized = json.dumps(payload, ensure_ascii=False).lower()
    assert 'must-not-leak' not in serialized

    allowed = app.test_client().get(
        '/api/task/task_api/evidence',
        headers={'Origin': 'http://localhost:3000'},
    )
    blocked = app.test_client().get(
        '/api/task/task_api/evidence',
        headers={'Origin': 'https://untrusted.example'},
    )
    assert allowed.headers.get('Access-Control-Allow-Origin') == 'http://localhost:3000'
    assert blocked.headers.get('Access-Control-Allow-Origin') is None

    from app.services.report_assembler import assemble_report

    report = assemble_report('task_api', {
        'title': '测试报告',
        'summary': '结构化溯源测试',
        'sections': [{'title': '摘要', 'content': '营业收入 100 元[E1]'}],
    })
    assert 'getFdmtIS' in report['markdown']
    assert 'private_derived_only' in report['markdown']
    assert 'must-not-leak' not in report['markdown']
    assert '## 数据完整性说明' in report['markdown']
    assert '仓库水位已过期' in report['markdown']
    assert 'Token 未配置' in report['markdown']
    assert 'DataAPI 调用失败或超时' in report['markdown']
    assert '公共数据源降级' in report['markdown']
    assert len(report['integrity_notes']) == 4
    assert '基于已授权数据与公开信息' in report['disclaimer']
    assert '仅整理已授权数据与公开信息' in report['markdown']
    assert '仅整理公开信息' not in report['markdown']


def test_structured_revision_invalidates_previous_version(isolated_runtime):
    task_id = 'task_revision'
    _write_card(isolated_runtime, task_id, _card())
    first = ingest_task_evidence(task_id)
    watermark = datetime.now().astimezone().isoformat(timespec='microseconds')

    revised = _card('fp-v2', '2026-02-01T09:00:00')
    revised.structured['revenue'] = 110
    revised.excerpt = '营业收入修订为 110 元'
    _write_card(isolated_runtime, task_id, revised)
    second = ingest_task_evidence(task_id)

    client = graphmod.get_graph_client(graphmod.project_group_id(task_id))
    changes = graph_changes_since(client, watermark)

    assert first['new_facts'] == 1
    assert second['revised_facts'] == 1
    assert second['invalidated_facts'] == 1
    assert len(changes['new_facts']) == 1
    assert len(changes['changed_facts']) == 1
    assert changes['new_facts'][0]['row_fingerprint'] == 'fp-v2'
    assert changes['changed_facts'][0]['row_fingerprint'] == 'fp-v1'


def test_run_now_refreshes_into_independent_batch_and_detects_revision(isolated_runtime, monkeypatch):
    task_id = 'task_tracking_refresh'
    task = ResearchTask(task_id=task_id, requirement='追踪贵州茅台财务修订')
    task.status = ResearchTaskStatus.COMPLETED
    task.set_task_card({
        'deliverable': 'tracking',
        'symbols': [{'code': '600519', 'name': '贵州茅台'}],
        'time_window': {'start': '2025-01-01', 'end': '2026-01-01'},
        'info_types': ['financial_report'],
    })
    original = _card()
    _write_card(isolated_runtime, task_id, original)
    original_path = isolated_runtime / 'tasks' / task_id / 'evidence' / 'financial.jsonl'
    original_text = original_path.read_text(encoding='utf-8')
    ingest_task_evidence(task_id)

    sub_id = 'sub_tracking_refresh'
    old_watermark = dbmod.now_iso()
    dbmod.insert_tracking_sub(sub_id, task_id, 'daily', 8, watermark=old_watermark)

    revised = _card('fp-v2', '2026-02-01T09:00:00')
    revised.structured['revenue'] = 110
    revised.excerpt = '营业收入修订为 110 元'
    called = []

    def fake_call_tool(name, **params):
        called.append((name, params))
        if name == 'fetch_financial_statements' and params.get('statement') == 'income':
            return [revised]
        return []

    from app.services import tracking_service

    monkeypatch.setattr(tracking_service, 'call_tool', fake_call_tool)
    result = tracking_service.run_subscription_now(sub_id)

    assert original_path.read_text(encoding='utf-8') == original_text
    tracking_files = list(original_path.parent.glob('zz_tracking_*.jsonl'))
    assert len(tracking_files) == 1
    assert result['refresh']['tool_calls'] == 4
    assert result['refresh']['cards'] == 1
    assert result['refresh']['failures'] == []
    assert result['new_facts'] == 1
    assert result['changed_facts'] == 1
    assert result['ingest']['revised_facts'] == 1
    assert result['refresh']['license_scopes'] == ['private_derived_only']
    assert '已授权数据与公开信息' in result['markdown']
    assert '不得进入公开 Demo' in result['markdown']
    assert {name for name, _ in called} == {
        'fetch_financial_statements', 'fetch_financial_indicators',
    }
    updated_sub = dbmod.get_tracking_sub(sub_id)
    assert updated_sub['watermark'] >= old_watermark
    assert dbmod.list_briefs(sub_id)[0]['run_id'] == result['refresh']['run_id']


def test_tracking_failure_keeps_watermark_and_redacts_error(isolated_runtime, monkeypatch):
    task_id = 'task_tracking_failure'
    task = ResearchTask(task_id=task_id, requirement='追踪新闻')
    task.status = ResearchTaskStatus.COMPLETED
    task.set_task_card({
        'deliverable': 'tracking',
        'symbols': [{'code': '600519', 'name': '贵州茅台'}],
        'time_window': {'start': '2025-01-01', 'end': '2026-01-01'},
        'info_types': ['news'],
    })
    sub_id = 'sub_tracking_failure'
    old_watermark = '2026-01-01T00:00:00+08:00'
    dbmod.insert_tracking_sub(sub_id, task_id, 'daily', 8, watermark=old_watermark)

    from app.services import tracking_service

    def failed_tool(*_args, **_kwargs):
        raise RuntimeError('Authorization: Bearer super-secret-token')

    monkeypatch.setattr(tracking_service, 'call_tool', failed_tool)
    result = tracking_service.run_subscription_now(sub_id)

    assert result['refresh']['failures']
    assert 'super-secret-token' not in json.dumps(result, ensure_ascii=False)
    assert '[REDACTED]' in result['refresh']['failures'][0]['error']
    assert dbmod.get_tracking_sub(sub_id)['watermark'] == old_watermark


def test_same_public_evidence_is_not_deduped_across_tasks(isolated_runtime):
    card = EvidenceCard(
        source_type='announcement',
        title='同一公告',
        url='https://example.com/notice/1',
        publish_time='2026-01-01T00:00:00',
        source_name='巨潮资讯',
        symbol='600519',
    )
    _write_card(isolated_runtime, 'task_a', card)
    _write_card(isolated_runtime, 'task_b', card)

    result_a = ingest_task_evidence('task_a')
    result_b = ingest_task_evidence('task_b')

    assert result_a['ingested'] == 1
    assert result_b['ingested'] == 1


def test_public_demo_seed_contains_no_datayes_raw_fixture():
    demo_root = Path(__file__).resolve().parents[2] / 'demo_seed'
    forbidden = (
        'datayes_raw', 'datayes_api', 'datayes_warehouse', 'wmcloud.com',
        'datayes_token', 'authorization', 'bearer ',
    )
    leaks = []
    for path in demo_root.rglob('*'):
        if not path.is_file() or path.suffix.lower() not in {'.json', '.jsonl', '.md', '.txt'}:
            continue
        text = path.read_text(encoding='utf-8', errors='ignore').lower()
        if any(marker in text for marker in forbidden):
            leaks.append(str(path.relative_to(demo_root)))
    seed_db = demo_root / 'chengzhu.db'
    if seed_db.is_file():
        conn = sqlite3.connect(f'file:{seed_db}?mode=ro', uri=True)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM evidence_card "
                "WHERE lower(card_json) LIKE '%datayes%' "
                "OR lower(card_json) LIKE '%wmcloud%'"
            ).fetchone()
            if row and int(row[0]) > 0:
                leaks.append('chengzhu.db:evidence_card')
        except sqlite3.OperationalError:
            pass
        finally:
            conn.close()
    assert not leaks, f'公开 demo_seed 含 Datayes 原始数据或凭证标记: {leaks}'


def test_demo_seed_builder_rejects_private_datayes_artifacts(tmp_path):
    import importlib.util

    script = Path(__file__).resolve().parents[2] / 'scripts' / 'build_demo_seed.py'
    spec = importlib.util.spec_from_file_location('build_demo_seed', script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)

    clean = tmp_path / 'clean'
    clean.mkdir()
    (clean / 'report.md').write_text('公开合成演示数据', encoding='utf-8')
    assert module.detect_private_datayes_artifacts(str(clean)) == []

    private = tmp_path / 'private'
    private.mkdir()
    (private / 'evidence.jsonl').write_text(
        '{"provenance":{"provider":"datayes_warehouse"}}',
        encoding='utf-8',
    )
    assert module.detect_private_datayes_artifacts(str(private)) == ['evidence.jsonl']

    database_root = tmp_path / 'database_private'
    database_root.mkdir()
    database = database_root / 'chengzhu.db'
    conn = sqlite3.connect(database)
    try:
        conn.execute('CREATE TABLE tool_log (message TEXT)')
        conn.execute('INSERT INTO tool_log VALUES (?)', ('datayes_api fallback',))
        conn.commit()
    finally:
        conn.close()
    assert module.detect_private_datayes_artifacts(str(database_root)) == ['chengzhu.db']
