"""EvidenceCard 统一返回结构（04§1）。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
import re
from typing import Any, Dict, Optional


PROVENANCE_FIELDS = (
    'provider', 'api', 'record_key', 'business_key', 'as_of', 'update_time',
    'warehouse_watermark', 'row_fingerprint', 'upstream_source', 'license_scope',
)


def _redact_text(value: str) -> str:
    value = re.sub(
        r'(?i)\bauthorization\s*[:=]\s*[^\r\n,;]+',
        'Authorization: [REDACTED]',
        value,
    )
    value = re.sub(r'(?i)\bbearer\s+[^\s,;]+', 'Bearer [REDACTED]', value)
    return re.sub(
        r'(?i)\b(token|api[_-]?key)\s*[:=]\s*[^\s,;]+',
        r'\1=[REDACTED]',
        value,
    )


def _jsonable(obj: Any) -> Any:
    """把 pandas Timestamp / date / numpy 等转成可 JSON 序列化类型。"""
    if obj is None:
        return None
    if isinstance(obj, str):
        return _redact_text(obj)
    if isinstance(obj, (int, float, bool)):
        return obj
    if isinstance(obj, datetime):
        return obj.isoformat(timespec='seconds')
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    # pandas / numpy
    try:
        import pandas as pd
        if isinstance(obj, pd.Timestamp):
            return obj.isoformat()
        if obj is pd.NaT:
            return None
    except Exception:
        pass
    try:
        import numpy as np
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            f = float(obj)
            return None if f != f else f
        if isinstance(obj, np.bool_):
            return bool(obj)
    except Exception:
        pass
    if obj != obj:  # NaN
        return None
    return str(obj)


@dataclass
class ToolResult:
    """分析/检索工具统一返回（ok + data / error）。"""
    ok: bool
    tool: str
    data: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    meta: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return _jsonable(asdict(self))


@dataclass
class EvidenceCard:
    source_type: str  # announcement | financial_report | news | research_report | industry_data | web_search
    title: str
    url: Optional[str]
    publish_time: str
    source_name: str
    symbol: Optional[str] = None
    excerpt: str = ''
    structured: Dict[str, Any] = field(default_factory=dict)
    reliability: int = 3
    fetch_tool: str = ''
    card_id: Optional[int] = None  # 任务内自增，入图/报告角标用
    # 新字段放在旧字段之后，保持历史位置参数调用的顺序兼容。
    # 结构化数据不一定有公开网页 URL；这里保存可审计的数据来源，
    # 但不得放置 token、Authorization 等凭证。
    provenance: Optional[Dict[str, Any]] = None
    # 内容稳定标识；card_id 仅是当次 run 内 E1…En 的显示顺序。
    evidence_uid: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        if isinstance(d.get('provenance'), dict):
            # 公共返回采用 allowlist，避免未来 Provider 误把凭证或请求头带入
            # JSONL、Agent prompt 与前端 API。
            d['provenance'] = {
                key: value
                for key, value in d['provenance'].items()
                if key in PROVENANCE_FIELDS
            }
        return _jsonable(d)


def reliability_for(source_name: str, source_type: str) -> int:
    name = (source_name or '').lower()
    if source_type in ('announcement', 'financial_report') or '巨潮' in name or 'cninfo' in name:
        return 5
    # Datayes 的公司披露/财务事实由上面的原始披露规则计 5；估值、
    # 行业资金流等结构化或计算数据计 4。
    if 'datayes' in name or '通联数据' in name:
        return 4
    if any(k in name for k in ('东方财富', 'eastmoney', '财联社', '新浪', '券商', '研报')):
        return 4
    if source_type == 'research_report':
        return 4
    if source_type == 'web_search' or '博查' in name:
        return 3
    return 3
