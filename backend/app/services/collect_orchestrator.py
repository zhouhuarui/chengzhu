"""并行采集编排器（02§3.2 + 03§A2 简化版：默认计划，无 LLM 计划也可跑通）。"""

from __future__ import annotations

import json
import hashlib
import os
import traceback
import uuid
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeout,
    as_completed,
)
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models.research_task import ResearchTask, ResearchTaskStatus, task_card_for_run
from ..tools.registry import call_tool
from ..tools.schema import EvidenceCard
from ..utils import db as dbutil
from ..utils.file_parser import FileParser
from .agent_logger import AgentLogger


COLLECTOR_PLAN = {
    'announcement': [
        ('fetch_announcements', lambda card, sym: {
            'symbol': sym,
            'start_date': card['time_window']['start'],
            'end_date': card['time_window']['end'],
            'max_count': 20,
        }),
    ],
    'financial': [
        ('fetch_financial_statements', lambda card, sym: {
            'symbol': sym, 'statement': 'income', 'period_count': 6,
        }),
        ('fetch_financial_indicators', lambda card, sym: {'symbol': sym}),
    ],
    'news': [
        ('fetch_stock_news', lambda card, sym: {'symbol': sym, 'max_count': 15}),
    ],
    'research': [
        ('fetch_research_reports', lambda card, sym: {'symbol': sym, 'max_count': 10}),
    ],
    'industry': [
        ('fetch_stock_quote', lambda card, sym: {'symbol': sym, 'days': 90}),
        ('fetch_industry_data', lambda card, sym: {'symbol': sym, 'macro_indicators': ['cpi', 'pmi']}),
    ],
}

INFO_TYPE_TO_COLLECTOR = {
    'announcement': 'announcement',
    'financial_report': 'financial',
    'news': 'news',
    'research_report': 'research',
    'industry_data': 'industry',
}


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _collect_uploaded_files(
    task: ResearchTask,
    run_id: Optional[str],
    card: Dict[str, Any],
    logger: AgentLogger,
    deadline_epoch: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Parse the run-owned upload snapshot into frozen evidence candidates."""

    folder = (
        os.path.join(task.run_folder(run_id), 'files')
        if run_id else os.path.join(task.folder, 'files')
    )
    if not os.path.isdir(folder):
        return None
    paths = [
        os.path.join(folder, name)
        for name in sorted(os.listdir(folder))
        if os.path.isfile(os.path.join(folder, name))
        and not os.path.islink(os.path.join(folder, name))
        and FileParser.is_supported(os.path.join(folder, name))
    ]
    if not paths:
        return None

    logger.log('collector_start', 'collecting', {'agent': 'uploaded'}, agent='collector_uploaded')
    cards: List[EvidenceCard] = []
    errors: List[str] = []
    symbols = [str(item.get('code')) for item in card.get('symbols') or [] if item.get('code')]
    symbol = symbols[0] if len(symbols) == 1 else None
    for path in paths:
        try:
            if deadline_epoch is not None:
                from ..utils.run_limits import call_with_deadline, ensure_time_remaining
                ensure_time_remaining(deadline_epoch, stage='uploaded_file_parser')
                file_hash, text = call_with_deadline(
                    lambda: (_sha256(path), FileParser.extract_text(path)),
                    deadline_epoch,
                    reserve_seconds=3,
                    stage='uploaded_file_parser',
                )
            else:
                file_hash = _sha256(path)
                text = FileParser.extract_text(path)
            structured: Dict[str, Any] = {
                'file_sha256': file_hash,
                'file_name': Path(path).name,
                'traditional_text': text,
                'visual_parse_incomplete': False,
            }
            suffix = Path(path).suffix.lower()
            visual: Optional[Dict[str, Any]] = None
            if suffix == '.pdf':
                from .pdf_visuals import parse_pdf_visuals

                if deadline_epoch is not None:
                    visual = call_with_deadline(
                        lambda: parse_pdf_visuals(
                            path,
                            max_pages=Config.VISION_MAX_PAGES,
                            run_id=run_id or task.task_id,
                            deadline_epoch=deadline_epoch,
                        ),
                        deadline_epoch,
                        reserve_seconds=2,
                        stage='pdf_visual_parser',
                    )
                else:
                    visual = parse_pdf_visuals(
                        path,
                        max_pages=Config.VISION_MAX_PAGES,
                        run_id=run_id or task.task_id,
                    )
            elif suffix in FileParser.IMAGE_EXTENSIONS:
                from .pdf_visuals import parse_image_visual

                if deadline_epoch is not None:
                    visual = call_with_deadline(
                        lambda: parse_image_visual(
                            path,
                            run_id=run_id or task.task_id,
                            deadline_epoch=deadline_epoch,
                        ),
                        deadline_epoch,
                        reserve_seconds=2,
                        stage='image_visual_parser',
                    )
                else:
                    visual = parse_image_visual(
                        path,
                        run_id=run_id or task.task_id,
                    )

            if visual is not None:
                structured.update({
                    'page_count': visual.get('page_count'),
                    'analyzed_page_limit': visual.get('analyzed_page_limit'),
                    'pages': visual.get('pages') or [],
                    'candidate_pages': visual.get('candidate_pages') or [],
                    'visual_status': visual.get('visual_status'),
                    'visual_parse_incomplete': bool(visual.get('visual_incomplete')),
                    'visual_pages': visual.get('visual_pages') or [],
                    'visual_notes': visual.get('vl_notes') or [],
                    'structured_markdown': visual.get('markdown') or '',
                })
                visual_markdown = str(visual.get('markdown') or '').strip()
                if visual_markdown and visual_markdown != '（未提取到表格或视觉内容）':
                    text = f'{text}\n\n{visual_markdown}'.strip()
                if visual.get('visual_incomplete'):
                    fallback_note = (
                        '视觉证据未完整解析；当前仅保留传统文本/表格解析结果。'
                        if suffix == '.pdf'
                        else '视觉证据未完整解析；图片已保留为待补解析证据。'
                    )
                    text = f'{text}\n\n{fallback_note}'.strip()

            if deadline_epoch is not None:
                ensure_time_remaining(deadline_epoch, stage='uploaded_evidence_publish')
            cards.append(EvidenceCard(
                source_type='uploaded_document',
                title=Path(path).name,
                url=None,
                publish_time=datetime.fromtimestamp(os.path.getmtime(path)).astimezone().isoformat(timespec='seconds'),
                source_name='用户上传文件',
                symbol=symbol,
                excerpt=text[:12000],
                structured=structured,
                reliability=5,
                fetch_tool='uploaded_file_parser',
                evidence_uid=f'ev_file_{file_hash[:24]}',
                provenance={
                    'provider': 'user_upload',
                    'api': 'local_file_parser',
                    'record_key': file_hash,
                    'as_of': datetime.now().astimezone().date().isoformat(),
                    'upstream_source': Path(path).name,
                    'license_scope': 'user_provided',
                },
            ))
        except Exception as error:
            if error.__class__.__name__ == 'RunDeadlineExceeded':
                raise
            errors.append(f'{Path(path).name}: {error}')
            logger.log(
                'error', 'collecting',
                {'file': Path(path).name, 'error': str(error)[:500]},
                agent='collector_uploaded',
            )

    if deadline_epoch is not None:
        from ..utils.run_limits import ensure_time_remaining
        ensure_time_remaining(deadline_epoch, stage='uploaded_evidence_publish')
    _write_evidence(task, 'uploaded', cards, run_id=run_id)
    logger.log(
        'collector_complete', 'collecting',
        {'agent': 'uploaded', 'cards': len(cards), 'errors': errors},
        agent='collector_uploaded',
    )
    return {
        'agent': 'uploaded',
        'ok': bool(cards),
        'cards': len(cards),
        'error': '; '.join(errors) if errors else None,
    }


def _write_evidence(
    task: ResearchTask,
    agent: str,
    cards: List[EvidenceCard],
    run_id: Optional[str] = None,
) -> str:
    evidence_folder = (
        os.path.join(task.run_folder(run_id), 'evidence')
        if run_id else os.path.join(task.folder, 'evidence')
    )
    os.makedirs(evidence_folder, exist_ok=True)
    path = os.path.join(evidence_folder, f'{agent}.jsonl')
    temp_path = os.path.join(evidence_folder, f'.{agent}-{uuid.uuid4().hex}.tmp')
    try:
        with open(temp_path, 'x', encoding='utf-8') as f:
            for i, c in enumerate(cards, 1):
                c.card_id = i
                f.write(json.dumps(c.to_dict(), ensure_ascii=False) + '\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_path, path)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)
    return path


def _run_one_collector(
    task: ResearchTask,
    agent: str,
    card: Dict[str, Any],
    logger: AgentLogger,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    from ..tools._helpers import set_tool_deadline
    from ..utils.run_limits import ensure_time_remaining

    set_tool_deadline(deadline_epoch)
    logger.log('collector_start', 'collecting', {'agent': agent}, agent=f'collector_{agent}')
    all_cards: List[EvidenceCard] = []
    errors: List[str] = []
    symbols = [s.get('code') for s in card.get('symbols') or [] if s.get('code')]
    if not symbols:
        errors.append('no symbols')
        return {'agent': agent, 'ok': False, 'cards': 0, 'error': ';'.join(errors)}

    for tool_name, param_fn in COLLECTOR_PLAN.get(agent, []):
        for sym in symbols:
            try:
                if deadline_epoch is not None:
                    ensure_time_remaining(deadline_epoch, stage=f'collector:{agent}')
                params = param_fn(card, sym)
                logger.log(
                    'tool_call', 'collecting',
                    {'tool_name': tool_name, 'parameters': params},
                    agent=f'collector_{agent}',
                )
                result = call_tool(
                    tool_name,
                    run_id=run_id or task.task_id,
                    agent=f'collector_{agent}',
                    **params,
                )
                if deadline_epoch is not None:
                    ensure_time_remaining(
                        deadline_epoch,
                        stage=f'collector:{agent}:tool_result',
                    )
                if isinstance(result, list):
                    all_cards.extend(result)
                    logger.log(
                        'tool_result', 'collecting',
                        {'tool_name': tool_name, 'cards': len(result)},
                        agent=f'collector_{agent}',
                    )
            except Exception as e:
                errors.append(f'{tool_name}/{sym}: {e}')
                logger.log(
                    'error', 'collecting',
                    {'tool_name': tool_name, 'error': str(e), 'trace': traceback.format_exc()[-500:]},
                    agent=f'collector_{agent}',
                )

    if deadline_epoch is not None:
        ensure_time_remaining(
            deadline_epoch,
            stage=f'collector:{agent}:evidence_publish',
        )
    _write_evidence(task, agent, all_cards, run_id=run_id)
    logger.log(
        'collector_complete', 'collecting',
        {'agent': agent, 'cards': len(all_cards), 'errors': errors},
        agent=f'collector_{agent}',
    )
    return {
        'agent': agent,
        'ok': len(all_cards) > 0,
        'cards': len(all_cards),
        'error': '; '.join(errors) if errors else None,
    }


def collect_uploaded_files(
    task: ResearchTask,
    run_id: Optional[str],
    card: Dict[str, Any],
    logger: AgentLogger,
    deadline_epoch: Optional[float] = None,
) -> Optional[Dict[str, Any]]:
    """Public deterministic upload-ingestion port for either runtime."""

    return _collect_uploaded_files(
        task,
        run_id,
        card,
        logger,
        deadline_epoch=deadline_epoch,
    )


def run_collector(
    task: ResearchTask,
    agent: str,
    card: Dict[str, Any],
    logger: AgentLogger,
    run_id: Optional[str] = None,
    deadline_epoch: Optional[float] = None,
) -> Dict[str, Any]:
    """Public one-collector domain port; it performs no role orchestration."""

    return _run_one_collector(
        task,
        agent,
        card,
        logger,
        run_id,
        deadline_epoch,
    )


def run_collection(
    task_id: str,
    run_id: Optional[str] = None,
    *,
    deadline_epoch: Optional[float] = None,
) -> ResearchTask:
    task = ResearchTask.load(task_id)
    if not task or not task.task_card:
        raise ValueError('task or task_card missing')

    if run_id and not task.has_run(run_id):
        raise ValueError('run 不存在或不属于该任务')
    from ..utils.run_limits import (
        deadline_epoch_for_run,
        ensure_time_remaining,
        remaining_seconds,
    )
    deadline_epoch = deadline_epoch or deadline_epoch_for_run(run_id or task_id)
    ensure_time_remaining(deadline_epoch, stage='collection_start')
    run_card = task_card_for_run(task, run_id)
    logger = AgentLogger(task_id, agent='orchestrator', run_id=run_id)
    task.set_status(ResearchTaskStatus.COLLECTING, '并行采集中', progress=5)
    if run_id and dbutil.get_task_run(run_id):
        dbutil.update_task_run(run_id, status=ResearchTaskStatus.COLLECTING.value)
    logger.log('collect_start', 'collecting', {'task_card': run_card})

    info_types = run_card.get('info_types') or list(INFO_TYPE_TO_COLLECTOR)
    agents = sorted({INFO_TYPE_TO_COLLECTOR[t] for t in info_types if t in INFO_TYPE_TO_COLLECTOR})

    collectors_state: Dict[str, Any] = {
        a: {'state': 'running', 'cards': 0} for a in agents
    }
    task.progress_detail = {
        'stage': 'collecting',
        'analysis_mode': run_card.get('analysis_mode', 'direct'),
        'run_id': run_id,
        'collectors': collectors_state,
    }
    task.save()

    results = []
    uploaded = _collect_uploaded_files(
        task, run_id, run_card, logger, deadline_epoch=deadline_epoch
    )
    if uploaded is not None:
        results.append(uploaded)
        collectors_state['uploaded'] = {
            'state': 'done' if uploaded['ok'] else 'failed',
            'cards': uploaded['cards'],
            'error': uploaded.get('error'),
        }
        task.progress_detail = {
            **(task.progress_detail or {}),
            'collectors': collectors_state,
            'message': 'uploaded 完成',
        }
        task.save()
    pool = ThreadPoolExecutor(max_workers=Config.COLLECTOR_MAX_PARALLEL)
    futs = {
        pool.submit(
            _run_one_collector,
            task,
            agent,
            run_card,
            logger,
            run_id,
            deadline_epoch,
        ): agent
        for agent in agents
    }
    try:
        done = 0
        for fut in as_completed(futs, timeout=max(0.01, remaining_seconds(deadline_epoch))):
            agent = futs[fut]
            try:
                r = fut.result()
            except Exception as e:
                r = {'agent': agent, 'ok': False, 'cards': 0, 'error': str(e)}
            results.append(r)
            done += 1
            collectors_state[agent] = {
                'state': 'done' if r['ok'] else 'failed',
                'cards': r['cards'],
                'error': r.get('error'),
            }
            task.progress = 5 + int(40 * done / max(len(agents), 1))
            task.progress_detail = {
                'stage': 'collecting',
                'analysis_mode': run_card.get('analysis_mode', 'direct'),
                'run_id': run_id,
                'collectors': collectors_state,
                'message': f'{agent} 完成',
            }
            task.save()
    except FuturesTimeout as error:
        for future in futs:
            future.cancel()
        pool.shutdown(wait=False, cancel_futures=True)
        raise TimeoutError('collection: run_deadline_exceeded') from error
    else:
        pool.shutdown(wait=True)

    task.collect_failures = [
        {'agent': r['agent'], 'error': r['error']} for r in results if not r['ok']
    ]
    ok_n = sum(1 for r in results if r['ok'])
    if ok_n == 0:
        task.error = '全部采集 Agent 失败'
        task.set_status(ResearchTaskStatus.FAILED, '采集失败', progress=100)
        logger.log('task_failed', 'failed', {'failures': task.collect_failures})
        if run_id and dbutil.get_task_run(run_id):
            dbutil.finish_task_run(run_id, ResearchTaskStatus.FAILED.value)
        if run_id and dbutil.get_debate_run(run_id):
            dbutil.finish_debate_run(run_id, 'failed', error='collection_failed')
        return task

    # 图谱摄入（Phase 2）未就绪：跳过 INGESTING，直接进入分析管线
    msg = '采集完成' if not task.collect_failures else '采集部分完成'
    task.progress = 60
    task.set_status(ResearchTaskStatus.INGESTING, msg + '（本地证据库就绪，跳过图谱）', progress=60)
    if run_id and dbutil.get_task_run(run_id):
        dbutil.update_task_run(run_id, status=ResearchTaskStatus.INGESTING.value)
    logger.log('collect_done', 'ingesting', {
        'results': results,
        'note': 'local evidence ready; graph ingest deferred',
    })
    return task
