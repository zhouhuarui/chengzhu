"""简化仿真运行器（无 camel-oasis 时的可演示实现）。"""

from __future__ import annotations

import json
import os
import random
import time
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...config import Config
from ...constants import SCENARIO_BANNER
from ...models.research_task import ResearchTask
from ...utils import db as dbutil
from ...utils.graph_client import get_graph_client, scenario_group_id
from ...utils.task_run_lock import task_run_lock
from .personas import instantiate_profiles
from .scenario_agent import design_scenario


def _scenario_dir(scenario_id: str) -> str:
    path = os.path.join(Config.UPLOAD_FOLDER, 'scenarios', scenario_id)
    os.makedirs(path, exist_ok=True)
    return path


def create_scenario(
    task_id: str,
    hypothesis: str,
    from_evidence_id: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    with task_run_lock(task_id):
        if not ResearchTask.load(task_id):
            raise ValueError('任务不存在')
        return _create_scenario_locked(
            task_id,
            hypothesis,
            from_evidence_id,
            run_id,
        )


def _create_scenario_locked(
    task_id: str,
    hypothesis: str,
    from_evidence_id: Optional[int],
    run_id: Optional[str],
) -> Dict[str, Any]:
    scenario_id = f'scen_{uuid.uuid4().hex[:10]}'
    config = design_scenario(
        task_id,
        hypothesis,
        from_evidence_id,
        run_id=run_id,
    )
    resolved_run_id = str(config['run_id'])
    folder = _scenario_dir(scenario_id)
    meta = {
        'scenario_id': scenario_id,
        'task_id': task_id,
        'run_id': resolved_run_id,
        'status': 'awaiting_confirm',
        'config': config,
        'progress': 0,
        'created_at': datetime.now().isoformat(timespec='seconds'),
    }
    with open(os.path.join(folder, 'scenario.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    dbutil.insert_scenario_run(scenario_id, task_id, config, 'awaiting_confirm')
    return meta


def load_scenario(scenario_id: str) -> Optional[Dict[str, Any]]:
    # A read must never recreate a directory after task deletion.
    path = os.path.join(
        Config.UPLOAD_FOLDER, 'scenarios', str(scenario_id), 'scenario.json',
    )
    if not os.path.isfile(path):
        return None
    with open(path, 'r', encoding='utf-8') as f:
        return json.load(f)


def save_scenario(meta: Dict[str, Any]) -> None:
    sid = meta['scenario_id']
    with open(os.path.join(_scenario_dir(sid), 'scenario.json'), 'w', encoding='utf-8') as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def _simulate_branch(
    scenario_id: str,
    branch: str,
    hypothesis: str,
    profiles: List[Dict],
    events: List[Dict],
    max_rounds: int,
    log_path: str,
) -> List[Dict]:
    actions = []
    random.seed(hash(f'{scenario_id}:{branch}') & 0xFFFFFFFF)
    with open(log_path, 'a', encoding='utf-8') as f:
        for rnd in range(1, max_rounds + 1):
            # 注入事件
            for ev in events:
                if int(ev.get('round', 0)) == rnd:
                    poster = next((p for p in profiles if p['role'] in (
                        '公司IR' if ev.get('poster_role') == 'company_ir' else
                        '财经媒体' if ev.get('poster_role') == 'financial_media' else p['role']
                    )), profiles[0])
                    act = {
                        'round': rnd,
                        'branch': branch,
                        'agent_id': poster['agent_id'],
                        'role': poster['role'],
                        'type': 'inject',
                        'content': ev.get('content'),
                        'ts': datetime.now().isoformat(timespec='seconds'),
                    }
                    actions.append(act)
                    f.write(json.dumps(act, ensure_ascii=False) + '\n')

            # 角色反应
            sample = random.sample(profiles, k=min(8, len(profiles)))
            for p in sample:
                tone = '观望'
                if branch == 'bearish':
                    tone = random.choice(['谨慎', '偏空解读', '关注风险', '观望'])
                else:
                    tone = random.choice(['中性', '关注基本面', '等待更多信息', '观望'])
                act = {
                    'round': rnd,
                    'branch': branch,
                    'agent_id': p['agent_id'],
                    'role': p['role'],
                    'type': 'post',
                    'content': f'【模拟情景中】{p["role"]}表示{tone}：围绕「{hypothesis[:40]}」的公开讨论仍在发酵。',
                    'ts': datetime.now().isoformat(timespec='seconds'),
                }
                actions.append(act)
                f.write(json.dumps(act, ensure_ascii=False) + '\n')
            time.sleep(0.05)
    return actions


def run_scenario(scenario_id: str, config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Run under the task deletion barrier for the entire write lifetime."""

    meta = load_scenario(scenario_id)
    if not meta:
        raise ValueError('scenario not found')
    task_id = str(meta.get('task_id') or '')
    if not task_id:
        raise ValueError('scenario task is missing')
    with task_run_lock(task_id):
        # DELETE may have won before this worker acquired the barrier.  Reload
        # both owners and fail without calling ``_scenario_dir`` in that case.
        if not ResearchTask.load(task_id):
            raise ValueError('scenario task was deleted')
        current = load_scenario(scenario_id)
        if not current:
            raise ValueError('scenario was deleted')
        return _run_scenario_locked(current, config)


def _run_scenario_locked(
    meta: Dict[str, Any],
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    scenario_id = str(meta['scenario_id'])
    if config:
        meta['config'] = config
    cfg = meta['config']
    folder = _scenario_dir(scenario_id)
    meta['status'] = 'running'
    meta['progress'] = 5
    save_scenario(meta)
    dbutil.update_scenario_run(scenario_id, status='running')

    # 角色
    from ...services.evidence_store import EvidenceStore
    run_id = str(meta.get('run_id') or cfg.get('run_id') or '')
    if not run_id:
        raise ValueError('scenario source run is missing')
    store = EvidenceStore(meta['task_id'], run_id=run_id)
    if not store.is_frozen or not store.cards:
        raise ValueError('scenario source run is not a frozen non-empty snapshot')
    from ...services.report_assembler import load_report
    if not load_report(meta['task_id'], run_id=run_id):
        raise ValueError('scenario source run is no longer published')
    # The start endpoint may accept user-edited scenario parameters, but it
    # must never be able to retarget the immutable source selected at create
    # time or smuggle evidence display IDs from another run into the report.
    valid_refs = {store.display_id(card) for card in store.cards}
    requested_refs = cfg.get('baseline_facts') or []
    if not isinstance(requested_refs, list):
        requested_refs = []
    cfg['baseline_facts'] = [
        str(ref) for ref in requested_refs if str(ref) in valid_refs
    ] or [store.display_id(card) for card in store.cards[:5]]
    cfg['task_id'] = meta['task_id']
    cfg['run_id'] = run_id
    meta['run_id'] = run_id
    meta['config'] = cfg
    save_scenario(meta)
    seed = [c.title for c in store.cards[:10]]
    profiles = instantiate_profiles(int(cfg.get('agent_scale') or 30), seed)
    with open(os.path.join(folder, 'profiles.json'), 'w', encoding='utf-8') as f:
        json.dump(profiles, f, ensure_ascii=False, indent=2)

    actions_path = os.path.join(folder, 'actions.jsonl')
    if os.path.isfile(actions_path):
        os.remove(actions_path)

    max_rounds = int(cfg.get('max_rounds') or 10)
    events = cfg.get('injected_events') or []

    # 悲观情景
    meta['progress'] = 20
    meta['message'] = '推演悲观情景'
    save_scenario(meta)
    bear = _simulate_branch(
        scenario_id, 'bearish', cfg.get('hypothesis', ''),
        profiles, events, max_rounds, actions_path,
    )

    # 基准情景
    meta['progress'] = 55
    meta['message'] = '推演基准情景'
    save_scenario(meta)
    counter = (cfg.get('counter_scenario') or {})
    base_h = counter.get('hypothesis') if counter.get('enabled', True) else '基准情景'
    base = _simulate_branch(
        scenario_id, 'baseline', base_h or '符合预期',
        profiles, [
            {**events[0], 'content': f'【假设·基准】{base_h}'} if events else
            {'round': 1, 'content': f'【假设·基准】{base_h}', 'poster_role': 'company_ir'}
        ],
        max_rounds, actions_path,
    )

    # 写入推演图谱
    g = get_graph_client(scenario_group_id(scenario_id))
    for a in (bear + base)[:40]:
        g.add_episode(
            body=f"[S][{a['branch']}][R{a['round']}] {a['role']}: {a['content']}",
            meta={'branch': a['branch'], 'round': a['round']},
        )

    meta['progress'] = 80
    meta['message'] = '生成推演报告'
    save_scenario(meta)
    report = _build_report(meta, profiles, bear, base)
    with open(os.path.join(folder, 'report.json'), 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    with open(os.path.join(folder, 'report.md'), 'w', encoding='utf-8') as f:
        f.write(report['markdown'])

    meta['status'] = 'completed'
    meta['progress'] = 100
    meta['message'] = '推演完成'
    meta['report_title'] = report.get('title')
    save_scenario(meta)
    dbutil.update_scenario_run(
        scenario_id, status='completed', finished_at=dbutil.now_iso(), cost=0,
    )
    return meta


def _build_report(meta: Dict, profiles: List[Dict], bear: List[Dict], base: List[Dict]) -> Dict[str, Any]:
    cfg = meta['config']
    title = f"情景推演观察报告 · {cfg.get('scenario_title', '')}"
    sections = [
        {
            'title': '执行摘要（情景设定重述）',
            'content': (
                f'在模拟沙盘中设定假设：「{cfg.get("hypothesis")}」。'
                f'基线事实角标：{", ".join(cfg.get("baseline_facts") or [])}。'
                f'同时运行基准情景：「{(cfg.get("counter_scenario") or {}).get("hypothesis")}」。'
                f'规模 {len(profiles)} 角色 × {cfg.get("max_rounds")} 轮。'
            ),
        },
        {
            'title': '舆论演化时间线',
            'content': _timeline_md(bear, '悲观情景') + '\n\n' + _timeline_md(base, '基准情景'),
        },
        {
            'title': '分歧图谱',
            'content': (
                '在模拟情景中，悲观分支更多出现「谨慎/偏空解读」表述；'
                '基准分支以「中性/等待更多信息」为主。上述分布仅反映沙盘内角色发言，不代表真实市场。'
            ),
        },
        {
            'title': '关键传导链',
            'content': (
                '在模拟情景中，信息大致沿「公司IR披露 → 卖方分析师解读 → 财经媒体放大 → 散户跟风讨论」扩散。'
                '该路径为沙盘规则设定，不等于真实传播。'
            ),
        },
        {
            'title': '双情景对比',
            'content': (
                f'悲观情景假设：{cfg.get("hypothesis")}\n\n'
                f'基准情景假设：{(cfg.get("counter_scenario") or {}).get("hypothesis")}\n\n'
                '在模拟沙盘中，两情景差异主要体现在情绪措辞强度，而非可验证的基本面事实。'
            ),
        },
        {
            'title': '情景观察与局限性',
            'content': (
                f'{SCENARIO_BANNER}\n\n'
                f'- Agent 规模：{len(profiles)}；轮数：{cfg.get("max_rounds")}。\n'
                '- 本推演为规则+模板驱动的简化舆论沙盘，存在模型偏差与情绪传播简化假设。\n'
                '- 禁止将模拟结果外推为现实预测或投资建议。'
            ),
        },
    ]
    md = [f'# {title}', '', f'> {SCENARIO_BANNER}', '']
    for s in sections:
        md += [f'## {s["title"]}', '', s['content'], '']
    return {
        'title': title,
        'banner': SCENARIO_BANNER,
        'sections': sections,
        'markdown': '\n'.join(md),
        'baseline_facts': cfg.get('baseline_facts'),
    }


def _timeline_md(actions: List[Dict], label: str) -> str:
    lines = [f'### {label}（模拟）']
    by_round: Dict[int, List] = {}
    for a in actions:
        by_round.setdefault(a['round'], []).append(a)
    for rnd in sorted(by_round):
        lines.append(f'- 第 {rnd} 轮：' + '；'.join(
            f'{x["role"]}「{x["content"][:40]}…」' for x in by_round[rnd][:3]
        ))
    return '\n'.join(lines)


def interview_agents(scenario_id: str, topic: str, max_agents: int = 3) -> List[Dict[str, Any]]:
    meta = load_scenario(scenario_id)
    if not meta:
        return []
    path = os.path.join(
        Config.UPLOAD_FOLDER, 'scenarios', str(scenario_id), 'profiles.json',
    )
    profiles = []
    if os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            profiles = json.load(f)
    picks = profiles[:max_agents]
    out = []
    for p in picks:
        out.append({
            'agent_id': p['agent_id'],
            'role': p['role'],
            'answer': (
                f'【模拟情景中】作为{p["role"]}，针对「{topic}」，'
                f'我仅基于沙盘内已发生的讨论作答：需要继续观察假设事件的后续披露，'
                f'不将其外推为真实市场结论。'
            ),
        })
    return out
