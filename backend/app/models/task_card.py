"""TaskCard 数据契约（03§A1）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


ALLOWED_DELIVERABLES = {'summary', 'compare', 'tracking'}
ALLOWED_ANALYSIS_MODES = {'direct', 'evidence_debate'}
ALLOWED_EXECUTION_MODES = {'agentteams', 'replay'}
ALLOWED_INFO_TYPES = {
    'announcement', 'financial_report', 'news', 'research_report', 'industry_data',
}


@dataclass
class SymbolRef:
    code: Optional[str]
    name: str = ''
    # DataYes 证券主表中的稳定标识。旧任务卡没有该字段，因此保持可选。
    sec_id: Optional[str] = None
    exchange: str = ''
    market: str = ''
    list_status: str = ''

    def to_dict(self) -> Dict[str, Any]:
        data: Dict[str, Any] = {'code': self.code, 'name': self.name}
        for key in ('sec_id', 'exchange', 'market', 'list_status'):
            value = getattr(self, key)
            if value not in (None, ''):
                data[key] = value
        return data


@dataclass
class TaskCard:
    deliverable: str
    symbols: List[SymbolRef]
    time_window: Dict[str, str]
    info_types: List[str] = field(default_factory=lambda: list(ALLOWED_INFO_TYPES))
    focus_points: List[str] = field(default_factory=list)
    compare_dimensions: List[str] = field(default_factory=list)
    output_language_style: str = 'professional_brief'
    clarifications: List[str] = field(default_factory=list)
    # 旧任务卡没有该字段，反序列化时必须回退到 direct。
    analysis_mode: str = 'direct'
    # 实时任务统一由 AgentTeams 执行；demo_seed 会在装载时显式标为 replay。
    execution_mode: str = 'agentteams'

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d['symbols'] = [s.to_dict() if isinstance(s, SymbolRef) else s for s in self.symbols]
        return d

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'TaskCard':
        symbols = [
            SymbolRef(
                code=s.get('code'),
                name=s.get('name', ''),
                sec_id=s.get('sec_id'),
                exchange=s.get('exchange', ''),
                market=s.get('market', ''),
                list_status=s.get('list_status', ''),
            )
            for s in (data.get('symbols') or [])
        ]
        return cls(
            deliverable=data.get('deliverable', 'summary'),
            symbols=symbols,
            time_window=data.get('time_window') or {},
            info_types=data.get('info_types') or list(ALLOWED_INFO_TYPES),
            focus_points=data.get('focus_points') or [],
            compare_dimensions=data.get('compare_dimensions') or [],
            output_language_style=data.get('output_language_style', 'professional_brief'),
            clarifications=data.get('clarifications') or [],
            analysis_mode=data.get('analysis_mode') or 'direct',
            execution_mode=data.get('execution_mode') or 'agentteams',
        )

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.deliverable not in ALLOWED_DELIVERABLES:
            errors.append(f'非法 deliverable: {self.deliverable}')
        if self.analysis_mode not in ALLOWED_ANALYSIS_MODES:
            errors.append(f'非法 analysis_mode: {self.analysis_mode}')
        if self.execution_mode not in ALLOWED_EXECUTION_MODES:
            errors.append(f'非法 execution_mode: {self.execution_mode}')
        if self.deliverable == 'tracking' and self.analysis_mode == 'evidence_debate':
            errors.append('tracking 任务暂不支持 evidence_debate')
        if not self.symbols:
            errors.append('symbols 不能为空')
        seen_codes = set()
        for s in self.symbols:
            # code 可为 null（等待用户确认），仅当给出非空值时校验 6 位数字
            if s.code in (None, '', 'null'):
                continue
            code = str(s.code)
            if not code.isdigit() or len(code) != 6:
                errors.append(f'非法股票代码: {s.code}')
                continue
            if code in seen_codes:
                errors.append(f'请勿重复添加股票代码: {code}')
            seen_codes.add(code)
        if not self.time_window.get('start') or not self.time_window.get('end'):
            errors.append('time_window 需要 start/end')
        bad = [t for t in self.info_types if t not in ALLOWED_INFO_TYPES]
        if bad:
            errors.append(f'非法 info_types: {bad}')
        return errors
