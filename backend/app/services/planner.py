"""Planner Agent：自然语言需求 → TaskCard（03§A1）。"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from ..config import Config
from ..models.task_card import TaskCard
from ..utils.llm_client import LLMClient
from ..utils.locale import get_language_instruction
from ..utils.logger import get_logger

logger = get_logger('chengzhu.planner')

# 常见简称 → 代码（Demo 兜底，无 LLM 时也能解析）
SYMBOL_ALIAS = {
    '茅台': ('600519', '贵州茅台'),
    '贵州茅台': ('600519', '贵州茅台'),
    '宁德时代': ('300750', '宁德时代'),
    '宁德': ('300750', '宁德时代'),
    '比亚迪': ('002594', '比亚迪'),
    '五粮液': ('000858', '五粮液'),
    '中国平安': ('601318', '中国平安'),
    '招商银行': ('600036', '招商银行'),
}

PLANNER_SYSTEM = """你是一名资深投研助理的需求分析模块。你的唯一任务是把用户的自然语言投研需求解析为结构化任务卡 JSON，供后续采集与分析系统执行。

解析规则：
1. deliverable 判定：出现"对比/比较/vs" → compare；出现"追踪/跟踪/盯/持续/每天/每周" → tracking；否则 → summary。若同时出现对比与追踪，主交付物取 compare，并在 clarifications 中提示可另行开启追踪订阅。
2. symbols：识别公司名/股票代码/简称，输出 6 位代码与标准名称。无法确定代码时 code 填 null，并在 clarifications 中列出待确认项。不允许编造代码。
3. time_window：解析"最近一个季度/今年以来/近半年"等口语时间。默认值：summary 取最近 90 天；compare 取最近 4 个报告期对应约 400 天；tracking 取最近 7 天。今天的日期是 {today}。
4. info_types：用户明确排除的类型不要包含；未提及则默认全部 5 类：announcement,financial_report,news,research_report,industry_data。
5. focus_points：提取用户强调的关注点原词，不要扩写。
6. 用户历史偏好（供参考，可据此填充默认值，但用户本次明确说的内容优先）：
{user_memory_context}
7. 任何你拿不准、需要用户确认的假设，一律写入 clarifications 数组，禁止沉默假设。
8. 只输出 JSON，不输出任何其他文本。字段：deliverable, symbols[{{code,name}}], time_window{{start,end}}, info_types, focus_points, compare_dimensions, output_language_style, clarifications

{playbook_rules}
"""


def _default_window(deliverable: str) -> Dict[str, str]:
    today = datetime.now().date()
    if deliverable == 'tracking':
        start = today - timedelta(days=7)
    elif deliverable == 'compare':
        start = today - timedelta(days=400)
    else:
        start = today - timedelta(days=90)
    return {'start': start.isoformat(), 'end': today.isoformat()}


def _heuristic_parse(requirement: str) -> TaskCard:
    text = requirement or ''
    deliverable = 'summary'
    if re.search(r'对比|比较|\bvs\b|VS', text, re.I):
        deliverable = 'compare'
    if re.search(r'追踪|跟踪|盯|持续|每天|每周', text):
        if deliverable != 'compare':
            deliverable = 'tracking'

    symbols = []
    for alias, (code, name) in SYMBOL_ALIAS.items():
        if alias in text:
            if not any(s['code'] == code for s in symbols):
                symbols.append({'code': code, 'name': name})
    for m in re.finditer(r'\b(\d{6})\b', text):
        code = m.group(1)
        if not any(s['code'] == code for s in symbols):
            symbols.append({'code': code, 'name': ''})

    clarifications: List[str] = []
    if not symbols:
        clarifications.append('未识别到股票代码或公司简称，请补充标的')

    card = TaskCard.from_dict({
        'deliverable': deliverable,
        'symbols': symbols or [{'code': None, 'name': ''}],
        'time_window': _default_window(deliverable),
        'info_types': [
            'announcement', 'financial_report', 'news', 'research_report', 'industry_data',
        ],
        'focus_points': [],
        'compare_dimensions': ['盈利能力', '现金流'] if deliverable == 'compare' else [],
        'output_language_style': 'professional_brief',
        'clarifications': clarifications,
    })
    return card


class PlannerService:
    def __init__(
        self,
        llm: Optional[LLMClient] = None,
        *,
        budget_run_id: Optional[str] = None,
    ):
        self._llm = llm
        self._budget_run_id = budget_run_id

    @property
    def llm(self) -> LLMClient:
        if self._llm is None:
            self._llm = LLMClient(budget_run_id=self._budget_run_id)
        return self._llm

    def plan(
        self,
        requirement: str,
        user_memory_context: str = '（暂无）',
        playbook_rules: str = '',
        run_id: Optional[str] = None,
    ) -> TaskCard:
        if not Config.LLM_API_KEY:
            logger.info('无 LLM_API_KEY，使用启发式 Planner')
            return _heuristic_parse(requirement)

        # Planner executes before a concrete run exists.  Its task-scoped
        # reservation and metadata are transferred to the confirmed run by
        # ``assign_pending_llm_logs``; this still enforces the same 2 CNY task
        # budget while the task card is being created.
        if self._llm is None and run_id:
            self._budget_run_id = run_id

        today = datetime.now().date().isoformat()
        system = PLANNER_SYSTEM.format(
            today=today,
            user_memory_context=user_memory_context or '（暂无）',
            playbook_rules=playbook_rules or '',
        ) + '\n' + get_language_instruction()

        try:
            messages = [
                {'role': 'system', 'content': system},
                {'role': 'user', 'content': requirement},
            ]
            from ..utils.llm_audit import ensure_llm_call_budget
            ensure_llm_call_budget(
                run_id,
                provider=str(getattr(self.llm, 'provider', None) or Config.TEXT_LLM_PROVIDER),
                model=str(getattr(self.llm, 'model', None) or Config.TEXT_LLM_FAST_MODEL),
                messages=messages,
                max_tokens=2048,
                attempts=2 * (1 + int(getattr(self.llm, 'max_retries', 0) or 0)),
            )
            result = self.llm.chat_json_result(
                messages=messages,
                temperature=0.2,
                max_tokens=2048,
                max_attempts=2,
                thinking=False,
            )
            from ..utils.llm_audit import record_llm_result
            record_llm_result(run_id, 'planner', result)
            data = result.parsed_json or {}
            card = TaskCard.from_dict(data)
            # 补默认时间窗
            if not card.time_window.get('start'):
                card.time_window = _default_window(card.deliverable)
            errors = card.validate()
            # code 可为 null（待确认），validate 对 null 放宽：临时改
            if errors and all('非法股票代码' not in e and 'symbols 不能为空' not in e for e in errors):
                # 仅时间窗等问题用启发式修补
                pass
            if not card.symbols:
                return _heuristic_parse(requirement)
            return card
        except Exception as e:
            from ..utils.llm_audit import record_llm_client_error, safe_error_summary
            record_llm_client_error(run_id, 'planner', self._llm, e)
            logger.warning(
                '使用启发式 Planner 降级：%s',
                safe_error_summary(e),
            )
            return _heuristic_parse(requirement)
