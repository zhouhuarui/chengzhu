"""分析管线的全离线端到端验收。

测试数据、SQLite、图谱和报告均写入 pytest 的临时目录，不读取或删除
``backend/uploads`` 中的真实任务数据。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.config import Config
from app.models.research_task import ResearchTask, ResearchTaskStatus
from app.models.task_card import TaskCard
from app.services import analyst as analyst_module
from app.services.analyst import Analyst
from app.services.evidence_store import EvidenceStore
from app.services.pipeline import run_analysis_pipeline
from app.services.collect_orchestrator import _collect_uploaded_files
from app.services.report_assembler import load_report
from app.tools.schema import EvidenceCard
from app.utils import db as dbmod
from app.utils import graph_client as graphmod
from app.utils.file_parser import FileParser
from app.utils.run_limits import RunDeadlineExceeded
from app.utils.llm_client import LLMResult


@pytest.fixture
def isolated_runtime(tmp_path, monkeypatch):
    """把分析管线依赖的所有持久化与外部能力隔离到临时目录。"""
    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None

    monkeypatch.setattr(Config, 'UPLOAD_FOLDER', str(tmp_path))
    monkeypatch.setattr(Config, 'DB_PATH', str(tmp_path / 'chengzhu.db'))
    monkeypatch.setattr(Config, 'LLM_API_KEY', None)
    monkeypatch.setattr(graphmod.GraphClient, '_try_init_neo4j', lambda self: None)
    graphmod._instances.clear()
    dbmod.init_db()

    yield tmp_path

    conn = getattr(dbmod._local, 'conn', None)
    if conn:
        conn.close()
        dbmod._local.conn = None
    graphmod._instances.clear()


def _financial_card(
    symbol: str,
    company: str,
    period: str,
    revenue_yi: float,
    profit_yi: float,
) -> EvidenceCard:
    fingerprint = f'{symbol}-{period}-{revenue_yi}-{profit_yi}'
    return EvidenceCard(
        source_type='financial_report',
        title=f'{company} {period} 年度财务数据',
        url=None,
        publish_time=f'{period}-12-31T18:00:00+08:00',
        source_name='Datayes',
        symbol=symbol,
        excerpt=f'{company}披露营业收入 {revenue_yi} 亿元、归母净利润 {profit_yi} 亿元。',
        structured={
            'REPORT_DATE': f'{period}-12-31',
            'TOTAL_OPERATE_INCOME_yi': revenue_yi,
            'PARENT_NETPROFIT_yi': profit_yi,
        },
        provenance={
            'provider': 'datayes_warehouse',
            'api': 'getFdmtIS',
            'record_key': f'getFdmtIS:{symbol}:{period}:A:1:{fingerprint}',
            'business_key': f'getFdmtIS:{symbol}:{period}:A:1',
            'as_of': f'{period}-12-31',
            'update_time': f'{period}-12-31T18:00:00+08:00',
            'warehouse_watermark': '2026-01-02T00:00:00+08:00',
            'row_fingerprint': fingerprint,
            'upstream_source': '上市公司定期报告',
            'license_scope': 'private_derived_only',
        },
        reliability=5,
        fetch_tool='fetch_financial_statements',
    )


def _announcement_card(symbol: str, company: str) -> EvidenceCard:
    return EvidenceCard(
        source_type='announcement',
        title=f'{company}年度报告公告',
        url=f'https://example.test/announcements/{symbol}',
        publish_time='2026-01-02T09:00:00+08:00',
        source_name='合成交易所披露',
        symbol=symbol,
        excerpt=f'{company}发布年度报告，具体财务口径以公告正文为准。',
        structured={'event_type': 'annual_report', 'synthetic': True},
        reliability=5,
        fetch_tool='fetch_announcements',
    )


def _industry_card(symbol: str, company: str) -> EvidenceCard:
    return EvidenceCard(
        source_type='industry_data',
        title=f'{company}所属行业数据',
        url=None,
        publish_time='2026-01-02T15:00:00+08:00',
        source_name='合成行业数据',
        symbol=symbol,
        excerpt=f'{company}所属行业为食品饮料，统计日期为 2026-01-02。',
        structured={'industry': '食品饮料', 'trade_date': '2026-01-02', 'synthetic': True},
        reliability=4,
        fetch_tool='fetch_industry_data',
    )


def _write_task(root: Path, deliverable: str) -> tuple[str, list[EvidenceCard]]:
    task_id = f'task_offline_{deliverable}'
    if deliverable == 'summary':
        symbols = [{'code': '600519', 'name': '贵州茅台'}]
        cards = [
            _financial_card('600519', '贵州茅台', '2024', 1709.0, 862.0),
            _financial_card('600519', '贵州茅台', '2025', 1819.0, 900.0),
            _announcement_card('600519', '贵州茅台'),
            _industry_card('600519', '贵州茅台'),
        ]
    else:
        symbols = [
            {'code': '600519', 'name': '贵州茅台'},
            {'code': '000858', 'name': '五粮液'},
        ]
        cards = [
            _financial_card('600519', '贵州茅台', '2025', 1819.0, 900.0),
            _financial_card('000858', '五粮液', '2025', 920.0, 340.0),
            _announcement_card('600519', '贵州茅台'),
            _announcement_card('000858', '五粮液'),
            _industry_card('600519', '贵州茅台'),
            _industry_card('000858', '五粮液'),
        ]

    task = ResearchTask(task_id=task_id, requirement=f'离线{deliverable}验收')
    task.set_task_card({
        'deliverable': deliverable,
        'symbols': symbols,
        'time_window': {'start': '2024-01-01', 'end': '2026-01-02'},
        'info_types': ['announcement', 'financial_report', 'industry_data'],
        'focus_points': ['财务表现', '公开披露'],
        'compare_dimensions': ['营业收入', '归母净利润'] if deliverable == 'compare' else [],
    })

    evidence_path = root / 'tasks' / task_id / 'evidence' / 'synthetic.jsonl'
    evidence_path.parent.mkdir(parents=True, exist_ok=True)
    evidence_path.write_text(
        ''.join(json.dumps(card.to_dict(), ensure_ascii=False) + '\n' for card in cards),
        encoding='utf-8',
    )
    return task_id, cards


def test_evidence_store_search_uses_only_synthetic_cards(isolated_runtime):
    task_id, cards = _write_task(isolated_runtime, 'summary')
    store = EvidenceStore(task_id)

    assert store.statistics()['total_cards'] == len(cards)
    assert store.statistics()['symbols'] == ['600519']
    hits = store.search('财务', limit=5)
    assert hits
    assert all(card.symbol == '600519' for card in hits)


def test_uploaded_parser_cannot_publish_after_absolute_deadline(
    isolated_runtime,
    monkeypatch,
):
    task = ResearchTask(task_id='task_slow_upload', requirement='慢文件超时')
    card = {
        'deliverable': 'summary',
        'symbols': [{'code': '600519', 'name': '贵州茅台'}],
        'time_window': {'start': '2025-01-01', 'end': '2026-01-01'},
        'info_types': ['announcement'],
    }
    task.set_task_card(card)
    run_id = task.create_run(card)
    upload = Path(task.run_folder(run_id)) / 'files' / 'slow.txt'
    upload.write_text('仅用于超时测试', encoding='utf-8')

    def slow_extract(_path):
        time.sleep(0.25)
        return '返回已超时'

    monkeypatch.setattr(FileParser, 'extract_text', staticmethod(slow_extract))

    class NoopLogger:
        def log(self, *_args, **_kwargs):
            return None

    started = time.monotonic()
    with pytest.raises(RunDeadlineExceeded):
        _collect_uploaded_files(
            task,
            run_id,
            card,
            NoopLogger(),
            # The parser keeps only the post-read publication reserve.  It
            # receives about 80ms and its late result must be discarded.
            deadline_epoch=time.time() + 3.08,
        )
    assert time.monotonic() - started < 0.2
    assert not (Path(task.run_folder(run_id)) / 'evidence' / 'uploaded.jsonl').exists()


def test_uploaded_image_is_not_silently_filtered_without_vision_key(
    isolated_runtime,
    monkeypatch,
):
    task = ResearchTask(task_id='task_uploaded_image', requirement='图片证据')
    card = {
        'deliverable': 'summary',
        'symbols': [{'code': '600519', 'name': '贵州茅台'}],
        'time_window': {'start': '2025-01-01', 'end': '2026-01-01'},
        'info_types': ['announcement'],
    }
    task.set_task_card(card)
    run_id = task.create_run(card)
    image_path = Path(task.run_folder(run_id)) / 'files' / 'chart.png'
    Image.new('RGB', (120, 80), color='white').save(image_path, format='PNG')
    monkeypatch.setattr(Config, 'VISION_LLM_API_KEY', None)

    class NoopLogger:
        def log(self, *_args, **_kwargs):
            return None

    result = _collect_uploaded_files(task, run_id, card, NoopLogger())

    assert result['cards'] == 1
    evidence_path = Path(task.run_folder(run_id)) / 'evidence' / 'uploaded.jsonl'
    card_payload = json.loads(evidence_path.read_text(encoding='utf-8').strip())
    assert card_payload['title'] == 'chart.png'
    assert card_payload['structured']['visual_status'] == 'not_configured'
    assert card_payload['structured']['visual_parse_incomplete'] is True
    assert '视觉证据未完整解析' in card_payload['excerpt']


def test_direct_analyst_uses_deepseek_text_only_after_grounding(
    isolated_runtime,
    monkeypatch,
):
    task_id, cards = _write_task(isolated_runtime, 'summary')
    task = ResearchTask.load(task_id)
    analyst = Analyst(task_id, TaskCard.from_dict(task.task_card or {}))
    exact_line = f'- {cards[0].excerpt}[E1]'

    class ScriptedTextLLM:
        provider = 'deepseek'
        model = 'deepseek-v4-flash'

        def __init__(self):
            self.responses = [
                '<tool_call>{"name":"evidence_search","parameters":{"query":"财务"}}</tool_call>',
                '<tool_call>{"name":"evidence_search","parameters":{"query":"公告"}}</tool_call>',
                f'Final Answer:\n{exact_line}',
            ]

        def chat_result(self, _messages, **_kwargs):
            content = self.responses.pop(0)
            return LLMResult(
                content=content,
                provider=self.provider,
                model=self.model,
                finish_reason='stop',
            )

    analyst._llm = ScriptedTextLLM()
    monkeypatch.setattr(
        analyst_module,
        'call_analyze_tool',
        lambda *_args, **_kwargs: SimpleNamespace(ok=True, data={'items': []}, error=None),
    )
    monkeypatch.setattr(
        'app.utils.llm_audit.record_llm_result',
        lambda *_args, **_kwargs: 1,
    )

    result = analyst.write_section_llm(
        {'title': '信息要点', 'goal': '整理可核验事实'},
        '测试报告',
        '',
    )

    assert result == exact_line
    assert analyst._llm_writing_used is True


def test_debate_expression_agent_can_reorder_but_not_rewrite_claims(
    isolated_runtime,
    monkeypatch,
):
    task_id, _cards = _write_task(isolated_runtime, 'summary')
    task = ResearchTask.load(task_id)
    analyst = Analyst(task_id, TaskCard.from_dict(task.task_card or {}))
    sections = [
        {'title': '共识事实', 'goal': '裁决表达', 'content': '- 事实 A[E1]'},
        {'title': '主要反证', 'goal': '裁决表达', 'content': '- 反证 B[E2]'},
    ]

    class OrderingLLM:
        provider = 'deepseek'
        model = 'deepseek-v4-flash'

        def chat_json_result(self, _messages, **_kwargs):
            return LLMResult(
                content='{"section_order":["主要反证","共识事实"]}',
                provider=self.provider,
                model=self.model,
                finish_reason='stop',
                parsed_json={'section_order': ['主要反证', '共识事实']},
            )

    analyst._llm = OrderingLLM()
    monkeypatch.setattr(
        'app.utils.llm_audit.record_llm_result',
        lambda *_args, **_kwargs: 1,
    )

    title, summary, ordered = analyst._express_debate_with_llm(
        '标题', '摘要', sections,
    )

    assert (title, summary) == ('标题', '摘要')
    assert [item['title'] for item in ordered] == ['主要反证', '共识事实']
    assert ordered[0]['content'] == '- 反证 B[E2]'
    assert analyst._llm_expression_used is True


@pytest.mark.parametrize(
    ('deliverable', 'expected_title', 'expected_sections'),
    [
        (
            'summary',
            '贵州茅台信息整理摘要',
            {'信息要点', '财务表现', '市场与行业背景'},
        ),
        (
            'compare',
            '贵州茅台、五粮液对比分析',
            {'对比范围与口径', '关键指标对照', '差异事实归纳'},
        ),
    ],
    ids=['summary', 'compare'],
)
def test_offline_analysis_pipeline_end_to_end(
    isolated_runtime,
    deliverable,
    expected_title,
    expected_sections,
):
    """合成 EvidenceCard 贯穿 ingest → analyst → reviewer → report。"""
    task_id, cards = _write_task(isolated_runtime, deliverable)
    stale = ResearchTask.load(task_id)
    stale.error = 'previous-run-error-must-be-cleared'
    stale.save()

    completed = run_analysis_pipeline(task_id)
    report = load_report(task_id)

    assert completed.status is ResearchTaskStatus.COMPLETED
    assert completed.error is None
    assert ResearchTask.load(task_id).error is None
    assert completed.progress == 100
    assert completed.progress_detail['ingest']['ingested'] == len(cards)
    assert completed.progress_detail['ingest']['errors'] == []
    assert report is not None
    assert report['title'] == expected_title
    assert report['mode'] == 'heuristic'
    assert report['statistics']['total_cards'] == len(cards)
    assert expected_sections.issubset({section['title'] for section in report['sections']})
    assert {'信息来源清单', '数据完整性说明', '风险与关注点'}.issubset(
        {section['title'] for section in report['sections']}
    )
    assert report['cited_ids']
    assert all(1 <= evidence_id <= len(cards) for evidence_id in report['cited_ids'])
    assert '[E' in report['markdown']
    assert '结构化溯源' in report['markdown']
    assert '不构成任何投资建议' in report['markdown']

    task_folder = isolated_runtime / 'tasks' / task_id
    for name in (
        'graph.json',
        'outline.json',
        'draft_report.json',
        'reviewed_report.json',
        'report.json',
        'report.md',
        'full_report.md',
    ):
        assert (task_folder / name).is_file(), f'缺少管线产物: {name}'

    graph = json.loads((task_folder / 'graph.json').read_text(encoding='utf-8'))
    assert graph['statistics']['episodes'] == len(cards)
    assert graph['statistics']['backend'] == 'local'
    assert graph['edges']

    task_run = dbmod.get_task_run(task_id)
    assert task_run is not None
    assert task_run['status'] == 'completed'
