"""Datayes API/Parquet 统一字段、类型、版本键与指纹。"""

from __future__ import annotations

import hashlib
import json
import re
from decimal import Decimal, InvalidOperation
from datetime import date, datetime
from typing import Any, Dict, Iterable, Mapping, Optional

from .manifest import EndpointSpec, canonical_type


NULL_VALUES = {'', 'N/A', 'NULL', 'null', 'None', '--'}


def _shanghai_iso(value: datetime) -> str:
    try:
        from zoneinfo import ZoneInfo
        zone = ZoneInfo('Asia/Shanghai')
        value = value.replace(tzinfo=zone) if value.tzinfo is None else value.astimezone(zone)
    except Exception:
        pass
    return value.isoformat(timespec='milliseconds' if value.microsecond else 'seconds')


def to_snake(name: str) -> str:
    value = str(name or '').strip()
    value = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', value)
    value = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', value)
    return value.lower()


def _date_string(value: Any, with_time: bool = False) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        return _shanghai_iso(value) if with_time else value.date().isoformat()
    if isinstance(value, date):
        return _shanghai_iso(datetime.combine(value, datetime.min.time())) if with_time else value.isoformat()
    text = str(value).strip()
    if not text or text in NULL_VALUES:
        return None
    if with_time:
        if re.match(r'^\d{4}-\d{2}-\d{2}', text):
            try:
                parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
                return _shanghai_iso(parsed)
            except ValueError:
                return None
        digits = ''.join(ch for ch in text if ch.isdigit())
        # DataAPI 常见 YYYYMMDDHHMMSS[mmm] 紧凑格式。
        if len(digits) >= 14 and 1900 <= int(digits[:4]) <= 2100:
            fmt = '%Y%m%d%H%M%S%f' if len(digits) >= 17 else '%Y%m%d%H%M%S'
            compact = digits[:17] if len(digits) >= 17 else digits[:14]
            return _shanghai_iso(datetime.strptime(compact, fmt))
        if len(digits) == 8:
            return _shanghai_iso(datetime.strptime(digits, '%Y%m%d'))
        return None
    digits = ''.join(ch for ch in text if ch.isdigit())
    if len(digits) >= 8 and 1900 <= int(digits[:4]) <= 2100:
        return datetime.strptime(digits[:8], '%Y%m%d').date().isoformat()
    try:
        return date.fromisoformat(text[:10]).isoformat()
    except ValueError:
        return None


def normalize_value(value: Any, datayes_type: str) -> Any:
    if value is None or (isinstance(value, str) and value.strip() in NULL_VALUES):
        return None
    kind = canonical_type(datayes_type)
    try:
        if kind == 'integer':
            if isinstance(value, int):
                return value
            number = Decimal(str(value).strip())
            if number != number.to_integral_value():
                return None
            return int(number)
        if kind == 'number':
            number = float(value)
            return None if number != number else number
        if kind == 'date':
            return _date_string(value)
        if kind == 'datetime':
            return _date_string(value, with_time=True)
        return str(value).strip()
    except (TypeError, ValueError, InvalidOperation):
        return None


def normalize_row(spec: EndpointSpec, row: Mapping[str, Any]) -> Dict[str, Any]:
    """API camelCase 和仓库 snake_case 均归一成稳定 snake_case。"""
    by_lower = {str(k).strip().lower(): v for k, v in row.items()}
    out: Dict[str, Any] = {}
    for original, dtype in spec.output_types.items():
        snake = to_snake(original)
        value = None
        for candidate in (original, snake):
            if candidate in row:
                value = row[candidate]
                break
            if candidate.lower() in by_lower:
                value = by_lower[candidate.lower()]
                break
        out[snake] = normalize_value(value, dtype)
    return out


def normalize_rows(spec: EndpointSpec, rows: Iterable[Mapping[str, Any]]) -> list:
    return [normalize_row(spec, row) for row in rows]


def row_fingerprint(row: Mapping[str, Any]) -> str:
    payload = json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def _key(parts: Iterable[Any]) -> str:
    payload = '|'.join('' if v is None else str(v) for v in parts)
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()


def business_key(spec: EndpointSpec, row: Mapping[str, Any]) -> str:
    parts = []
    semantic_parts = []
    for field in spec.business_key_fields:
        if field == 'api':
            parts.append(spec.api)
        else:
            value = row.get(field)
            parts.append(value)
            semantic_parts.append(value)
    # 固定 api 字符串不是业务身份；业务字段全空时必须逐行 fallback。
    if not any(value not in (None, '') for value in semantic_parts):
        parts = [spec.api, row_fingerprint(row)]
    return f'{spec.api}:{_key(parts)}'


def record_key(spec: EndpointSpec, row: Mapping[str, Any], bkey: Optional[str] = None) -> str:
    bkey = bkey or business_key(spec, row)
    parts = []
    for field in spec.record_key_fields:
        if field == 'business_key':
            parts.append(bkey)
        elif field == 'api':
            parts.append(spec.api)
        else:
            parts.append(row.get(field))
    if not parts:
        parts = [bkey, row_fingerprint(row)]
    return f'{spec.api}:{_key(parts)}'
