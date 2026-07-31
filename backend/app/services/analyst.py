"""分析 Agent：大纲规划 + 章节 ReAct；无 LLM Key 时走启发式装配。"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models.research_task import task_artifact_folder
from ..models.task_card import TaskCard
from ..tools.graph_local import call_analyze_tool, tools_description
from .agent_logger import AgentLogger
from .evidence_store import EvidenceStore

SKELETONS = {
    'summary': [
        {'title': '信息要点', 'goal': '梳理近期可核验证据中的关键事实与变化', 'required': True},
        {'title': '财务表现', 'goal': '汇总可得财务与经营指标，注明报告期，必要时输出 chart 数据块', 'required': True},
        {'title': '市场与行业背景', 'goal': '归纳相关新闻与行业动态（不作投资建议）', 'required': False},
    ],
    'compare': [
        {'title': '对比范围与口径', 'goal': '说明对比主体、报告期与数据口径', 'required': True},
        {'title': '关键指标对照', 'goal': '用表格对照同口径指标，表下注明报告期', 'required': True},
        {'title': '差异事实归纳', 'goal': '归纳可核实的差异事实，禁止估值与买卖建议', 'required': True},
    ],
    'tracking': [
        {'title': '本期新增信息', 'goal': '列出水位线以来新出现的事实', 'required': True},
        {'title': '变化与更正', 'goal': '列出口径变化、更正或失效信息（若有）', 'required': True},
        {'title': '持续关注点', 'goal': '基于证据列出可跟踪事项，不作建议', 'required': False},
    ],
}

TOOL_CALL_RE = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.DOTALL)
FINAL_RE = re.compile(r'Final Answer:\s*(.*)', re.DOTALL | re.IGNORECASE)


class Analyst:
    def __init__(
        self,
        task_id: str,
        task_card: TaskCard,
        logger: Optional[AgentLogger] = None,
        run_id: Optional[str] = None,
        allow_llm: bool = True,
        deadline_epoch: Optional[float] = None,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.allow_llm = allow_llm
        self.deadline_epoch = deadline_epoch
        self.task_card = task_card
        self.store = EvidenceStore(task_id, run_id=run_id)
        self.artifact_folder = task_artifact_folder(task_id, run_id)
        self.has_private_evidence = any(
            (card.provenance or {}).get('license_scope') == 'private_derived_only'
            for card in self.store.cards
        )
        self.logger = logger or AgentLogger(task_id)
        self._llm = None
        self._llm_unavailable = False
        self._llm_outline_used = False
        self._llm_writing_used = False
        self._llm_expression_used = False

    def _get_llm(self):
        if (
            self._llm is None
            and not self._llm_unavailable
            and self.allow_llm
            and Config.TEXT_LLM_API_KEY
        ):
            from ..utils.llm_client import LLMClient
            read_timeout = min(Config.LLM_READ_TIMEOUT_SECONDS, 30.0)
            if self.deadline_epoch is not None:
                from ..utils.run_limits import bounded_timeout
                read_timeout = bounded_timeout(
                    self.deadline_epoch,
                    read_timeout,
                    reserve_seconds=30,
                    stage='analyst_client',
                )
            try:
                self._llm = LLMClient(
                    api_key=Config.TEXT_LLM_API_KEY,
                    base_url=Config.TEXT_LLM_BASE_URL,
                    model=Config.TEXT_LLM_FAST_MODEL,
                    provider=Config.TEXT_LLM_PROVIDER,
                    connect_timeout=min(Config.LLM_CONNECT_TIMEOUT_SECONDS, 5),
                    read_timeout=read_timeout,
                    deadline_epoch=self.deadline_epoch,
                    deadline_reserve_seconds=30,
                    budget_run_id=self.run_id or self.task_id,
                )
            except Exception as error:
                from ..utils.llm_audit import record_llm_client_error, safe_error_summary
                self._llm_unavailable = True
                record_llm_client_error(
                    self.run_id or self.task_id,
                    'analyst',
                    self._llm,
                    error,
                )
                self.logger.log(
                    'analyst',
                    'llm_client_fallback',
                    {'error': safe_error_summary(error)},
                )
        return self._llm

    def plan_outline(self, *, use_llm: bool = True) -> Dict[str, Any]:
        deliverable = self.task_card.deliverable
        skeleton = SKELETONS.get(deliverable, SKELETONS['summary'])
        stats = self.store.statistics()
        names = '、'.join(s.name or s.code or '' for s in self.task_card.symbols) or '标的'
        title_map = {
            'summary': f'{names}信息整理摘要',
            'compare': f'{names}对比分析',
            'tracking': f'{names}信息追踪',
        }
        outline = {
            'title': title_map.get(deliverable, f'{names}投研信息整理'),
            'summary': (
                f'基于 {stats.get("total_cards", 0)} 条'
                f'{"已授权数据与公开信息" if self.has_private_evidence else "公开信息"}'
                '证据卡的信息整理（非投资建议）。'
            ),
            'sections': [{'title': s['title'], 'goal': s['goal']} for s in skeleton],
            'statistics': stats,
        }

        llm = self._get_llm() if use_llm else None
        if llm:
            try:
                prompt = (
                    '你是投研报告的主编。根据任务卡与证据概况，在给定章节骨架上微调，'
                    '输出 JSON：{"title","summary","sections":[{"title","goal"}]}。'
                    f'\n交付物类型：{deliverable}\n骨架：{json.dumps(skeleton, ensure_ascii=False)}'
                    f'\n证据概况：{json.dumps(stats, ensure_ascii=False)}'
                    f'\n证据范围：{"已授权数据与公开信息" if self.has_private_evidence else "公开信息"}'
                    f'\n任务卡：{json.dumps(self.task_card.to_dict(), ensure_ascii=False)}'
                )
                result = llm.chat_json_result(
                    [{'role': 'user', 'content': prompt}],
                    temperature=0.2,
                    max_tokens=2048,
                    max_attempts=2,
                    thinking=False,
                )
                from ..utils.llm_audit import record_llm_result
                record_llm_result(self.run_id or self.task_id, 'planner', result)
                self._llm_outline_used = True
                candidate = result.parsed_json or {}
                candidate_sections = candidate.get('sections') if isinstance(candidate, dict) else None
                if isinstance(candidate_sections, list) and candidate_sections:
                    outline['sections'] = [
                        {
                            'title': str(item.get('title') or skeleton[index]['title']),
                            'goal': str(item.get('goal') or skeleton[index]['goal']),
                        }
                        for index, item in enumerate(candidate_sections[:len(skeleton)])
                        if isinstance(item, dict) and index < len(skeleton)
                    ] or outline['sections']
                # Metadata shown without a citation remains deterministic; an
                # LLM cannot insert an unaudited headline/summary assertion.
                if self.has_private_evidence and isinstance(outline, dict):
                    summary = str(outline.get('summary') or '')
                    if '已授权' not in summary:
                        outline['summary'] = summary.replace(
                            '公开信息', '已授权数据与公开信息'
                        )
            except Exception as e:
                from ..utils.llm_audit import record_llm_client_error, safe_error_summary
                record_llm_client_error(
                    self.run_id or self.task_id,
                    'planner',
                    llm,
                    e,
                )
                self.logger.log(
                    'analyst',
                    'outline_llm_fallback',
                    {'error': safe_error_summary(e)},
                )

        path = os.path.join(self.artifact_folder, 'outline.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(outline, f, ensure_ascii=False, indent=2)
        self.logger.log('analyst', 'outline_ready', {'title': outline.get('title'), 'n': len(outline.get('sections') or [])})
        return outline

    def _load_normalized_facts(self) -> List[Dict[str, Any]]:
        path = os.path.join(self.artifact_folder, 'normalized_facts.jsonl')
        facts: List[Dict[str, Any]] = []
        if not os.path.isfile(path):
            return facts
        with open(path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    if isinstance(item, dict):
                        facts.append(item)
                except json.JSONDecodeError:
                    continue
        return facts

    @staticmethod
    def _fact_field(fact: Dict[str, Any], *names: str) -> Any:
        for name in names:
            if name in fact and fact[name] not in (None, ''):
                return fact[name]
        return None

    def _comparable_financial_groups(self) -> List[Dict[str, Any]]:
        """Return only exact-key groups; unknown fields never become comparable."""
        groups: Dict[tuple, List[Dict[str, Any]]] = {}
        for fact in self._load_normalized_facts():
            metric = self._fact_field(fact, 'metric', 'metric_name')
            period = self._fact_field(fact, 'period', 'reporting_period')
            period_type = self._fact_field(fact, 'period_type', 'report_period_type')
            unit = self._fact_field(fact, 'unit')
            currency = self._fact_field(fact, 'currency')
            scope = self._fact_field(fact, 'consolidation_scope', 'scope')
            cumulative = self._fact_field(fact, 'cumulative', 'accumulation_basis', 'period_basis')
            value = self._fact_field(fact, 'value', 'decimal_value')
            subject = self._fact_field(fact, 'subject', 'symbol', 'entity')
            quality = fact.get('quality_flags') or fact.get('quality_flag') or []
            if any(v is None for v in (metric, period, period_type, unit, currency, scope, cumulative, value, subject)):
                continue
            if fact.get('comparable') is False or fact.get('valid') is False:
                continue
            if isinstance(quality, str):
                quality = [quality]
            if any(flag in {
                'unit_unknown', 'missing_unit', 'period_unknown', 'unknown_period',
                'scope_unknown', 'unknown_consolidation_scope', 'invalid_value',
                'missing_value', 'missing_currency', 'unknown_accumulation_basis',
                'out_of_window', 'outside_time_window',
                'missing_disclosure_time', 'invalid_disclosure_time',
            } for flag in quality):
                continue
            key = (
                str(metric), str(period), str(period_type), str(unit), str(currency),
                str(cumulative), str(scope),
            )
            groups.setdefault(key, []).append(fact)

        out = []
        for key, facts in groups.items():
            subjects = {
                str(self._fact_field(fact, 'subject', 'symbol', 'entity'))
                for fact in facts
            }
            if len(subjects) >= 2:
                out.append({'key': key, 'facts': facts})
        return out

    def _financial_comparison_markdown(self) -> List[str]:
        groups = self._comparable_financial_groups()
        if not groups:
            return ['暂无同口径数据。']

        lines: List[str] = []
        for group in groups[:4]:
            metric, period, period_type, unit, currency, cumulative, scope = group['key']
            lines.extend([
                f'\n**{metric}（{period}/{period_type}，{currency}/{unit}，{cumulative}，{scope}）**',
                '',
                '| 主体 | 数值 | 来源 |',
                '| --- | ---: | --- |',
            ])
            seen = set()
            chart_rows = []
            for fact in group['facts']:
                subject = str(self._fact_field(fact, 'subject', 'symbol', 'entity'))
                if subject in seen:
                    continue
                seen.add(subject)
                value = self._fact_field(fact, 'value', 'decimal_value')
                uid = self._fact_field(fact, 'evidence_uid')
                card = self.store.get(str(uid)) if uid else None
                ref = f'[{self.store.display_id(card)}]' if card else ''
                lines.append(f'| {subject} | {value} | {ref or "证据映射缺失"} |')
                try:
                    chart_rows.append((
                        subject,
                        float(value),
                        ref.strip('[]'),
                        str(self._fact_field(fact, 'fact_uid', 'uid', 'financial_fact_uid') or ''),
                    ))
                except (TypeError, ValueError):
                    pass
            if len(chart_rows) >= 2:
                chart = {
                    'type': 'bar',
                    'title': f'{metric}同口径对照（{period}）',
                    'x': [row[0] for row in chart_rows],
                    'series': [{
                        'name': metric,
                        'data': [row[1] for row in chart_rows],
                        'fact_uids': [row[3] for row in chart_rows],
                    }],
                    'source_refs': [row[2] for row in chart_rows if row[2]],
                    'comparison_basis': {
                        'period': period,
                        'period_type': period_type,
                        'unit': unit,
                        'currency': currency,
                        'cumulative': cumulative,
                        'consolidation_scope': scope,
                    },
                }
                lines.append('\n```chart\n' + json.dumps(chart, ensure_ascii=False) + '\n```\n')
        return lines or ['暂无同口径数据。']

    def _disclosure_rejected_evidence_uids(self) -> set[str]:
        rejected: set[str] = set()
        for fact in self._load_normalized_facts():
            flags = set(fact.get('quality_flags') or [])
            if flags.intersection({'missing_disclosure_time', 'invalid_disclosure_time'}):
                uid = self._fact_field(fact, 'evidence_uid')
                if uid:
                    rejected.add(str(uid))
        return rejected

    def write_section_heuristic(self, section: Dict[str, Any], previous_titles: List[str]) -> str:
        title = section.get('title', '')
        goal = section.get('goal', '')
        # 按章节目标检索
        cards = self.store.search(f'{title} {goal}', limit=8)

        from .memory_service import style_directives
        from .playbook import get_rules, render_rules_for_prompt

        style = style_directives()
        rules_txt = render_rules_for_prompt(get_rules('analyst'))
        evidence_scope = '已授权数据与公开信息' if self.has_private_evidence else '公开信息'
        lines = [f'本章围绕「{goal}」整理{evidence_scope}。']
        if style:
            lines.append(f'（风格偏好：{style}）')

        is_financial = any(token in title for token in ('对照', '对比', '财务', '指标', '表现'))

        # 财务比较由 FinancialFact 确定性渲染；原始证据摘录也必须先过
        # 披露时点门禁，避免被过滤的数字从正文旁路重新出现。
        if is_financial:
            lines.extend(self._financial_comparison_markdown())

        rejected_evidence = self._disclosure_rejected_evidence_uids() if is_financial else set()
        eligible_cards = [
            card for card in cards
            if str(card.evidence_uid or '') not in rejected_evidence
        ]
        for c in eligible_cards[:6]:
            excerpt = (c.excerpt or c.title or '').replace('\n', ' ')
            if len(excerpt) > 180:
                excerpt = excerpt[:180] + '…'
            lines.append(f'- {excerpt}[{self.store.display_id(c)}]')

        if len(cards) < 2:
            lines.append('已采集证据中部分维度未见充分披露，以上仅基于当前证据。')
        if rules_txt:
            self.logger.log('analyst', 'playbook_injected', {'rules': rules_txt[:200]})

        text = '\n'.join(lines)
        self.logger.log('analyst', 'section_heuristic', {'title': title, 'citations': len(cards)})
        return text

    def write_section_llm(self, section: Dict[str, Any], report_title: str, previous_summary: str) -> str:
        llm = self._get_llm()
        if not llm:
            return self.write_section_heuristic(section, [])

        min_tools, max_tools, max_iter = 2, 6, 8
        messages = [
            {
                'role': 'system',
                'content': (
                    f'你是一名严谨的投研信息整理分析师，正在撰写报告《{report_title}》的章节「{section.get("title")}」。'
                    f'本章目标：{section.get("goal")}。\n'
                    '工作方式（ReAct）：\n'
                    '1. 每轮回复要么调用一个工具，要么给出最终答案，二者不可同时出现。\n'
                    '2. 调用工具格式：<tool_call>{"name": "工具名", "parameters": {...}}</tool_call>\n'
                    '3. 信息足够后，以 "Final Answer:" 开头输出章节正文（Markdown，不要使用 # 标题，400-900 字）。\n'
                    f'4. 至少调用 {min_tools} 次工具后才允许 Final Answer；最多 {max_tools} 次。\n'
                    f'可用工具：\n{tools_description(frozen=bool(self.run_id))}\n'
                    '写作铁律：只陈述证据中的事实；每句事实末尾标注 [E{id}]；'
                    '禁止投资建议/目标价/走势预测；证据不足写"当前证据中未见相关披露"。\n'
                    f'已完成章节摘要：{previous_summary or "无"}'
                ),
            },
            {'role': 'user', 'content': f'请开始撰写章节「{section.get("title")}」。'},
        ]

        tool_calls = 0
        for _ in range(max_iter):
            try:
                result = llm.chat_result(
                    messages,
                    temperature=0.3,
                    max_tokens=4096,
                    thinking=False,
                )
                from ..utils.llm_audit import record_llm_result
                record_llm_result(self.run_id or self.task_id, 'analyst', result)
                reply = result.content
            except Exception as e:
                from ..utils.llm_audit import record_llm_client_error, safe_error_summary
                record_llm_client_error(
                    self.run_id or self.task_id,
                    'analyst',
                    llm,
                    e,
                )
                self.logger.log(
                    'analyst',
                    'section_llm_error',
                    {'error': safe_error_summary(e)},
                )
                return self.write_section_heuristic(section, [])

            messages.append({'role': 'assistant', 'content': reply})
            final = FINAL_RE.search(reply or '')
            tool_m = TOOL_CALL_RE.search(reply or '')

            if final and tool_calls >= min_tools:
                candidate = final.group(1).strip()
                accepted = self._accept_grounded_llm_section(section, candidate)
                return accepted if accepted is not None else self.write_section_heuristic(section, [])

            if tool_m and tool_calls < max_tools:
                try:
                    spec = json.loads(tool_m.group(1))
                    name = spec.get('name')
                    params = spec.get('parameters') or {}
                    result = call_analyze_tool(self.task_id, name, params, run_id=self.run_id)
                    tool_calls += 1
                    payload = result.data if result.ok else {'error': result.error}
                    messages.append({
                        'role': 'user',
                        'content': f'<tool_result name="{name}">\n{json.dumps(payload, ensure_ascii=False)[:6000]}\n</tool_result>',
                    })
                    self.logger.log('analyst', 'tool_call', {'name': name, 'ok': result.ok})
                    continue
                except Exception as e:
                    messages.append({'role': 'user', 'content': f'工具调用失败: {e}'})
                    continue

            if final:
                messages.append({
                    'role': 'user',
                    'content': f'请先完成至少 {min_tools} 次冻结证据检索，再输出 Final Answer。',
                })
                continue

            messages.append({'role': 'user', 'content': '请按协议继续：调用工具或输出 Final Answer。'})

        return self.write_section_heuristic(section, [])

    def _accept_grounded_llm_section(
        self,
        section: Dict[str, Any],
        candidate: str,
    ) -> Optional[str]:
        """Accept DeepSeek prose only when every factual line is source-exact."""

        from .compliance_checker import check_compliance
        from .report_assembler import _direct_evidence_issue

        if check_compliance(candidate):
            issue = 'compliance_blacklist'
        else:
            issue = _direct_evidence_issue(
                {
                    'title': section.get('title'),
                    'goal': section.get('goal'),
                    'content': candidate,
                },
                self.store,
            )
        if issue:
            self.logger.log(
                'analyst',
                'llm_section_rejected',
                {'title': section.get('title'), 'reason': str(issue)[:160]},
            )
            return None
        self._llm_writing_used = True
        return candidate

    @staticmethod
    def _list_value(value: Any) -> List[Any]:
        if value in (None, ''):
            return []
        return value if isinstance(value, list) else [value]

    def _fact_evidence_map(self) -> Dict[str, str]:
        mapping: Dict[str, str] = {}
        for fact in self._load_normalized_facts():
            fact_uid = self._fact_field(fact, 'fact_uid', 'uid', 'financial_fact_uid')
            evidence_uid = self._fact_field(fact, 'evidence_uid')
            if fact_uid and evidence_uid:
                mapping[str(fact_uid)] = str(evidence_uid)
        return mapping

    def _render_verdict_item(self, item: Any, fact_map: Dict[str, str]) -> str:
        if not isinstance(item, dict):
            return str(item)
        text = str(
            item.get('statement')
            or item.get('text')
            or item.get('description')
            or item.get('claim')
            or item.get('reason')
            or ''
        ).strip()
        evidence_refs = self._list_value(
            item.get('evidence_uids')
            or item.get('evidence_refs')
            or item.get('evidence_ids')
        )
        for fact_uid in self._list_value(item.get('fact_uids') or item.get('financial_fact_uids')):
            if str(fact_uid) in fact_map:
                evidence_refs.append(fact_map[str(fact_uid)])
        rendered: List[str] = []
        for ref in evidence_refs:
            raw = ref.get('evidence_uid') if isinstance(ref, dict) else ref
            card = self.store.get(str(raw))
            if card:
                rendered.append(f'[{self.store.display_id(card)}]')
            elif re.fullmatch(r'E\d+', str(raw), flags=re.IGNORECASE):
                rendered.append(f'[{str(raw).upper()}]')
        return (text or '（未提供文本）') + ''.join(dict.fromkeys(rendered))

    def _sections_from_verdict(self, verdict: Dict[str, Any]) -> List[Dict[str, Any]]:
        fact_map = self._fact_evidence_map()
        specs = [
            ('共识事实', verdict.get('consensus_facts')),
            ('有证据支持的解释', verdict.get('supported_interpretations') or verdict.get('interpretations')),
            ('主要反证', verdict.get('major_challenges') or verdict.get('key_challenges')),
            ('未决分歧', verdict.get('unresolved_disagreements') or verdict.get('unresolved_disputes') or verdict.get('disagreements')),
            ('撤回观点', verdict.get('withdrawn_claims')),
            ('证据不足', verdict.get('evidence_gaps')),
            ('后续公开事项', verdict.get('follow_up_public_events') or verdict.get('follow_up_public_items') or verdict.get('follow_ups')),
        ]
        sections = []
        for title, values in specs:
            rows = self._list_value(values)
            content = '\n'.join(f'- {self._render_verdict_item(row, fact_map)}' for row in rows)
            if not content:
                content = '- 当前冻结证据中暂无可裁决内容。'
            sections.append({
                'title': title,
                'goal': '忠实表达通过证据审计的辩论裁决',
                'content': content,
                'audited_debate': True,
            })
        return sections

    def _express_debate_with_llm(
        self,
        title: str,
        summary: str,
        sections: List[Dict[str, Any]],
    ) -> tuple[str, str, List[Dict[str, Any]]]:
        llm = self._get_llm()
        if not llm:
            return title, summary, sections
        required_titles = [section['title'] for section in sections]
        prompt = (
            '你是报告表达编辑。下列章节均由通过硬审计的 ClaimCard 确定性渲染，'
            '正文不可改写；你的职责是根据阅读逻辑安排章节顺序。'
            '输出 JSON，示例：{"section_order":["共识事实","主要反证","未决分歧"]}。'
            '必须且只能包含所有给定标题，每个标题恰好一次；不要输出正文或思维过程。'
            f'可用标题：{json.dumps(required_titles, ensure_ascii=False)}。\n'
            f'章节概览：{json.dumps([{"title": item["title"], "content": item.get("content", "")[:500]} for item in sections], ensure_ascii=False)}'
        )
        try:
            result = llm.chat_json_result(
                [{'role': 'user', 'content': prompt}],
                temperature=0.2,
                max_tokens=4096,
                max_attempts=2,
                thinking=False,
            )
            from ..utils.llm_audit import record_llm_result
            record_llm_result(self.run_id or self.task_id, 'analyst', result)
            data = result.parsed_json or {}
            order = data.get('section_order')
            if (
                not isinstance(order, list)
                or len(order) != len(required_titles)
                or set(map(str, order)) != set(required_titles)
                or len(set(map(str, order))) != len(required_titles)
            ):
                raise ValueError('Analyst 未返回完整且唯一的 section_order')
            by_title = {item['title']: item for item in sections}
            ordered = [by_title[str(item)] for item in order]
            self._llm_expression_used = True
            return title, summary, ordered
        except Exception as exc:
            from ..utils.llm_audit import record_llm_client_error, safe_error_summary
            record_llm_client_error(
                self.run_id or self.task_id,
                'analyst',
                llm,
                exc,
            )
            self.logger.log(
                'analyst',
                'debate_expression_fallback',
                {'error': safe_error_summary(exc)},
            )
            return title, summary, sections

    def run(
        self,
        debate_verdict: Optional[Dict[str, Any]] = None,
        *,
        debate_status: Optional[str] = None,
        debate_fallback_reason: Optional[str] = None,
    ) -> Dict[str, Any]:
        outline = self.plan_outline(use_llm=not bool(debate_verdict))
        if debate_verdict:
            sections_out = self._sections_from_verdict(debate_verdict)
            report_title, report_summary, sections_out = self._express_debate_with_llm(
                outline.get('title', ''),
                '基于同一冻结证据快照，经两轮多视角质询、确定性证据审计与裁决形成。',
                sections_out,
            )
        else:
            report_title = outline.get('title')
            report_summary = outline.get('summary')
            sections_out = []
            previous = []
            for sec in outline.get('sections') or []:
                title = str(sec.get('title') or '')
                # Financial comparisons and all compare deliverables are
                # rendered only from exact-key FinancialFact groups. Free-form
                # model prose must never create a mixed-period table/chart.
                deterministic_financial = any(
                    token in title for token in ('财务', '指标', '对照', '对比', '表现')
                )
                # Direct-report facts are rendered as exact evidence excerpts.
                # DeepSeek still plans the outline and Reviewer classifies
                # issues, but free-form prose cannot create unsupported facts.
                body = (
                    self.write_section_llm(
                        sec,
                        str(report_title or ''),
                        '\n'.join(previous),
                    )
                    if not deterministic_financial and self._get_llm()
                    else self.write_section_heuristic(sec, previous)
                )
                sections_out.append({
                    'title': sec.get('title'),
                    'goal': sec.get('goal'),
                    'content': body,
                    'deterministic_financial': deterministic_financial,
                })
                previous.append(f'{sec.get("title")}: {(body or "")[:120]}')

        draft = {
            'title': report_title,
            'summary': report_summary,
            'sections': sections_out,
            'mode': (
                'llm'
                if self._llm_writing_used or self._llm_expression_used
                else ('hybrid' if self._llm_outline_used else 'heuristic')
            ),
            'analysis_mode': self.task_card.analysis_mode,
            'debate_status': debate_status,
            'debate_fallback_reason': debate_fallback_reason,
            'debate_verdict': debate_verdict,
        }
        path = os.path.join(self.artifact_folder, 'draft_report.json')
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        self.logger.log('analyst', 'draft_ready', {'sections': len(sections_out), 'mode': draft['mode']})
        return draft
