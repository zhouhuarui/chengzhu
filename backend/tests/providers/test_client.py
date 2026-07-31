import os
import sys

import pytest
import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.config import Config
from app.providers.datayes.client import DatayesApiClient, _api_value
from app.providers.datayes.errors import (
    AuthenticationError,
    DatayesError,
    ParameterValidationError,
    RateLimitError,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f'HTTP {self.status_code}')


class FakeHttpClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_client(responses, **kwargs):
    http = FakeHttpClient(responses)
    client = DatayesApiClient(
        token=kwargs.pop('token', 'super-secret-token'),
        client=http,
        page_size=kwargs.pop('page_size', 2),
        max_rps=1_000_000,
        retries=kwargs.pop('retries', 2),
        sleep=lambda _: None,
        **kwargs,
    )
    return client, http


def test_client_paginates_and_only_requests_reviewed_fields():
    client, http = make_client([
        FakeResponse({'retCode': 1, 'data': [
            {'ticker': '000001', 'tradeDate': '20260701', 'closePrice': 10},
            {'ticker': '000001', 'tradeDate': '20260702', 'closePrice': 11},
        ]}),
        FakeResponse({'retCode': 1, 'data': [
            {'ticker': '000001', 'tradeDate': '20260703', 'closePrice': 12},
        ]}),
    ])
    rows = client.call('getMktEqud', {'ticker': '000001'})
    assert [row['close_price'] for row in rows] == [10.0, 11.0, 12.0]
    assert [call[1]['params']['pagenum'] for call in http.calls] == [1, 2]
    requested = set(http.calls[0][1]['params']['field'].split(','))
    assert requested == set(getattr(__import__('app.providers.datayes.manifest', fromlist=['get_endpoint']), 'get_endpoint')('getMktEqud').fields)
    assert http.calls[0][1]['headers']['Authorization'] == 'Bearer super-secret-token'


@pytest.mark.parametrize('ret_code', [-5, -7, -16])
def test_client_handles_empty_and_retries_transient_codes(ret_code):
    client, _ = make_client([
        FakeResponse({'retCode': ret_code, 'retMsg': 'temporary'}),
        FakeResponse({'retCode': 1, 'data': []}),
    ])
    assert client.call('getMktEqud', {'ticker': '000001'}) == []

    empty, _ = make_client([FakeResponse({'retCode': -1, 'data': []})])
    assert empty.call('getMktEqud', {'ticker': '000001'}) == []


def test_client_retries_timeout_but_not_permanent_error():
    retrying, http = make_client([
        httpx.ReadTimeout('timed out'),
        FakeResponse({'retCode': 1, 'data': []}),
    ])
    assert retrying.call('getMktEqud', {'ticker': '000001'}) == []
    assert len(http.calls) == 2

    permanent, permanent_http = make_client([
        FakeResponse({'retCode': -99, 'retMsg': 'bad request'}),
        FakeResponse({'retCode': 1, 'data': []}),
    ])
    with pytest.raises(DatayesError):
        permanent.call('getMktEqud', {'ticker': '000001'})
    assert len(permanent_http.calls) == 1


def test_client_maps_retcode_403_and_exhausted_rate_limit():
    forbidden, _ = make_client([FakeResponse({'retCode': 403, 'retMsg': 'forbidden'})])
    with pytest.raises(AuthenticationError):
        forbidden.call('getMktEqud', {'ticker': '000001'})

    limited, _ = make_client([
        FakeResponse({'retCode': -16, 'retMsg': 'too fast'}),
        FakeResponse({'retCode': -16, 'retMsg': 'too fast'}),
    ])
    with pytest.raises(RateLimitError):
        limited.call('getMktEqud', {'ticker': '000001'})


def test_client_redacts_token_from_auth_and_transport_failures():
    token = 'token-must-never-leak'
    client, _ = make_client([FakeResponse({}, status_code=403)], token=token)
    with pytest.raises(AuthenticationError) as exc:
        client.call('getMktEqud', {'ticker': '000001'})
    assert token not in str(exc.value)
    assert 'Authorization' not in str(exc.value)

    broken, _ = make_client([RuntimeError(f'Authorization: Bearer {token}')], token=token, retries=1)
    with pytest.raises(Exception) as exc2:
        broken.call('getMktEqud', {'ticker': '000001'})
    assert token not in str(exc2.value)


def test_client_rejects_unreviewed_output_field_before_network():
    client, http = make_client([])
    with pytest.raises(ParameterValidationError):
        client.call('getMktEqud', {'ticker': '000001'}, fields=['madeUpField'])
    assert http.calls == []


def test_api_datetime_normalization_is_case_and_whitespace_insensitive():
    assert _api_value('2026-07-01 12:34:56', ' Datetime ') == '20260701123456'
    assert _api_value('2026-07-01', ' date ') == '20260701'


@pytest.mark.network
@pytest.mark.skipif(
    not os.environ.get('DATAYES_TOKEN') or not Config.DATAYES_NETWORK_TESTS,
    reason='需要 DATAYES_TOKEN 且 DATAYES_NETWORK_TESTS=true',
)
def test_real_trade_calendar_contract():
    client = DatayesApiClient(token=os.environ['DATAYES_TOKEN'], page_size=10)
    rows = client.call(
        'getTradeCal',
        {'exchangeCD': 'XSHG', 'beginDate': '20260101', 'endDate': '20260105'},
    )
    assert rows
    assert all('calendar_date' in row for row in rows)
