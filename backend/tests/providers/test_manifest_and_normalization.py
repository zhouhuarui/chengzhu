import dataclasses
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.providers.datayes.errors import EndpointNotAllowed, ParameterValidationError
from app.providers.datayes.manifest import canonical_type, get_endpoint, list_endpoints, load_manifest
from app.providers.datayes.normalization import (
    business_key,
    normalize_value,
    normalize_row,
    record_key,
    row_fingerprint,
)


def test_reviewed_manifest_has_exactly_29_self_contained_endpoints():
    specs = list_endpoints()
    assert len(specs) == 29
    assert len({spec.api for spec in specs}) == 29
    assert all(spec.path.startswith('/api/') for spec in specs)
    assert all(spec.input_types and spec.output_types for spec in specs)
    assert all(set(spec.output_units) == set(spec.output_types) for spec in specs)
    assert all(set(spec.output_unit_status) == set(spec.output_types) for spec in specs)
    assert all(
        set(spec.output_unit_status.values()) <= {
            'reviewed', 'unverified', 'not_applicable'
        }
        for spec in specs
    )
    assert get_endpoint('getMktEqud').output_units['closePrice'] == 'CNY/share'
    assert get_endpoint('getTradeCal').output_units['calendarDate'] is None
    assert all(isinstance(spec.mutually_exclusive, tuple) for spec in specs)
    assert all(
        set(spec.required_all) == {name for name, flag in spec.input_flags.items() if flag == 'Y'}
        for spec in specs
    )
    assert load_manifest()['default_mutually_exclusive'] == []


def test_reviewed_manifest_matches_purchased_docs_when_available():
    docs_path = '/Users/zhouhuarui/Projects/Datayes/datayes_api_docs.json'
    if not os.path.isfile(docs_path):
        pytest.skip('开发机未挂载 Datayes 文档；运行时不依赖该文件')
    docs = {item['api']: item for item in json.load(open(docs_path, encoding='utf-8'))['apis']}
    for spec in list_endpoints():
        assert spec.api in docs
        documented_inputs = {item['n'] for item in docs[spec.api]['inputs']}
        documented_outputs = {item['n'] for item in docs[spec.api]['outputs']}
        assert set(spec.input_types) == documented_inputs
        assert set(spec.output_types) <= documented_outputs


def test_runtime_contract_matches_central_chengzhu_manifest_when_available():
    central_path = '/Users/zhouhuarui/Projects/Datayes/docs/chengzhu_api_manifest.json'
    if not os.path.isfile(central_path):
        pytest.skip('未挂载 Datayes 中央 Chengzhu manifest；运行时使用自包含镜像')
    with open(central_path, encoding='utf-8') as stream:
        central_doc = json.load(stream)
    central_apis = {item['api']: item for item in central_doc['apis']}
    runtime_specs = {spec.api: spec for spec in list_endpoints()}
    assert set(runtime_specs) == set(central_apis)

    for api, spec in runtime_specs.items():
        central = central_apis[api]
        assert spec.path == central['path']

        central_inputs = {
            item['name']: item['type'] for item in central['allowed_params']
        }
        assert set(spec.input_types) == set(central_inputs)
        for name, runtime_type in spec.input_types.items():
            assert canonical_type(runtime_type) == canonical_type(central_inputs[name])

        central_outputs = {
            item['name']: item for item in central['output_fields']
        }
        assert set(spec.output_types) <= set(central_outputs)
        for name, runtime_type in spec.output_types.items():
            central_field = central_outputs[name]
            assert canonical_type(runtime_type) == canonical_type(central_field['type'])
            assert spec.output_units[name] == central_field['unit']
            assert spec.output_unit_status[name] == central_field['unit_status']

        assert spec.freshness_hours == central['freshness_hours']


def test_parameter_rules_make_m_s_y_n_semantics_explicit():
    with pytest.raises(ParameterValidationError):
        get_endpoint('getTradeCal').validate_params({'beginDate': '20260101'})
    assert get_endpoint('getTradeCal').validate_params({'exchangeCD': 'XSHG'})
    assert get_endpoint('getMktEqud').validate_params({'ticker': '000001'})
    assert get_endpoint('getEquIsParticipantQa').validate_params({'eventID': 'evt-1'})
    with pytest.raises(ParameterValidationError):
        get_endpoint('getMktEqud').validate_params({'unknown': 'x'})

    exclusive = dataclasses.replace(
        get_endpoint('getMktEqud'),
        mutually_exclusive=(('ticker', 'secID'),),
    )
    with pytest.raises(ParameterValidationError):
        exclusive.validate_params({'ticker': '000001', 'secID': '000001.XSHE'})

    regulatory = get_endpoint('getEquRegulatory')
    assert regulatory.required_all == ()
    assert regulatory.bounded_ranges == (('beginDate', 'endDate'),)
    with pytest.raises(ParameterValidationError):
        regulatory.validate_params({'ticker': '000001'})


def test_parameter_types_dates_and_security_codes_are_validated():
    spec = get_endpoint('getMktEqud')
    assert spec.validate_params({
        'ticker': '000001', 'beginDate': '2026-07-01', 'endDate': '20260727',
    })
    assert spec.validate_params({
        'ticker': ['000001', '600519'], 'tradeDate': '20260727',
    })
    with pytest.raises(ParameterValidationError):
        spec.validate_params({'ticker': '1', 'tradeDate': '20260727'})
    with pytest.raises(ParameterValidationError):
        spec.validate_params({'ticker': '000001', 'tradeDate': '20260230'})
    with pytest.raises(ParameterValidationError):
        get_endpoint('getTradeCal').validate_params({
            'exchangeCD': 'XSHG', 'isOpen': 'not-an-integer',
        })
    with pytest.raises(ParameterValidationError):
        get_endpoint('getFdmtIS').validate_params({
            'ticker': '000001', 'updateTimeBegin': 'not-a-datetime',
        })


def test_unreviewed_endpoint_is_never_callable():
    with pytest.raises(EndpointNotAllowed):
        get_endpoint('getAnythingTheAgentWants')


def test_type_normalization_preserves_codes_and_overrides_datetime():
    spec = get_endpoint('getFdmtIS')
    row = normalize_row(spec, {
        'secID': '000001.XSHE',
        'ticker': '000001',
        'publishDate': '20260701',
        'actPubtime': '2026-07-01 18:30:01',
        'updateTime': '2026-07-01 18:31:01',
        'tRevenue': '123.5',
        'basicEPS': 'N/A',
    })
    assert row['ticker'] == '000001'
    assert row['publish_date'] == '2026-07-01'
    assert row['act_pubtime'] == '2026-07-01T18:30:01+08:00'
    assert row['update_time'] == '2026-07-01T18:31:01+08:00'
    assert row['t_revenue'] == 123.5
    assert row['basic_eps'] is None
    assert canonical_type('Int16') == 'integer'
    assert canonical_type('Text') == 'string'
    assert canonical_type(' Datetime ') == 'datetime'
    assert canonical_type(' int ') == 'integer'
    assert canonical_type(' INT16 ') == 'integer'
    assert canonical_type(' text ') == 'string'


def test_int64_normalization_does_not_round_through_float():
    row = normalize_row(get_endpoint('getSecID'), {
        'secID': '000001.XSHE', 'ticker': '000001',
        'partyID': '9007199254740993',
    })
    assert row['party_id'] == 9007199254740993


def test_date_normalization_rejects_impossible_dates_and_keeps_timezone():
    assert normalize_value('20261340', 'Date') is None
    assert normalize_value('2026-02-30', 'Date') is None
    assert normalize_value('20260230120000', 'Datetime') is None
    assert normalize_value('', 'Datetime') is None
    assert normalize_value('20260701183001999', 'Datetime') == '2026-07-01T18:30:01.999+08:00'
    assert normalize_value('2026-07-01T10:30:01Z', 'Datetime') == '2026-07-01T18:30:01+08:00'


def test_financial_revision_keeps_business_key_and_changes_record_key():
    spec = get_endpoint('getFdmtIS')
    base = normalize_row(spec, {
        'secID': '000001.XSHE', 'ticker': '000001', 'endDate': '20260630',
        'reportType': 'S1', 'mergedFlag': '1', 'publishDate': '20260801',
        'updateTime': '20260801100000', 'tRevenue': 100,
    })
    revision = dict(base, update_time='2026-08-02T10:00:00', t_revenue=110.0)
    assert business_key(spec, base) == business_key(spec, revision)
    assert record_key(spec, base) != record_key(spec, revision)
    assert row_fingerprint(base) != row_fingerprint(revision)


def test_strengthened_business_keys_cover_version_identity():
    assert get_endpoint('getFdmtEfNew').business_key_fields == (
        'api', 'sec_id', 'end_date', 'report_type', 'merged_flag', 'forecast_type'
    )
    assert 'into_date' in get_endpoint('getEquIndustry').business_key_fields
    assert get_endpoint('getEquIsParticipantQa').business_key_fields == (
        'event_id', 'party_name', 'participant_name'
    )
    assert 'assetLiabRatio' not in get_endpoint('getFdmtMainDataQPIT').output_types


def test_missing_business_identifiers_fall_back_to_row_fingerprint():
    spec = get_endpoint('getEquMjrCntrPIT')
    first = normalize_row(spec, {'ticker': '000001', 'cntrName': '合同甲'})
    second = normalize_row(spec, {'ticker': '000001', 'cntrName': '合同乙'})
    assert business_key(spec, first) != business_key(spec, second)


def test_non_migrated_tools_have_no_datayes_dependency():
    from pathlib import Path
    tools_dir = Path(__file__).parents[2] / 'app/tools'
    for name in ('news.py', 'research.py', 'web_search.py', 'read_announcement.py'):
        source = (tools_dir / name).read_text(encoding='utf-8').lower()
        assert 'datayes' not in source
        assert 'app.providers' not in source
