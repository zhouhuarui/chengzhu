"""受控 Datayes DataAPI 客户端：白名单、分页、限速、重试和脱敏。"""

from __future__ import annotations

import threading
import time
from datetime import date, datetime
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

import httpx

from .errors import (
    AuthenticationError,
    DatayesError,
    ParameterValidationError,
    RateLimitError,
    ServiceUnavailableError,
    redact_secret,
)
from .manifest import EndpointSpec, canonical_type, get_endpoint
from .normalization import normalize_rows


RETRYABLE_CODES = {-5, -7, -16}
AUTH_CODES = {403}


class _Pacer:
    def __init__(self, max_rps: float, clock: Callable[[], float], sleep: Callable[[float], None]):
        self.interval = 1.0 / max(float(max_rps), 0.01)
        self.clock = clock
        self.sleep = sleep
        self._lock = threading.Lock()
        self._last = 0.0

    def wait(self) -> None:
        with self._lock:
            now = self.clock()
            delay = self.interval - (now - self._last)
            if delay > 0:
                self.sleep(delay)
            self._last = self.clock()


def _api_value(value: Any, dtype: str) -> Any:
    if isinstance(value, (list, tuple, set)):
        return ','.join(str(v) for v in value)
    kind = canonical_type(dtype)
    if isinstance(value, datetime):
        return value.strftime('%Y%m%d%H%M%S') if kind == 'datetime' else value.strftime('%Y%m%d')
    if isinstance(value, date):
        return value.strftime('%Y%m%d')
    if kind in ('date', 'datetime'):
        text = str(value).strip()
        digits = ''.join(ch for ch in text if ch.isdigit())
        return digits[:14] if kind == 'datetime' and len(digits) > 8 else digits[:8]
    return value


class DatayesApiClient:
    """只允许调用 reviewed_endpoints.json 中声明的字段和端点。"""

    def __init__(
        self,
        token: Optional[str],
        base_url: str = 'https://api.wmcloud.com/data/v1',
        timeout_seconds: float = 30,
        page_size: int = 5000,
        max_rps: float = 1,
        max_concurrency: int = 2,
        retries: int = 3,
        client: Optional[Any] = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._token = token or ''
        self.base_url = base_url.rstrip('/')
        self.timeout_seconds = float(timeout_seconds)
        self.page_size = max(1, min(int(page_size), 100000))
        self.retries = max(1, int(retries))
        self._sleep = sleep
        self._pacer = _Pacer(max_rps=max_rps, clock=clock, sleep=sleep)
        self._semaphore = threading.BoundedSemaphore(max(1, int(max_concurrency)))
        self._client = client or httpx.Client(timeout=self.timeout_seconds, follow_redirects=False)
        self._owns_client = client is None

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def close(self) -> None:
        if self._owns_client and hasattr(self._client, 'close'):
            self._client.close()

    def _fields(self, spec: EndpointSpec, fields: Optional[Iterable[str]]) -> List[str]:
        selected = list(fields or spec.fields)
        unknown = sorted(set(selected) - set(spec.fields))
        if unknown:
            raise ParameterValidationError(
                f'{spec.api} 未审核输出字段: {", ".join(unknown)}'
            )
        return selected

    def _request_page(self, spec: EndpointSpec, query: Dict[str, Any]) -> List[Mapping[str, Any]]:
        if not self._token:
            raise AuthenticationError('DATAYES_TOKEN 未配置')
        url = f'{self.base_url}{spec.path}.json'
        headers = {'Authorization': f'Bearer {self._token}'}
        last_error: Optional[Exception] = None
        for attempt in range(self.retries):
            try:
                self._pacer.wait()
                with self._semaphore:
                    response = self._client.get(
                        url,
                        params=query,
                        headers=headers,
                        timeout=self.timeout_seconds,
                    )
                if response.status_code in (401, 403):
                    raise AuthenticationError('Datayes 鉴权或接口权限不足')
                if response.status_code == 429:
                    raise RateLimitError('Datayes HTTP 限流')
                response.raise_for_status()
                payload = response.json()
                code_raw = payload.get('retCode')
                try:
                    code = int(code_raw)
                except (TypeError, ValueError):
                    raise ServiceUnavailableError('Datayes 返回了无效 retCode')
                if code == 1:
                    data = payload.get('data') or []
                    if not isinstance(data, list):
                        raise ServiceUnavailableError('Datayes data 不是列表')
                    return data
                if code == -1:
                    return []
                if code in AUTH_CODES:
                    raise AuthenticationError('Datayes 鉴权或接口权限不足')
                if code == -16:
                    last_error = RateLimitError('Datayes 调用频率超限')
                elif code in RETRYABLE_CODES:
                    last_error = ServiceUnavailableError(f'Datayes 暂时不可用 retCode={code}')
                else:
                    msg = redact_secret(payload.get('retMsg') or '', self._token)
                    raise DatayesError(f'Datayes 调用失败 retCode={code}: {msg[:200]}')
            except (AuthenticationError, ParameterValidationError):
                raise
            except (httpx.TimeoutException, httpx.NetworkError, RateLimitError, ServiceUnavailableError) as exc:
                last_error = exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code if exc.response is not None else 0
                last_error = ServiceUnavailableError(f'Datayes HTTP {status}')
            except DatayesError:
                raise
            except Exception as exc:
                # 不向上层透传 request/header repr，避免凭证出现在日志和 tool_call_log。
                last_error = ServiceUnavailableError(
                    redact_secret(f'Datayes 请求失败: {type(exc).__name__}', self._token)
                )
            if attempt < self.retries - 1:
                self._sleep(min(2 ** attempt, 8))
        if isinstance(last_error, RateLimitError):
            raise last_error
        raise ServiceUnavailableError(redact_secret(last_error or 'Datayes 请求失败', self._token))

    def call(
        self,
        api: str,
        params: Mapping[str, Any],
        *,
        fields: Optional[Iterable[str]] = None,
    ) -> List[Dict[str, Any]]:
        spec = get_endpoint(api)
        clean = spec.validate_params(params)
        selected = self._fields(spec, fields)
        base_query = {
            key: _api_value(value, spec.input_types[key])
            for key, value in clean.items()
        }
        base_query['field'] = ','.join(selected)
        rows: List[Mapping[str, Any]] = []
        page = 1
        while True:
            query = dict(base_query)
            query['pagenum'] = page
            query['pagesize'] = self.page_size
            batch = self._request_page(spec, query)
            rows.extend(batch)
            if len(batch) < self.page_size:
                break
            page += 1
            if page > 10000:
                raise ServiceUnavailableError('Datayes 分页超过安全上限')
        return normalize_rows(spec, rows)


# 计划/验收使用的公共命名；保留 Client 名称兼容内部实现。
DatayesApiProvider = DatayesApiClient
