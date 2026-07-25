"""EvidenceCard 统一返回结构（04§1）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


@dataclass
class EvidenceCard:
    source_type: str  # announcement | financial_report | news | research_report | industry_data | web_search
    title: str
    url: str
    publish_time: str
    source_name: str
    symbol: Optional[str] = None
    excerpt: str = ''
    structured: Dict[str, Any] = field(default_factory=dict)
    reliability: int = 3
    fetch_tool: str = ''
    card_id: Optional[int] = None  # 任务内自增，入图/报告角标用

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def reliability_for(source_name: str, source_type: str) -> int:
    name = (source_name or '').lower()
    if source_type in ('announcement', 'financial_report') or '巨潮' in name or 'cninfo' in name:
        return 5
    if any(k in name for k in ('东方财富', 'eastmoney', '财联社', '新浪', '券商', '研报')):
        return 4
    if source_type == 'research_report':
        return 4
    if source_type == 'web_search' or '博查' in name:
        return 3
    return 3
