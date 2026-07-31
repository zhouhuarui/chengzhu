import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '../..'))

from app.providers.datayes.mappers import (
    announcement_dedup_key,
    map_company_events,
    map_financial_statements,
    map_investor_research,
    map_stock_quote,
    merge_announcement_cards,
)
from app.providers.datayes.normalization import normalize_row
from app.providers.datayes.manifest import get_endpoint
from app.providers.router import ProviderResult
from app.tools.schema import EvidenceCard


def result(api, rows, provider='datayes_api'):
    return ProviderResult(rows=rows, provider=provider, api=api, license_scope='private_derived_only')


def test_financial_mapper_exposes_minimum_fields_and_keeps_parent_variant():
    spec = get_endpoint('getFdmtIS')
    merged = normalize_row(spec, {
        'secID': '000001.XSHE', 'ticker': '000001', 'endDate': '20260630',
        'reportType': 'S1', 'mergedFlag': '1', 'publishDate': '20260801',
        'updateTime': '20260801100000', 'tRevenue': 1000000000,
        'NIncomeAttrP': 100000000, 'basicEPS': 1,
    })
    parent = dict(merged, merged_flag='0', t_revenue=800000000.0)
    card = map_financial_statements(result('getFdmtIS', [merged, parent]), '000001', 'income', 4)[0]
    assert card.url is None
    assert card.structured['TOTAL_OPERATE_INCOME'] == 1000000000.0
    assert card.structured['TOTAL_OPERATE_INCOME_yi'] == 10.0
    assert card.structured['parent_company_statement']['TOTAL_OPERATE_INCOME'] == 800000000.0
    assert 'sec_id' not in card.structured
    assert 'datayes_raw' not in card.structured
    assert card.provenance['business_key']
    assert card.provenance['row_fingerprint']


def test_financial_mapper_selects_newest_version_across_datetime_formats():
    spec = get_endpoint('getFdmtIS')
    older = normalize_row(spec, {
        'secID': '000001.XSHE', 'ticker': '000001', 'endDate': '20260630',
        'reportType': 'S1', 'mergedFlag': '1', 'publishDate': '20260801',
        'updateTime': '20260801100000', 'tRevenue': 100,
    })
    newer = normalize_row(spec, {
        'secID': '000001.XSHE', 'ticker': '000001', 'endDate': '20260630',
        'reportType': 'S1', 'mergedFlag': '1', 'publishDate': '20260801',
        'updateTime': '2026-08-02 10:00:00', 'tRevenue': 110,
    })
    card = map_financial_statements(result('getFdmtIS', [older, newer]), '000001', 'income', 1)[0]
    assert card.structured['TOTAL_OPERATE_INCOME'] == 110.0


def test_company_event_mapper_never_embeds_raw_row():
    spec = get_endpoint('getEquShareBuyback')
    row = normalize_row(spec, {
        'secID': '000001.XSHE', 'ticker': '000001', 'secShortName': '平安银行',
        'publishDate': '20260701', 'buyBackValue': 100000000,
        'buyBackVol': 1000000, 'chgDetl': '回购实施进展', 'updateTime': '20260702',
    })
    card = map_company_events(result('getEquShareBuyback', [row]), '000001')[0]
    assert card.structured['event_type'] == 'buyback'
    assert card.structured['buy_back_value'] == 100000000.0
    assert 'sec_id' not in card.structured
    assert 'update_time' not in card.structured
    assert 'datayes_raw' not in card.to_dict()


def test_company_event_current_version_uses_update_time_before_business_date():
    spec = get_endpoint('getEquMjrCntrPIT')
    older = normalize_row(spec, {
        'ticker': '000001', 'recordID': 'R-1', 'annoID': 'A-1',
        'publishDate': '20260710', 'updateTime': '20260701100000',
        'cntrName': '旧版本合同',
    })
    newer = normalize_row(spec, {
        'ticker': '000001', 'recordID': 'R-1', 'annoID': 'A-1',
        'publishDate': '20260709', 'updateTime': '20260702100000',
        'cntrName': '修订后合同',
    })
    cards = map_company_events(result('getEquMjrCntrPIT', [older, newer]), '000001')
    assert len(cards) == 1
    assert cards[0].structured['cntr_name'] == '修订后合同'
    assert cards[0].structured['revision_count'] == 1


def test_announcement_dedup_uses_shared_anno_id_but_namespaces_record_id():
    common = dict(
        source_type='announcement', title='同一公告', url=None,
        publish_time='2026-07-01', source_name='test', symbol='000001.SZ',
        fetch_tool='fetch_announcements',
    )
    a = EvidenceCard(**common, structured={'announcement_id': 'ANN-1'}, provenance={'api': 'getA'})
    b = EvidenceCard(**common, structured={'announcement_id': 'ANN-1'}, provenance={'api': 'getB'})
    assert announcement_dedup_key(a) == announcement_dedup_key(b)
    assert len(merge_announcement_cards([a], [b])) == 1

    c = EvidenceCard(**common, structured={'record_id': '1'}, provenance={'api': 'getEquBoardPubPIT'})
    d = EvidenceCard(**common, structured={'record_id': '1'}, provenance={'api': 'getEquMjrCntrPIT'})
    assert announcement_dedup_key(c) != announcement_dedup_key(d)


def test_same_announcement_id_aggregates_different_board_items():
    common = dict(
        source_type='announcement', url=None, publish_time='2026-07-01',
        source_name='Datayes', symbol='000001.SZ', fetch_tool='fetch_announcements',
        provenance={'api': 'getEquBoardPubPIT'},
    )
    first = EvidenceCard(
        **common, title='议案一',
        structured={'announcement_id': 'ANN-1', 'record_id': '1', 'event_type': 'board_resolution', 'prop_name': '议案一'},
    )
    second = EvidenceCard(
        **common, title='议案二',
        structured={'announcement_id': 'ANN-1', 'record_id': '2', 'event_type': 'board_resolution', 'prop_name': '议案二'},
    )
    merged = merge_announcement_cards([], [first, second])
    assert len(merged) == 1
    assert len(merged[0].structured['items']) == 2
    assert merged[0].structured['related_titles'] == ['议案一', '议案二']


def test_event_id_namespace_and_same_day_distinct_events_do_not_collide():
    common = dict(
        source_type='announcement', url=None, publish_time='2026-07-01',
        source_name='test', symbol='000001.SZ', fetch_tool='fetch_announcements',
    )
    announcement = EvidenceCard(**common, title='公告', structured={'announcement_id': 'SAME'})
    event = EvidenceCard(**common, title='事件', structured={'event_id': 'SAME'})
    assert announcement_dedup_key(announcement) != announcement_dedup_key(event)

    buyback = EvidenceCard(**common, title='股份回购进展', structured={'event_type': 'buyback'})
    lawsuit = EvidenceCard(**common, title='重大诉讼进展', structured={'event_type': 'lawsuit'})
    assert len(merge_announcement_cards([], [buyback, lawsuit])) == 2


def test_public_and_datayes_no_id_event_share_canonical_fallback_key():
    common = dict(
        source_type='announcement', title='股份回购进展', url=None,
        publish_time='2026-07-01', symbol='000001.SZ',
        fetch_tool='fetch_announcements',
    )
    public = EvidenceCard(
        **common, source_name='巨潮资讯网',
        structured={'category': '股权变动', 'canonical_event_type': 'buyback'},
    )
    datayes = EvidenceCard(
        **common, source_name='Datayes',
        structured={'event_type': 'buyback', 'canonical_event_type': 'buyback'},
    )
    assert announcement_dedup_key(public) == announcement_dedup_key(datayes)
    assert len(merge_announcement_cards([public], [datayes])) == 1


def test_quote_never_uses_raw_prices_as_hfq_return_when_factor_is_missing():
    quotes = result('getMktEqud', [
        {'trade_date': '2026-07-01', 'close_price': 10.0},
        {'trade_date': '2026-07-02', 'close_price': 11.0},
    ], provider='datayes_warehouse')
    factors = result('getMktAdjfAf', [], provider='public_fallback')
    factors.degraded = True
    factors.degradation_reasons = ['datayes_token_missing']
    valuation = result('getMktEqudEvalNew', [], provider='public_fallback')
    card = map_stock_quote('000001', 2, quotes, factors, valuation)[0]
    assert card.structured['return_basis'] == 'hfq_unavailable'
    assert card.structured['range_pct'] is None
    assert all(point['hfq_close'] is None for point in card.structured['series'])
    assert '后复权区间涨跌幅不可用' in card.excerpt


def test_quote_rejects_partially_covered_hfq_interval():
    quotes = result('getMktEqud', [
        {'trade_date': '2026-07-01', 'close_price': 10.0},
        {'trade_date': '2026-07-02', 'close_price': 11.0},
    ], provider='datayes_warehouse')
    factors = result('getMktAdjfAf', [
        {'ex_div_date': '2026-07-02', 'accum_adj_factor': 1.1},
    ], provider='datayes_warehouse')
    valuation = result('getMktEqudEvalNew', [], provider='public_fallback')
    card = map_stock_quote('000001', 2, quotes, factors, valuation)[0]
    assert card.structured['return_basis'] == 'hfq_unavailable'
    assert card.structured['range_pct'] is None
    assert all(point['hfq_close'] is None for point in card.structured['series'])


def test_investor_research_is_not_mislabeled_as_broker_research():
    activity_spec = get_endpoint('getEquIsActivity')
    detail_spec = get_endpoint('getEquIsParticipantQa')
    activity = normalize_row(activity_spec, {
        'eventID': 'EVT-1', 'ticker': '000001', 'secShortName': '平安银行',
        'surveyDate': '20260701', 'publishDate': '20260702', 'partyNum': 3,
    })
    detail = normalize_row(detail_spec, {
        'eventID': 'EVT-1', 'ticker': '000001', 'partyName': '某基金',
        'activityType': '电话会议', 'centent': '公司介绍经营情况。',
    })
    cards = map_investor_research(
        result('getEquIsActivity', [activity]),
        {'EVT-1': result('getEquIsParticipantQa', [detail])},
        '000001',
    )
    assert cards[0].source_type == 'investor_research'
    assert cards[0].source_type != 'research_report'
    assert cards[0].structured['event_id'] == 'EVT-1'
    assert cards[0].structured['discussion_available'] is True
    assert 'discussion_excerpt' not in cards[0].structured
    assert '公司介绍经营情况' not in cards[0].excerpt


def test_no_mapper_source_contains_forbidden_raw_row_bucket():
    from pathlib import Path
    source = Path(__file__).parents[2] / 'app/providers/datayes/mappers.py'
    assert 'datayes' + '_raw' not in source.read_text(encoding='utf-8')
