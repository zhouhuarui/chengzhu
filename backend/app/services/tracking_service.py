"""追踪订阅与简报。"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Config
from ..models.research_task import ResearchTask
from ..tools.registry import call_tool
from ..tools.schema import EvidenceCard
from ..utils import db as dbutil
from ..utils.graph_client import get_graph_client, project_group_id
from ..utils.task_run_lock import task_run_lock
from .graph_ingest import _dedup_key, ingest_task_evidence


TRACKING_INFO_TYPES = {
    'announcement', 'financial_report', 'news', 'research_report', 'industry_data',
}


def _safe_error(exc: Exception) -> str:
    text = str(exc)
    for secret in (
        Config.DATAYES_TOKEN, Config.LLM_API_KEY, Config.BOCHA_API_KEY,
    ):
        if secret:
            text = text.replace(str(secret), '[REDACTED]')
    text = re.sub(
        r'(?i)\bauthorization\s*[:=]\s*[^\r\n,;]+',
        'Authorization: [REDACTED]',
        text,
    )
    text = re.sub(r'(?i)\bbearer\s+[^\s,;]+', 'Bearer [REDACTED]', text)
    text = re.sub(
        r'(?i)\b(token|api[_-]?key)\s*[:=]\s*[^\s,;]+',
        r'\1=[REDACTED]',
        text,
    )
    return text[:300]


def _tracking_tool_calls(task_card: Dict[str, Any], watermark: Optional[str]) -> List[Tuple[str, Dict[str, Any]]]:
    """把原任务范围映射为受控工具调用，不开放任意 tool/params。"""
    symbols = [
        str(symbol.get('code'))
        for symbol in (task_card.get('symbols') or [])
        if symbol.get('code')
    ]
    info_types = set(task_card.get('info_types') or TRACKING_INFO_TYPES)
    start = (watermark or (task_card.get('time_window') or {}).get('start') or '')[:10]
    end = date.today().isoformat()
    calls: List[Tuple[str, Dict[str, Any]]] = []
    for symbol in symbols:
        if 'announcement' in info_types:
            calls.append(('fetch_announcements', {
                'symbol': symbol, 'start_date': start, 'end_date': end, 'max_count': 30,
            }))
        if 'financial_report' in info_types:
            for statement in ('income', 'balance', 'cashflow'):
                calls.append(('fetch_financial_statements', {
                    'symbol': symbol, 'statement': statement, 'period_count': 6,
                }))
            calls.append(('fetch_financial_indicators', {'symbol': symbol}))
        if 'news' in info_types:
            calls.append(('fetch_stock_news', {'symbol': symbol, 'max_count': 20}))
        if 'research_report' in info_types:
            calls.append(('fetch_research_reports', {'symbol': symbol, 'max_count': 15}))
        if 'industry_data' in info_types:
            calls.append(('fetch_stock_quote', {'symbol': symbol, 'days': 90}))
            calls.append(('fetch_industry_data', {
                'symbol': symbol, 'macro_indicators': ['cpi', 'pmi'],
            }))
    return calls


def refresh_tracking_evidence(
    task: ResearchTask,
    sub: Dict[str, Any],
    *,
    tool_caller: Optional[Callable[..., Any]] = None,
) -> Dict[str, Any]:
    """增量刷新并写独立证据批次；永不覆盖原报告的 collector JSONL。"""
    caller = tool_caller or call_tool
    calls = _tracking_tool_calls(task.task_card or {}, sub.get('watermark'))
    run_id = f"tracking_{sub['sub_id']}_{uuid.uuid4().hex[:10]}"
    cards: List[EvidenceCard] = []
    failures: List[Dict[str, Any]] = []
    degraded_cards = 0
    seen = set()

    if not calls:
        failures.append({'tool': None, 'error': '原任务没有可刷新的证券代码或信息类型'})

    for tool_name, params in calls:
        try:
            result = caller(
                tool_name,
                run_id=run_id,
                agent='tracking_collector',
                **params,
            )
            if not isinstance(result, list):
                continue
            for card in result:
                if not isinstance(card, EvidenceCard):
                    continue
                key = _dedup_key(card, task.task_id)
                if key in seen:
                    continue
                seen.add(key)
                cards.append(card)
                if bool((card.structured or {}).get('degraded')):
                    degraded_cards += 1
        except Exception as exc:
            failures.append({'tool': tool_name, 'error': _safe_error(exc)})

    evidence_dir = os.path.join(task.folder, 'evidence')
    os.makedirs(evidence_dir, exist_ok=True)
    stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%f')
    path = os.path.join(evidence_dir, f'zz_tracking_{stamp}_{run_id[-6:]}.jsonl')
    with open(path, 'x', encoding='utf-8') as f:
        for card in cards:
            # card_id 由 EvidenceStore 按所有文件的稳定排序重新分配。
            card.card_id = None
            f.write(json.dumps(card.to_dict(), ensure_ascii=False) + '\n')

    return {
        'run_id': run_id,
        'evidence_file': os.path.basename(path),
        'tool_calls': len(calls),
        'cards': len(cards),
        'degraded_cards': degraded_cards,
        'license_scopes': sorted({
            str((card.provenance or {}).get('license_scope'))
            for card in cards
            if (card.provenance or {}).get('license_scope')
        }),
        'failures': failures,
    }


def graph_changes_since(client, watermark: Optional[str], limit: int = 30) -> Dict[str, Any]:
    """仅返回 watermark 后观察到的当前事实和版本失效。"""
    panorama = client.panorama(limit=limit, since=watermark or None)
    return {
        'new_facts': panorama.get('new_facts') or panorama.get('current') or [],
        'changed_facts': panorama.get('invalidated_facts') or panorama.get('historical') or [],
    }


def subscribe(task_id: str, cron: str = 'weekly', hour: int = 8) -> Dict[str, Any]:
    with task_run_lock(task_id):
        task = ResearchTask.load(task_id)
        if not task:
            raise ValueError('任务不存在')
        sub_id = f'sub_{uuid.uuid4().hex[:10]}'
        watermark = dbutil.now_iso()
        dbutil.insert_tracking_sub(sub_id, task_id, cron, hour, watermark=watermark)
        return dbutil.get_tracking_sub(sub_id) or {'sub_id': sub_id}


def run_subscription_now(sub_id: str) -> Dict[str, Any]:
    sub = dbutil.get_tracking_sub(sub_id)
    if not sub:
        raise ValueError('订阅不存在')
    task_id = str(sub['task_id'])
    with task_run_lock(task_id):
        # Recheck both owners after acquiring the deletion barrier.  If DELETE
        # won the race, do not recreate evidence/brief directories.
        current_sub = dbutil.get_tracking_sub(sub_id)
        task = ResearchTask.load(task_id)
        if not current_sub or not task:
            raise ValueError('订阅或原任务已删除')
        return _run_subscription_now_locked(current_sub, task)


def _run_subscription_now_locked(
    sub: Dict[str, Any],
    task: ResearchTask,
) -> Dict[str, Any]:
    sub_id = str(sub['sub_id'])
    task_id = str(sub['task_id'])

    # 基于原任务范围主动刷新，写入独立 tracking evidence 后再做版本比较。
    refresh = refresh_tracking_evidence(task, sub)
    ingest_stats = ingest_task_evidence(task_id)
    client = get_graph_client(project_group_id(task_id))
    changes = graph_changes_since(client, sub.get('watermark'), limit=30)
    new_facts = changes['new_facts']
    changed = changes['changed_facts']

    # 生成简报 markdown（启发式）
    title = f"追踪简报 · {(task.task_card or {}).get('symbols', [{}])[0].get('name', task_id)}"
    lines = [
        f'# {title}',
        f'*生成时间：{datetime.now().strftime("%Y-%m-%d %H:%M")}*',
        '',
        '## 本期新增信息',
    ]
    if new_facts:
        for f in new_facts[:15]:
            fact = f.get('fact') if isinstance(f, dict) else str(f)
            cid = f.get('card_id') if isinstance(f, dict) else None
            lines.append(f'- {fact}' + (f'[E{cid}]' if cid else ''))
    else:
        lines.append('- 本期没有观察到新增事实。')
    lines += ['', '## 变化与更正']
    if changed:
        for f in changed[:10]:
            fact = f.get('fact') if isinstance(f, dict) else str(f)
            lines.append(f'- {fact}（已失效/更新）')
    else:
        lines.append('- 本期未见明确失效事实（本地图谱模式）。')
    lines += ['', '## 数据完整性']
    if refresh['failures']:
        lines.append(f"- {len(refresh['failures'])} 个刷新调用失败；水位保持不变，下次将重试。")
        for failure in refresh['failures'][:5]:
            lines.append(f"- {failure.get('tool') or 'tracking'}：{failure.get('error')}")
    elif refresh['degraded_cards']:
        lines.append(
            f"- {refresh['degraded_cards']} 条证据来自降级数据源；水位保持不变，"
            '下次将重试，请结合溯源信息复核。'
        )
    else:
        lines.append('- 本期刷新调用完成，未记录数据源失败。')
    if 'private_derived_only' in refresh['license_scopes']:
        lines += [
            '',
            '> 本简报基于已授权数据与公开信息；Datayes 派生事实仅限私有使用，'
            '不得进入公开 Demo 或批量导出。本简报不构成投资建议。',
        ]
    else:
        lines += ['', '> 本简报仅整理公开信息，不构成投资建议。']
    md = '\n'.join(lines)

    brief_id = f'brief_{uuid.uuid4().hex[:10]}'
    folder = os.path.join(Config.UPLOAD_FOLDER, 'briefs', sub_id)
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f'{brief_id}.md')
    with open(path, 'w', encoding='utf-8') as f:
        f.write(md)

    dbutil.insert_brief(
        brief_id, sub_id,
        run_id=refresh['run_id'],
        date=datetime.now().strftime('%Y-%m-%d'),
        markdown_path=path,
        new_facts=len(new_facts),
        changed_facts=len(changed),
    )
    completed_at = dbutil.now_iso()
    update_fields = {'last_run_at': completed_at}
    # 任一工具失败时保留旧水位，成功来源产生的重复卡会由指纹去重；这样
    # 下次仍可补齐失败来源的时间窗口。
    if not refresh['failures'] and not refresh['degraded_cards']:
        update_fields['watermark'] = completed_at
    dbutil.update_tracking_sub(
        sub_id,
        **update_fields,
    )
    return {
        'brief_id': brief_id,
        'markdown': md,
        'new_facts': len(new_facts),
        'changed_facts': len(changed),
        'refresh': refresh,
        'ingest': ingest_stats,
    }


def start_scheduler(app=None):
    """APScheduler：每日检查订阅。"""
    try:
        from apscheduler.schedulers.background import BackgroundScheduler
    except Exception:
        return None
    if not Config.TRACKING_CRON_ENABLED:
        return None
    sched = BackgroundScheduler()

    def _tick():
        hour = datetime.now().hour
        for sub in dbutil.list_tracking_subs(status='active'):
            if int(sub.get('hour') or 8) != hour:
                continue
            # 简易：每天该小时跑 daily；weekly 仅周一
            cron = sub.get('cron')
            if cron == 'weekly' and datetime.now().weekday() != 0:
                continue
            try:
                run_subscription_now(sub['sub_id'])
            except Exception:
                pass

    sched.add_job(_tick, 'interval', minutes=60, id='tracking_tick')
    sched.start()
    return sched
