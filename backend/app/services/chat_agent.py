"""报告对话 Agent：基于已生成报告 + 本地证据检索回答。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from ..config import Config
from ..constants import DISCLAIMER
from .report_assembler import load_report
from ..tools.graph_local import call_analyze_tool

TOOL_CALL_RE = re.compile(r'<tool_call>\s*(\{.*?\})\s*</tool_call>', re.DOTALL)


class ChatAgent:
    def __init__(self, task_id: str, run_id: Optional[str] = None):
        self.task_id = task_id
        self.run_id = run_id
        self.report = load_report(task_id, run_id=run_id)

    def _context_snippet(self, question: str) -> str:
        if not self.report:
            return '（报告尚未生成）'
        parts = [f"报告标题：{self.report.get('title')}", f"摘要：{self.report.get('summary')}"]
        q = question or ''
        for s in self.report.get('sections') or []:
            title = s.get('title') or ''
            body = s.get('content') or ''
            if any(t in title or t in body for t in q.split() if len(t) > 1) or len(parts) < 4:
                parts.append(f"## {title}\n{body[:800]}")
        return '\n\n'.join(parts[:8])

    def _heuristic_answer(
        self,
        question: str,
        report_disclaimer: str,
        *,
        llm_degraded: bool = False,
    ) -> Dict[str, Any]:
        result = call_analyze_tool(
            self.task_id,
            'graph_quick_search',
            {'query': question, 'limit': 5},
            run_id=self.run_id,
        )
        text = (result.data or {}).get('text', '')
        mode_note = (
            '大模型不可用，已降级为本地证据检索'
            if llm_degraded
            else '启发式，未调用大模型'
        )
        answer = (
            f'基于本报告与相关证据，简要回答如下（{mode_note}）：\n\n'
            f'{text[:2000] if text else "当前证据中暂未检索到直接相关内容。"}\n\n'
            f'> {report_disclaimer}'
        )
        ids = (result.data or {}).get('card_ids') or []
        return {
            'answer': answer,
            'citations': ids,
            'correction_flag': '错误' in question or '不对' in question,
        }

    def ask(self, question: str, history: Optional[List[Dict[str, str]]] = None) -> Dict[str, Any]:
        if not self.report:
            return {
                'answer': '报告尚未生成，请先完成任务。可新建任务重新整理。',
                'citations': [],
                'correction_flag': False,
            }

        report_disclaimer = self.report.get('disclaimer') or DISCLAIMER

        # 无 Key：启发式回答
        if not Config.TEXT_LLM_API_KEY:
            return self._heuristic_answer(question, report_disclaimer)

        from ..utils.llm_client import LLMClient
        llm = None
        try:
            llm = LLMClient(
                api_key=Config.TEXT_LLM_API_KEY,
                base_url=Config.TEXT_LLM_BASE_URL,
                model=Config.TEXT_LLM_FAST_MODEL,
                provider=Config.TEXT_LLM_PROVIDER,
                # The API resolves a concrete run before constructing ChatAgent.
                # Legacy root-only reports intentionally remain unbudgeted rather
                # than guessing which historical run is current.
                budget_run_id=self.run_id,
            )
        except Exception as error:
            from ..utils.llm_audit import record_llm_client_error
            record_llm_client_error(self.run_id, 'chat', llm, error)
            return self._heuristic_answer(
                question,
                report_disclaimer,
                llm_degraded=True,
            )
        messages: List[Dict[str, str]] = [
            {
                'role': 'system',
                'content': (
                    '你是这份投研信息整理报告的作者。回答遵守：只依据报告与工具证据；'
                    '事实句带 [E{id}]；禁止投资建议与走势预测；超出范围时明确说明。\n'
                    f'报告上下文：\n{self._context_snippet(question)}'
                ),
            }
        ]
        for h in history or []:
            messages.append({'role': h.get('role', 'user'), 'content': h.get('content', '')})
        messages.append({'role': 'user', 'content': question})

        tool_calls = 0
        answer = ''
        for _ in range(2):
            try:
                llm_result = llm.chat_result(
                    messages,
                    temperature=0.3,
                    max_tokens=2048,
                    thinking=False,
                )
                from ..utils.llm_audit import record_llm_result
                record_llm_result(self.run_id, 'chat', llm_result)
            except Exception as error:
                from ..utils.llm_audit import record_llm_client_error
                record_llm_client_error(self.run_id, 'chat', llm, error)
                return self._heuristic_answer(
                    question,
                    report_disclaimer,
                    llm_degraded=True,
                )
            reply = llm_result.content
            messages.append({'role': 'assistant', 'content': reply})
            m = TOOL_CALL_RE.search(reply or '')
            if m and tool_calls < 2:
                try:
                    spec = json.loads(m.group(1))
                    result = call_analyze_tool(
                        self.task_id,
                        spec.get('name', ''),
                        spec.get('parameters') or {},
                        run_id=self.run_id,
                    )
                    tool_calls += 1
                    messages.append({
                        'role': 'user',
                        'content': f'<tool_result>{json.dumps(result.data if result.ok else {"error": result.error}, ensure_ascii=False)[:4000]}</tool_result>',
                    })
                    continue
                except Exception as e:
                    messages.append({'role': 'user', 'content': f'工具失败: {e}'})
            answer = reply or ''
            break

        correction = '<!--correction-->' in answer or any(k in question for k in ('错误', '不对', '更正'))
        if correction and '<!--correction-->' not in answer:
            answer = answer + '\n<!--correction-->'
        from .compliance_checker import check_compliance, strip_advice_phrases
        if check_compliance(answer):
            answer = strip_advice_phrases(answer)
        from .evidence_store import EvidenceStore
        store = EvidenceStore(self.task_id, run_id=self.run_id)
        valid_ids = {
            int(card.card_id) for card in store.cards if card.card_id is not None
        }
        cited_ids = [int(x) for x in re.findall(r'\[E(\d+)\]', answer)]
        invalid_ids = set(cited_ids) - valid_ids
        for evidence_id in invalid_ids:
            answer = answer.replace(f'[E{evidence_id}]', '[证据不足]')
        return {
            'answer': answer.replace('<!--correction-->', '').strip() + f'\n\n> {report_disclaimer}',
            'citations': [value for value in cited_ids if value in valid_ids],
            'correction_flag': correction,
        }
