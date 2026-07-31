from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, ResearchTaskStatus
from app.services.pipeline import run_analysis_pipeline
from app.services.report_assembler import assemble_report, load_report
from app.services.evidence_store import EvidenceStore
from app.services.financial_normalizer import FinancialNormalizer
from app.utils import db as dbutil
from app.utils import graph_client as graphmod
from app.utils.llm_audit import estimate_cost_cny


@pytest.fixture
def isolated_debate_runtime(tmp_path, monkeypatch):
    connection = getattr(dbutil._local, 'conn', None)
    if connection:
        connection.close()
        dbutil._local.conn = None
    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'chengzhu.db'))
    monkeypatch.setattr(Config, 'TEXT_LLM_API_KEY', None)
    monkeypatch.setattr(Config, 'LLM_API_KEY', None)
    monkeypatch.setattr(graphmod.GraphClient, '_try_init_neo4j', lambda self: None)
    graphmod._instances.clear()
    dbutil.init_db()
    yield tmp_path
    connection = getattr(dbutil._local, 'conn', None)
    if connection:
        connection.close()
        dbutil._local.conn = None
    graphmod._instances.clear()


def _card(title: str, symbol: str, *, financial: bool) -> dict:
    structured = {'synthetic': True}
    source_type = 'announcement'
    if financial:
        source_type = 'financial_report'
        structured = {
            'synthetic': True,
            'statement': 'income',
            'REPORT_DATE': '2025-06-30',
            'period_type': 'H1',
            'accumulation_basis': 'cumulative',
            'merged_flag': 1,
            'currency': 'CNY',
            'TOTAL_OPERATE_INCOME_yi': '100.00',
        }
    return {
        'source_type': source_type,
        'title': title,
        'url': f'https://example.test/{symbol}/{source_type}',
        'publish_time': '2025-08-01',
        'source_name': '公开合成披露夹具',
        'symbol': symbol,
        'excerpt': f'{title}，仅用于离线协议验收。',
        'structured': structured,
        'reliability': 5,
        'fetch_tool': 'fixture',
    }


def test_debate_pipeline_freezes_only_current_run_and_discloses_keyless_fallback(
    isolated_debate_runtime: Path,
):
    task = ResearchTask(task_id='task_run_isolation', requirement='离线辩论回退验收')
    card = {
        'deliverable': 'compare',
        'analysis_mode': 'evidence_debate',
        'symbols': [
            {'code': '300750', 'name': '宁德时代'},
            {'code': '002594', 'name': '比亚迪'},
        ],
        'time_window': {'start': '2025-01-01', 'end': '2025-12-31'},
        'info_types': ['financial_report', 'announcement'],
        'focus_points': ['盈利质量'],
        'compare_dimensions': ['盈利质量'],
    }
    task.set_task_card(card)
    run_id = task.create_run(card)
    dbutil.insert_task_run(run_id, task.task_id, card, 'ingesting')
    dbutil.insert_debate_run(run_id, task.task_id, 'pending')

    run_evidence = Path(task.run_folder(run_id)) / 'evidence' / 'fixture.jsonl'
    current_cards = [
        _card('宁德时代 2025H1 财务披露', '300750', financial=True),
        _card('宁德时代半年度报告公告', '300750', financial=False),
    ]
    run_evidence.write_text(
        ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in current_cards),
        encoding='utf-8',
    )
    legacy = Path(task.folder) / 'evidence' / 'stale.jsonl'
    legacy.write_text(
        json.dumps(_card('上一轮残留证据不得出现', '002594', financial=False), ensure_ascii=False) + '\n',
        encoding='utf-8',
    )

    completed = run_analysis_pipeline(task.task_id, run_id=run_id)
    report = load_report(task.task_id, run_id=run_id)

    assert completed.status is ResearchTaskStatus.COMPLETED_PARTIAL
    assert report is not None
    assert report['analysis_mode'] == 'evidence_debate'
    assert report['debate_status'] == 'fallback_direct'
    assert any(section['title'] == '辩论运行说明' for section in report['sections'])
    assert '上一轮残留证据不得出现' not in report['markdown']
    assert (Path(task.run_folder(run_id)) / 'normalized_facts.jsonl').is_file()
    frozen = json.loads(
        (Path(task.run_folder(run_id)) / 'evidence_index.json').read_text(encoding='utf-8')
    )
    assert len(frozen['items']) == len(current_cards)
    assert all('上一轮残留' not in item['card']['title'] for item in frozen['items'])
    debate = dbutil.get_debate_run(run_id)
    assert debate and debate['status'] == 'failed'
    assert (Path(task.folder) / 'report.json').is_file()


def test_llm_cost_estimate_is_conservative_and_bounded():
    usage = {'prompt_tokens': 10_000, 'completion_tokens': 5_000}
    flash = estimate_cost_cny('deepseek', 'deepseek-v4-flash', usage)
    pro = estimate_cost_cny('deepseek', 'deepseek-v4-pro', usage)
    vision = estimate_cost_cny('dashscope', 'qwen3-vl-plus', usage)
    assert 0 < flash < pro < 2
    assert 0 < vision < 2


def test_report_gate_rejects_invalid_reference_and_mixed_period_chart(
    isolated_debate_runtime: Path,
):
    task = ResearchTask(task_id='task_report_gate', requirement='报告门禁')
    card = {
        'deliverable': 'compare',
        'analysis_mode': 'direct',
        'symbols': [{'code': '300750', 'name': '甲'}, {'code': '002594', 'name': '乙'}],
        'time_window': {'start': '2025-01-01', 'end': '2025-12-31'},
        'info_types': ['financial_report'],
    }
    task.set_task_card(card)
    run_id = task.create_run(card)
    evidence_path = Path(task.run_folder(run_id)) / 'evidence' / 'financial.jsonl'
    h1 = _card('甲 H1', '300750', financial=True)
    q1 = _card('乙 Q1', '002594', financial=True)
    q1['structured']['REPORT_DATE'] = '2025-03-31'
    q1['structured']['period_type'] = 'Q1'
    evidence_path.write_text(
        ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in (h1, q1)),
        encoding='utf-8',
    )
    store = EvidenceStore(task.task_id, run_id=run_id, allow_staging=True)
    store.freeze_to_run(run_id)
    frozen = json.loads(
        (Path(task.run_folder(run_id)) / 'evidence_index.json').read_text(encoding='utf-8')
    )
    FinancialNormalizer(card['time_window']).normalize_to_run(
        frozen['items'], task.run_folder(run_id),
    )

    with pytest.raises(ValueError, match='无效证据引用'):
        assemble_report(task.task_id, {
            'title': '门禁', 'summary': '',
            'sections': [{'title': '摘要', 'content': '不存在的事实[E999]'}],
        }, run_id=run_id)

    with pytest.raises(ValueError, match='证据相关性校验'):
        assemble_report(task.task_id, {
            'title': '门禁', 'summary': '',
            'sections': [{
                'title': '摘要',
                'content': '该公司发生重大安全事故[E1]',
            }],
        }, run_id=run_id)

    mixed_chart = '''```chart
{"type":"bar","title":"错期混比","x":["甲","乙"],"series":[{"name":"营业总收入","data":[100,100]}],"source_refs":["E1","E2"],"comparison_basis":{"period":"2025-06-30","period_type":"H1","unit":"亿元","currency":"CNY","cumulative":"cumulative","consolidation_scope":"consolidated"}}
```'''
    with pytest.raises(ValueError, match='同口径校验'):
        assemble_report(task.task_id, {
            'title': '门禁', 'summary': '',
            'sections': [{'title': '关键指标对照', 'content': mixed_chart}],
        }, run_id=run_id)


def test_report_gate_rejects_fabricated_financial_table_values(
    isolated_debate_runtime: Path,
):
    task = ResearchTask(task_id='task_financial_prose_gate', requirement='财务正文门禁')
    card = {
        'deliverable': 'compare',
        'analysis_mode': 'direct',
        'symbols': [{
            'code': '300750', 'name': '甲',
        }, {
            'code': '002594', 'name': '乙',
        }],
        'time_window': {'start': '2025-01-01', 'end': '2025-12-31'},
        'info_types': ['financial_report'],
    }
    task.set_task_card(card)
    run_id = task.create_run(card)
    first = _card('甲 H1', '300750', financial=True)
    second = _card('乙 H1', '002594', financial=True)
    second['structured']['TOTAL_OPERATE_INCOME_yi'] = '90.00'
    evidence_path = Path(task.run_folder(run_id)) / 'evidence' / 'financial.jsonl'
    evidence_path.write_text(
        ''.join(json.dumps(item, ensure_ascii=False) + '\n' for item in (first, second)),
        encoding='utf-8',
    )
    store = EvidenceStore(task.task_id, run_id=run_id, allow_staging=True)
    store.freeze_to_run(run_id)
    frozen = json.loads(
        (Path(task.run_folder(run_id)) / 'evidence_index.json').read_text(encoding='utf-8')
    )
    FinancialNormalizer(card['time_window']).normalize_to_run(
        frozen['items'], task.run_folder(run_id),
    )

    fabricated = (
        '| 主体 | 营业总收入（亿元） |\n'
        '| --- | --- |\n'
        '| 300750 | 999 |\n'
        '| 002594 | 888 |\n'
        '数值对比[E1][E2]'
    )
    with pytest.raises(ValueError, match='财务数值 999'):
        assemble_report(task.task_id, {
            'title': '门禁', 'summary': '',
            'sections': [{
                'title': '财务对比', 'content': fabricated,
                'audited_debate': True,
            }],
        }, run_id=run_id)

    supported = fabricated.replace('999', '100').replace('888', '90')
    report = assemble_report(task.task_id, {
        'title': '门禁', 'summary': '',
        'sections': [{
            'title': '财务对比', 'content': supported,
            'audited_debate': True,
        }],
    }, run_id=run_id)
    assert '100' in report['markdown'] and '90' in report['markdown']
