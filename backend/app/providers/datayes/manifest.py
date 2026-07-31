"""读取运行时自包含的 29 接口审核白名单。"""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .errors import EndpointNotAllowed, ParameterValidationError


MANIFEST_PATH = os.path.join(os.path.dirname(__file__), 'reviewed_endpoints.json')


def _parameter_values(value: Any) -> Tuple[Any, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    if isinstance(value, str) and ',' in value:
        return tuple(part.strip() for part in value.split(',') if part.strip())
    return (value,)


def _parameter_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return True


def _validate_scalar(name: str, value: Any, source_type: str) -> None:
    kind = canonical_type(source_type)
    try:
        if kind == 'date':
            if isinstance(value, datetime):
                return
            if isinstance(value, date):
                return
            text = str(value).strip()
            if re.fullmatch(r'\d{8}', text):
                datetime.strptime(text, '%Y%m%d')
                return
            date.fromisoformat(text)
            return
        if kind == 'datetime':
            if isinstance(value, (date, datetime)):
                return
            text = str(value).strip()
            if re.fullmatch(r'\d{8}', text):
                datetime.strptime(text, '%Y%m%d')
                return
            if re.fullmatch(r'\d{14}', text):
                datetime.strptime(text, '%Y%m%d%H%M%S')
                return
            datetime.fromisoformat(text.replace('Z', '+00:00'))
            return
        if kind == 'integer':
            number = Decimal(str(value).strip())
            if number != number.to_integral_value():
                raise ValueError
            return
        if kind == 'number':
            if not math.isfinite(float(value)):
                raise ValueError
            return
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ParameterValidationError(
            f'{name} 参数类型无效，应为 {source_type}'
        ) from exc

    text = str(value).strip()
    if name == 'ticker' and not re.fullmatch(r'\d{6}', text):
        raise ParameterValidationError('ticker 必须是 6 位证券代码')
    if name == 'secID' and not re.fullmatch(r'[A-Za-z0-9]+(?:\.[A-Za-z0-9]+)?', text):
        raise ParameterValidationError('secID 格式无效')


@dataclass(frozen=True)
class EndpointSpec:
    api: str
    path: str
    title: str
    capability: str
    table: Optional[str]
    input_types: Mapping[str, str]
    input_flags: Mapping[str, str]
    output_types: Mapping[str, str]
    output_units: Mapping[str, Optional[str]] = field(default_factory=dict)
    output_unit_status: Mapping[str, str] = field(default_factory=dict)
    required_all: Tuple[str, ...] = field(default_factory=tuple)
    required_any: Tuple[Tuple[str, ...], ...] = field(default_factory=tuple)
    mutually_exclusive: Tuple[Tuple[str, ...], ...] = field(default_factory=tuple)
    bounded_ranges: Tuple[Tuple[str, str], ...] = field(default_factory=tuple)
    business_key_fields: Tuple[str, ...] = field(default_factory=tuple)
    record_key_fields: Tuple[str, ...] = field(default_factory=tuple)
    watermark_field: Optional[str] = None
    warehouse_watermark_field: Optional[str] = None
    incremental_param: Optional[str] = None
    version_order_fields: Tuple[str, ...] = field(default_factory=tuple)
    freshness_hours: int = 24

    @property
    def fields(self) -> Tuple[str, ...]:
        return tuple(self.output_types.keys())

    @property
    def units(self) -> Mapping[str, Optional[str]]:
        """兼容简称；所有输出字段均有键，未知单位为 None。"""
        return self.output_units

    @property
    def unit_statuses(self) -> Mapping[str, str]:
        """每个输出字段的中央单位审核状态。"""
        return self.output_unit_status

    def validate_params(self, params: Mapping[str, Any]) -> Dict[str, Any]:
        unknown = sorted(set(params) - set(self.input_types))
        if unknown:
            raise ParameterValidationError(
                f'{self.api} 不允许参数: {", ".join(unknown)}'
            )
        clean = {k: v for k, v in params.items() if _parameter_present(v)}
        missing = [name for name in self.required_all if name not in clean]
        if missing:
            raise ParameterValidationError(
                f'{self.api} 缺少必填参数: {", ".join(missing)}'
            )
        # 每一个 any-of 组都必须至少命中一个。用于明确 M/S 的实际组合语义。
        for group in self.required_any:
            if not any(name in clean for name in group):
                raise ParameterValidationError(
                    f'{self.api} 参数至少提供一个: {", ".join(group)}'
                )
        for group in self.mutually_exclusive:
            present = [name for name in group if name in clean]
            if len(present) > 1:
                raise ParameterValidationError(
                    f'{self.api} 参数互斥: {", ".join(present)}'
                )
        for begin, end in self.bounded_ranges:
            if begin not in clean or end not in clean:
                raise ParameterValidationError(
                    f'{self.api} 必须提供有界日期范围: {begin}, {end}'
                )
        for name, value in clean.items():
            for scalar in _parameter_values(value):
                _validate_scalar(name, scalar, self.input_types[name])
        return clean


@lru_cache(maxsize=1)
def load_manifest() -> Dict[str, Any]:
    with open(MANIFEST_PATH, 'r', encoding='utf-8') as f:
        manifest = json.load(f)
    if len(manifest.get('endpoints') or {}) != 29:
        raise RuntimeError('Datayes reviewed endpoint manifest must contain exactly 29 APIs')
    return manifest


@lru_cache(maxsize=1)
def _specs() -> Dict[str, EndpointSpec]:
    out: Dict[str, EndpointSpec] = {}
    default_exclusive = load_manifest().get('default_mutually_exclusive') or ()
    default_unit = load_manifest().get('default_output_unit')
    for api, raw in load_manifest()['endpoints'].items():
        out[api] = EndpointSpec(
            api=api,
            path=raw['path'],
            title=raw['title'],
            capability=raw['capability'],
            table=raw.get('table'),
            input_types=dict(raw.get('input_types') or {}),
            input_flags=dict(raw.get('input_flags') or {}),
            output_types=dict(raw.get('output_types') or {}),
            output_units={
                field_name: (raw.get('output_units') or {}).get(field_name, default_unit)
                for field_name in (raw.get('output_types') or {})
            },
            output_unit_status={
                field_name: (raw.get('output_unit_status') or {}).get(
                    field_name, 'unverified'
                )
                for field_name in (raw.get('output_types') or {})
            },
            required_all=tuple(raw.get('required_all') or ()),
            required_any=tuple(tuple(g) for g in (raw.get('required_any') or ())),
            mutually_exclusive=tuple(
                tuple(g) for g in (raw.get('mutually_exclusive', default_exclusive) or ())
            ),
            bounded_ranges=tuple(
                tuple(g) for g in (raw.get('bounded_ranges') or ())
            ),
            business_key_fields=tuple(raw.get('business_key_fields') or ()),
            record_key_fields=tuple(raw.get('record_key_fields') or ()),
            watermark_field=raw.get('watermark_field'),
            warehouse_watermark_field=raw.get('warehouse_watermark_field'),
            incremental_param=raw.get('incremental_param'),
            version_order_fields=tuple(raw.get('version_order_fields') or ()),
            freshness_hours=int(raw.get('freshness_hours') or 24),
        )
    return out


def get_endpoint(api: str) -> EndpointSpec:
    try:
        return _specs()[api]
    except KeyError as exc:
        raise EndpointNotAllowed(f'Datayes endpoint 未经审核: {api}') from exc


def list_endpoints(capability: Optional[str] = None) -> List[EndpointSpec]:
    specs = list(_specs().values())
    if capability:
        specs = [s for s in specs if s.capability == capability]
    return specs


def license_scope() -> str:
    return str(load_manifest().get('license_scope') or 'private_derived_only')


def canonical_type(datayes_type: str) -> str:
    aliases = load_manifest().get('type_aliases') or {}
    normalized = str(datayes_type).strip().lower()
    by_lower = {str(key).strip().lower(): value for key, value in aliases.items()}
    return str(by_lower.get(normalized, 'string'))
