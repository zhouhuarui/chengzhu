"""Datayes 仓库/API 的确定性路由，不依赖工具层实现。"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Mapping, Optional

from ..config import Config
from .datayes.client import DatayesApiClient
from .datayes.errors import DatayesError, WarehouseUnavailableError, redact_secret
from .datayes.manifest import EndpointSpec, get_endpoint, license_scope
from .datayes.normalization import business_key, record_key, row_fingerprint
from .datayes.warehouse import DatayesWarehouse


def _config(name: str, default: Any) -> Any:
    return getattr(Config, name, os.environ.get(name, default))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or '').strip().lower() in {'1', 'true', 'yes', 'on'}


def _shanghai_now() -> str:
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')
    except Exception:
        return datetime.now().astimezone().isoformat(timespec='seconds')


@dataclass
class ProviderResult:
    rows: List[Dict[str, Any]]
    provider: str
    api: str
    degraded: bool = False
    degradation_reasons: List[str] = field(default_factory=list)
    warehouse_watermark: Optional[str] = None
    as_of: str = field(default_factory=_shanghai_now)
    license_scope: str = 'private_derived_only'
    row_sources: Dict[str, str] = field(default_factory=dict, repr=False)

    def provenance_for(
        self,
        row: Mapping[str, Any],
        business_key_value: Optional[str] = None,
        record_key_value: Optional[str] = None,
        upstream_source: Optional[str] = None,
    ) -> Dict[str, Any]:
        spec = get_endpoint(self.api)
        fingerprint = row_fingerprint(row)
        bkey = business_key_value or business_key(spec, row)
        rkey = record_key_value or record_key(spec, row, bkey)
        return {
            'provider': self.row_sources.get(fingerprint, self.provider),
            'api': self.api,
            'record_key': rkey,
            'business_key': bkey,
            'as_of': self.as_of,
            'update_time': row.get('update_time') or row.get('act_pubtime'),
            'warehouse_watermark': self.warehouse_watermark,
            'row_fingerprint': fingerprint,
            'upstream_source': upstream_source,
            'license_scope': self.license_scope,
        }


class ProviderRouter:
    MODES = {'warehouse_then_api', 'warehouse_only', 'api_only'}

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        mode: Optional[str] = None,
        warehouse: Optional[DatayesWarehouse] = None,
        api_client: Optional[DatayesApiClient] = None,
        license_mode: Optional[str] = None,
    ):
        self.enabled = _truthy(_config('DATAYES_ENABLED', False)) if enabled is None else bool(enabled)
        self.mode = str(mode or _config('DATAYES_PROVIDER_MODE', 'warehouse_then_api'))
        if self.mode not in self.MODES:
            self.mode = 'warehouse_then_api'
        data_dir = str(_config('DATAYES_DATA_DIR', '') or '')
        self.warehouse = warehouse or DatayesWarehouse(data_dir)
        self.api_client = api_client or DatayesApiClient(
            token=str(_config('DATAYES_TOKEN', '') or ''),
            base_url=str(_config('DATAYES_BASE_URL', 'https://api.wmcloud.com/data/v1')),
            timeout_seconds=float(_config('DATAYES_TIMEOUT_SECONDS', 30)),
            page_size=int(_config('DATAYES_PAGE_SIZE', 5000)),
            max_rps=float(_config('DATAYES_MAX_RPS', 1)),
            max_concurrency=int(_config('DATAYES_MAX_CONCURRENCY', 2)),
        )
        self.license_mode = str(license_mode or _config('DATAYES_LICENSE_MODE', license_scope()))

    def _result(
        self,
        spec: EndpointSpec,
        rows: Optional[List[Dict[str, Any]]] = None,
        provider: str = 'datayes_warehouse',
        reasons: Optional[List[str]] = None,
        row_sources: Optional[Dict[str, str]] = None,
    ) -> ProviderResult:
        reasons = list(dict.fromkeys(reasons or []))
        return ProviderResult(
            rows=rows or [],
            provider=provider,
            api=spec.api,
            degraded=bool(reasons),
            degradation_reasons=reasons,
            warehouse_watermark=self.warehouse.watermark(spec.api),
            license_scope=self.license_mode,
            row_sources=row_sources or {},
        )

    @staticmethod
    def _requested_end(params: Mapping[str, Any], explicit: Optional[str]) -> Optional[str]:
        if explicit:
            return explicit
        for key in ('endDate', 'tradeDate', 'publishDateEnd', 'updateTimeEnd'):
            if params.get(key):
                return str(params[key])
        return None

    @staticmethod
    def _next_day(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = str(value)[:10]
        if len(text) == 8 and text.isdigit():
            text = f'{text[:4]}-{text[4:6]}-{text[6:]}'
        try:
            return (date.fromisoformat(text) + timedelta(days=1)).strftime('%Y%m%d')
        except ValueError:
            return None

    @staticmethod
    def _overlap_72h(value: Optional[str]) -> Optional[str]:
        if not value:
            return None
        text = str(value).strip()
        try:
            digits = ''.join(ch for ch in text if ch.isdigit())
            if len(digits) >= 14:
                stamp = datetime.strptime(digits[:14], '%Y%m%d%H%M%S')
            elif len(digits) >= 8:
                stamp = datetime.strptime(digits[:8], '%Y%m%d')
            else:
                stamp = datetime.fromisoformat(text.replace('Z', '+00:00'))
            return (stamp - timedelta(hours=72)).strftime('%Y%m%d%H%M%S')
        except (TypeError, ValueError):
            return None

    def _incremental_params(
        self,
        spec: EndpointSpec,
        params: Mapping[str, Any],
        watermark: Optional[str],
    ) -> Dict[str, Any]:
        out = dict(params)
        if not watermark:
            return out
        if spec.incremental_param == 'beginDate' and spec.api == 'getMktEqud':
            begin = self._next_day(watermark)
            if begin:
                out['beginDate'] = begin
                out.pop('tradeDate', None)
        elif spec.incremental_param == 'updateTimeBegin':
            begin = self._overlap_72h(watermark)
            if begin:
                out[spec.incremental_param] = begin
                out.pop('beginDate', None)
        return out

    @staticmethod
    def _merge(spec: EndpointSpec, local: List[Dict[str, Any]], remote: List[Dict[str, Any]]):
        merged: Dict[str, Dict[str, Any]] = {}
        sources: Dict[str, str] = {}
        for source, rows in (('datayes_warehouse', local), ('datayes_api', remote)):
            for row in rows:
                key = record_key(spec, row)
                merged[key] = row
                sources[row_fingerprint(row)] = source
        return list(merged.values()), sources

    def fetch(
        self,
        api: str,
        params: Mapping[str, Any],
        *,
        end_date: Optional[str] = None,
        latest: bool = False,
        limit: int = 5000,
    ) -> ProviderResult:
        spec = get_endpoint(api)
        spec.validate_params(params)
        if not self.enabled:
            return self._result(spec, provider='public_fallback', reasons=['datayes_disabled'])

        requested_end = self._requested_end(params, end_date)
        warehouse_allowed = self.mode in ('warehouse_then_api', 'warehouse_only') and bool(spec.table)
        api_allowed = self.mode in ('warehouse_then_api', 'api_only')
        local: List[Dict[str, Any]] = []
        local_error: Optional[str] = None
        local_complete = False

        if warehouse_allowed and self.warehouse.available(api):
            try:
                local = self.warehouse.query(api, params, limit=limit)
                if requested_end and not latest:
                    local_complete = self.warehouse.covers(api, requested_end)
                elif latest:
                    local_complete = self.warehouse.fresh(api, latest=True)
                else:
                    local_complete = True
            except WarehouseUnavailableError as exc:
                local_error = redact_secret(exc)
        elif warehouse_allowed:
            local_error = 'warehouse_unavailable'

        if local_complete and self.mode != 'api_only':
            sources = {row_fingerprint(row): 'datayes_warehouse' for row in local}
            return self._result(spec, local, 'datayes_warehouse', row_sources=sources)

        if api_allowed and self.api_client.configured:
            try:
                remote_params = self._incremental_params(spec, params, self.warehouse.watermark(api)) if local else dict(params)
                remote = self.api_client.call(api, remote_params)
                rows, sources = self._merge(spec, local, remote)
                # API 成功（即使没有新增记录）即证明仓库在本次 as-of 下没有遗漏。
                provider = 'datayes_api' if remote or not local else 'datayes_warehouse'
                return self._result(spec, rows, provider, row_sources=sources)
            except DatayesError as exc:
                reasons = [f'datayes_api_failed:{redact_secret(exc)}']
                if local_error:
                    reasons.append(local_error)
                if local:
                    reasons.append('warehouse_stale')
                    # latest/超水位请求不能在 API 失败时把旧仓库伪装成最新结果。
                    return self._result(spec, provider='public_fallback', reasons=reasons)
                return self._result(spec, provider='public_fallback', reasons=reasons)

        reasons = []
        if not api_allowed:
            reasons.append('datayes_api_disabled_by_mode')
        elif not self.api_client.configured:
            reasons.append('datayes_token_missing')
        if local_error:
            reasons.append(local_error)
        if local:
            reasons.append('warehouse_stale')
            return self._result(spec, provider='public_fallback', reasons=reasons)
        return self._result(spec, provider='public_fallback', reasons=reasons or ['datayes_unavailable'])


_router: Optional[ProviderRouter] = None
_router_lock = threading.Lock()


def get_provider_router() -> ProviderRouter:
    global _router
    if _router is None:
        with _router_lock:
            if _router is None:
                _router = ProviderRouter()
    return _router


def reset_provider_router() -> None:
    global _router
    with _router_lock:
        if _router is not None:
            try:
                _router.api_client.close()
            except Exception:
                pass
        _router = None
