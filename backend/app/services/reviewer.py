"""审校 Agent：规则引擎 + 可选 LLM 复核。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional, Set

from ..config import Config
from ..models.research_task import task_artifact_folder
from .agent_logger import AgentLogger
from .compliance_checker import (
    check_chart_blocks,
    check_citations,
    check_compliance,
    strip_advice_phrases,
)
from .evidence_store import EvidenceStore

CITATION_RE = re.compile(r'\[E(\d+)\]')
CITATION_OPTIONAL_TITLES = {
    '证据不足', '后续公开事项', '撤回观点', '辩论运行说明',
    '风险与关注点', '数据完整性说明',
}


class Reviewer:
    def __init__(
        self,
        task_id: str,
        logger: Optional[AgentLogger] = None,
        run_id: Optional[str] = None,
        allow_llm: bool = True,
        deadline_epoch: Optional[float] = None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.allow_llm = allow_llm
        self.deadline_epoch = deadline_epoch
        self.store = EvidenceStore(task_id, run_id=run_id)
        self.logger = logger or AgentLogger(task_id)
        self._llm = None
        self._llm_unavailable = False
        self.artifact_folder = task_artifact_folder(task_id, run_id)
        self.review_path = os.path.join(self.artifact_folder, 'review_log.jsonl')

    def _get_llm(self):
        if (
            self._llm is None
            and not self._llm_unavailable
            and self.allow_llm
            and Config.TEXT_LLM_API_KEY
        ):
            from ..utils.llm_client import LLMClient
            read_timeout = min(Config.LLM_READ_TIMEOUT_SECONDS, 25.0)
            if self.deadline_epoch is not None:
                from ..utils.run_limits import bounded_timeout
                read_timeout = bounded_timeout(
                    self.deadline_epoch,
                    read_timeout,
                    reserve_seconds=10,
                    stage='reviewer_client',
                )
            try:
                self._llm = LLMClient(
                    api_key=Config.TEXT_LLM_API_KEY,
                    base_url=Config.TEXT_LLM_BASE_URL,
                    model=Config.TEXT_LLM_REASONING_MODEL,
                    provider=Config.TEXT_LLM_PROVIDER,
                    connect_timeout=min(Config.LLM_CONNECT_TIMEOUT_SECONDS, 5),
                    read_timeout=read_timeout,
                    deadline_epoch=self.deadline_epoch,
                    deadline_reserve_seconds=10,
                    budget_run_id=self.run_id or self.task_id,
                )
            except Exception as error:
                from ..utils.llm_audit import record_llm_client_error, safe_error_summary
                self._llm_unavailable = True
                record_llm_client_error(
                    self.run_id or self.task_id,
                    'reviewer',
                    self._llm,
                    error,
                )
                self.logger.log(
                    'reviewer',
                    'llm_client_fallback',
                    {'error': safe_error_summary(error)},
                )
        return self._llm

    def _append_log(self, record: Dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(self.review_path), exist_ok=True)
        with open(self.review_path, 'a', encoding='utf-8') as f:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')

    def _evidence_index_for_text(self, text: str) -> Dict[str, Any]:
        ids = [int(x) for x in CITATION_RE.findall(text or '')]
        index = {}
        for eid in ids:
            card = self.store.get(eid)
            if card:
                index[f'E{eid}'] = {
                    'title': card.title,
                    'excerpt': card.excerpt,
                    'source_type': card.source_type,
                    'publish_time': card.publish_time,
                }
        return index

    def review_section(
        self,
        section: Dict[str, Any],
        *,
        use_llm: bool = True,
        append_log: bool = True,
    ) -> Dict[str, Any]:
        title = section.get('title', '')
        text = section.get('content', '') or ''
        valid_ids: Set[int] = set(self.store._by_id.keys())

        evidence_blobs = {
            eid: f'{c.title}\n{c.excerpt}\n{json.dumps(c.structured, ensure_ascii=False)}'
            for eid, c in self.store._by_id.items()
        }
        issues = (
            check_compliance(text)
            + check_citations(
                text,
                valid_ids,
                require_any=title not in CITATION_OPTIONAL_TITLES,
            )
            + check_chart_blocks(text, evidence_blobs)
        )
        verdict = 'revise' if issues else 'pass'
        revised = text

        llm = self._get_llm() if use_llm else None
        if llm and (issues or len(text) > 100):
            try:
                evidence_index = self._evidence_index_for_text(text)
                prompt = (
                    '你是投研报告的审校与合规官。逐句核对章节草稿与证据，输出 JSON：'
                    '{"verdict":"pass|revise","issues":[{"quote","type","detail","suggestion"}],'
                    '"revised_text":"verdict=revise 时给出全文改写稿"}。'
                    '宁可错杀。禁止投资建议与走势预测。\n'
                    f'章节：{title}\n草稿：\n{text}\n证据索引：\n{json.dumps(evidence_index, ensure_ascii=False)}'
                )
                llm_result = llm.chat_json_result(
                    [{'role': 'user', 'content': prompt}],
                    temperature=0.1,
                    max_tokens=4096,
                    max_attempts=2,
                    thinking=False,
                )
                from ..utils.llm_audit import record_llm_result
                record_llm_result(self.run_id or self.task_id, 'reviewer', llm_result)
                result = llm_result.parsed_json or {}
                if isinstance(result, dict):
                    llm_issues = result.get('issues') or []
                    issues = issues + llm_issues
                    if result.get('verdict') == 'revise' and result.get('revised_text'):
                        revised = result['revised_text']
                        verdict = 'revise'
                    elif not issues:
                        verdict = 'pass'
            except Exception as e:
                from ..utils.llm_audit import record_llm_client_error, safe_error_summary
                record_llm_client_error(
                    self.run_id or self.task_id,
                    'reviewer',
                    llm,
                    e,
                )
                self.logger.log(
                    'reviewer',
                    'llm_fallback',
                    {'error': safe_error_summary(e), 'title': title},
                )

        if (
            text.strip()
            and title not in CITATION_OPTIONAL_TITLES
            and not CITATION_RE.search(revised)
        ):
            issues.append({
                'type': 'evidence_gap',
                'detail': '章节没有可核验的证据引用',
                'suggestion': '补充匹配证据后再形成事实结论',
            })
            verdict = 'revise'

        if verdict == 'revise':
            # 规则兜底：至少去掉黑名单
            if check_compliance(revised):
                revised = strip_advice_phrases(revised)
            # 无引用时只能披露证据不足，禁止自动附加无关“高可信来源”。
            if title not in CITATION_OPTIONAL_TITLES and not CITATION_RE.search(revised):
                note = '当前冻结证据中未找到足以支持本章事实结论的匹配证据。'
                if note not in revised:
                    revised = revised.rstrip() + f'\n\n{note}'

        record = {
            'section': title,
            'verdict': verdict,
            'issues': issues,
            'final_len': len(revised),
        }
        if append_log:
            self._append_log(record)
        self.logger.log('reviewer', 'section_reviewed', {'title': title, 'verdict': verdict, 'issues': len(issues)})
        output = {
            'title': title,
            'goal': section.get('goal'),
            'content': revised,
            'verdict': verdict,
            'issues': issues,
        }
        for marker in ('deterministic_financial', 'audited_debate', 'system'):
            if section.get(marker):
                output[marker] = True
        return output

    def _batch_llm_review(self, sections: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        llm = self._get_llm()
        if not llm or not sections:
            return sections
        combined = '\n'.join(section.get('content') or '' for section in sections)
        evidence_index = self._evidence_index_for_text(combined)
        prompt = (
            '你是投研报告审校与合规官。一次批量复核所有章节，只依据给定证据。'
            '禁止投资建议、趋势预测和无证据事实；不得增加引用。输出 JSON，示例：'
            '{"sections":[{"index":0,"verdict":"pass|revise","issues":'
            '[{"type":"citation","detail":"..."}],"revised_text":"完整章节"}]}。'
            'index 必须对应输入；无需改写时 revised_text 为空。\n'
            f'章节：{json.dumps(sections, ensure_ascii=False)[:18000]}\n'
            f'证据索引：{json.dumps(evidence_index, ensure_ascii=False)[:10000]}'
        )
        try:
            llm_result = llm.chat_json_result(
                [{'role': 'user', 'content': prompt}],
                temperature=0.1,
                max_tokens=4096,
                max_attempts=2,
                thinking=False,
            )
            from ..utils.llm_audit import record_llm_result
            record_llm_result(self.run_id or self.task_id, 'reviewer', llm_result)
            payload = llm_result.parsed_json or {}
            proposals = payload.get('sections') or []
            if not isinstance(proposals, list):
                raise ValueError('Reviewer 批量输出缺少 sections')
            by_index = {
                int(item.get('index')): item
                for item in proposals
                if isinstance(item, dict) and str(item.get('index', '')).isdigit()
            }
            output: List[Dict[str, Any]] = []
            valid_ids: Set[int] = set(self.store._by_id.keys())
            evidence_blobs = {
                eid: f'{card.title}\n{card.excerpt}\n{json.dumps(card.structured, ensure_ascii=False)}'
                for eid, card in self.store._by_id.items()
            }
            for index, section in enumerate(sections):
                current = dict(section)
                proposal = by_index.get(index) or {}
                issues = list(current.get('issues') or []) + list(proposal.get('issues') or [])
                revised = current.get('content') or ''
                if proposal.get('verdict') == 'revise' and proposal.get('revised_text'):
                    revised = str(proposal['revised_text'])
                deterministic = (
                    check_compliance(revised)
                    + check_citations(
                        revised,
                        valid_ids,
                        require_any=current.get('title') not in CITATION_OPTIONAL_TITLES,
                    )
                    + check_chart_blocks(revised, evidence_blobs)
                )
                if (
                    revised.strip()
                    and current.get('title') not in CITATION_OPTIONAL_TITLES
                    and not CITATION_RE.search(revised)
                ):
                    deterministic.append({
                        'type': 'evidence_gap',
                        'detail': '章节没有可核验的证据引用',
                        'suggestion': '补充匹配证据后再形成事实结论',
                    })
                    revised = revised.rstrip() + '\n\n当前冻结证据中未找到足以支持本章事实结论的匹配证据。'
                issues.extend(deterministic)
                if check_compliance(revised):
                    revised = strip_advice_phrases(revised)
                current['content'] = revised
                current['issues'] = issues
                current['verdict'] = 'revise' if issues or proposal.get('verdict') == 'revise' else 'pass'
                output.append(current)
            return output
        except Exception as error:
            from ..utils.llm_audit import record_llm_client_error, safe_error_summary
            record_llm_client_error(
                self.run_id or self.task_id,
                'reviewer',
                llm,
                error,
            )
            self.logger.log(
                'reviewer',
                'batch_llm_fallback',
                {'error': safe_error_summary(error)},
            )
            return sections

    def run(self, draft: Dict[str, Any]) -> Dict[str, Any]:
        raw_sections = list(draft.get('sections') or [])
        immutable_debate = (
            draft.get('analysis_mode') == 'evidence_debate'
            and draft.get('debate_status') == 'completed'
        )
        if self._get_llm() and len(raw_sections) > 1:
            reviewed_sections = [
                self.review_section(section, use_llm=False, append_log=False)
                for section in raw_sections
            ]
            if immutable_debate:
                immutable_contents = [section.get('content') or '' for section in reviewed_sections]
                advisory = self._batch_llm_review(reviewed_sections)
                for index, section in enumerate(advisory):
                    # Reviewer may classify issues, but cannot introduce a
                    # sentence that was not rendered from an accepted Claim.
                    section['content'] = immutable_contents[index]
                reviewed_sections = advisory
            else:
                immutable_indices = {
                    index: section.get('content') or ''
                    for index, section in enumerate(reviewed_sections)
                }
                reviewed_sections = self._batch_llm_review(reviewed_sections)
                for index, content in immutable_indices.items():
                    reviewed_sections[index]['content'] = content
            for section in reviewed_sections:
                self._append_log({
                    'section': section.get('title'),
                    'verdict': section.get('verdict'),
                    'issues': section.get('issues') or [],
                    'final_len': len(section.get('content') or ''),
                })
        else:
            reviewed_sections = [
                self.review_section(section, use_llm=False)
                for section in raw_sections
            ]
        out = {
            'title': draft.get('title'),
            'summary': draft.get('summary'),
            'sections': reviewed_sections,
            'mode': draft.get('mode'),
            'analysis_mode': draft.get('analysis_mode', 'direct'),
            'debate_status': draft.get('debate_status'),
            'debate_fallback_reason': draft.get('debate_fallback_reason'),
            'debate_verdict': draft.get('debate_verdict'),
        }
        path = os.path.join(self.artifact_folder, 'reviewed_report.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(out, f, ensure_ascii=False, indent=2)
        return out
