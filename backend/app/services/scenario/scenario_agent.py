"""ScenarioAgent：情景设计。"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from ...config import Config
from ...models.research_task import ResearchTask, resolve_task_run_id
from ...services.evidence_store import EvidenceStore
from ...services.report_assembler import load_report


def resolve_scenario_source(
    task_id: str,
    requested_run_id: Optional[str] = None,
) -> tuple[str, EvidenceStore]:
    """Resolve one immutable, report-published run for scenario generation."""

    task = ResearchTask.load(task_id)
    if not task:
        raise ValueError('任务不存在')
    run_id = resolve_task_run_id(task_id, requested_run_id)
    # A legacy task-root report has no immutable run evidence identity and is
    # therefore unsafe for a run-bound scenario.
    if not run_id or run_id == task_id:
        raise ValueError('当前没有可用于情景推演的已发布 run')
    store = EvidenceStore(task_id, run_id=run_id)
    if not store.is_frozen or not store.cards:
        raise ValueError('情景推演要求已冻结且非空的 run 证据')
    if not load_report(task_id, run_id=run_id):
        raise ValueError('情景推演要求已发布报告的 run')
    return run_id, store


def design_scenario(
    task_id: str,
    hypothesis: str,
    from_evidence_id: Optional[int] = None,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_run_id, store = resolve_scenario_source(task_id, run_id)
    # Task-level graph groups can contain episodes from several runs. Keep the
    # scenario source strictly within the selected frozen evidence snapshot.
    matched_cards = store.search(hypothesis, limit=10)
    facts = [
        {
            'display_id': store.display_id(card),
            'evidence_uid': card.evidence_uid,
            'title': card.title,
            'excerpt': card.excerpt,
            'publish_time': card.publish_time,
            'source_type': card.source_type,
        }
        for card in matched_cards
    ]
    baseline = []
    selected = store.get(from_evidence_id) if from_evidence_id else None
    if selected:
        baseline.append(store.display_id(selected))
    baseline.extend(item['display_id'] for item in facts)
    baseline = list(dict.fromkeys(baseline))[:8] or [
        store.display_id(card) for card in store.cards[:5]
    ]

    config = {
        'scenario_title': '业绩/事件情景推演',
        'hypothesis': hypothesis,
        'baseline_facts': baseline,
        'injected_events': [
            {
                'round': 1,
                'type': 'official_disclosure',
                'content': f'【假设】{hypothesis}',
                'poster_role': 'company_ir',
            },
            {
                'round': 3,
                'type': 'media_report',
                'content': f'【假设】财经媒体解读上述情景：{hypothesis[:80]}',
                'poster_role': 'financial_media',
            },
        ],
        'agent_scale': Config.SCENARIO_AGENT_SCALE,
        'max_rounds': Config.SCENARIO_MAX_ROUNDS,
        'counter_scenario': {
            'enabled': True,
            'hypothesis': '上述事件未发生或影响符合市场一致预期',
        },
        'task_id': task_id,
        'run_id': resolved_run_id,
    }

    if Config.TEXT_LLM_API_KEY:
        llm = None
        try:
            from ...utils.llm_client import LLMClient
            llm = LLMClient(
                api_key=Config.TEXT_LLM_API_KEY,
                base_url=Config.TEXT_LLM_BASE_URL,
                model=Config.TEXT_LLM_FAST_MODEL,
                provider=Config.TEXT_LLM_PROVIDER,
                budget_run_id=resolved_run_id,
            )
            prompt = (
                '你是情景设计 Agent。基于用户假设与基线事实，输出 ScenarioConfig JSON，'
                '字段含 scenario_title, hypothesis, baseline_facts, injected_events, '
                'agent_scale, max_rounds, counter_scenario。'
                '注入事件须标注假设；必须开启 counter_scenario。\n'
                f'假设：{hypothesis}\n基线：{baseline}\n事实：{json.dumps(facts[:5], ensure_ascii=False)}'
            )
            llm_result = llm.chat_json_result(
                [{'role': 'user', 'content': prompt}],
                temperature=0.3,
                max_tokens=2048,
                max_attempts=2,
                thinking=False,
            )
            from ...utils.llm_audit import record_llm_result
            record_llm_result(resolved_run_id, 'scenario', llm_result)
            out = llm_result.parsed_json or {}
            if isinstance(out, dict) and out.get('hypothesis'):
                out.setdefault('counter_scenario', config['counter_scenario'])
                valid_refs = {store.display_id(card) for card in store.cards}
                requested_baseline = out.get('baseline_facts') or []
                if not isinstance(requested_baseline, list):
                    requested_baseline = []
                out['baseline_facts'] = [
                    str(ref) for ref in requested_baseline if str(ref) in valid_refs
                ] or baseline
                out['task_id'] = task_id
                out['run_id'] = resolved_run_id
                return out
        except Exception as error:
            # JSON repair attempts and transport failures may already have
            # consumed billable tokens. Keep that metadata on the same run as
            # the frozen scenario source while preserving the deterministic
            # fallback below.
            if llm is not None:
                from ...utils.llm_audit import record_llm_client_error
                record_llm_client_error(
                    resolved_run_id,
                    'scenario',
                    llm,
                    error,
                )
    return config
