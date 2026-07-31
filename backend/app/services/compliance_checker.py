"""规则级合规与角标校验（零 LLM 成本）。"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Set

COMPLIANCE_BLACKLIST = re.compile(
    r'(?:建议|推荐)\s*(?:投资者|客户|用户|读者|大家)?\s*'
    r'(?:(?:可|可以)?\s*(?:考虑|择机|继续|直接)?\s*)?'
    r'(?:买\s*入|卖\s*出|增\s*持|减\s*持|持\s*有)|'
    r'(?:投资者|客户|用户|读者|大家)\s*(?:(?:可|可以|应当|应该)\s*)?'
    r'(?:(?:考虑|择机|继续|直接)\s*)?'
    r'(?:买\s*入|卖\s*出|增\s*持|减\s*持|持\s*有)|'
    r'(?:应当|应该|可以|可)\s*(?:直接\s*)?(?:买\s*入|买(?=\s*[/／、，。；;]|\s*$)|卖\s*出|增\s*持|减\s*持)|'
    r'(?:强烈|积极|坚定)\s*(?:建议|推荐)?\s*'
    r'(?:买\s*入|卖\s*出|增\s*持|减\s*持)|'
    r'评级\s*(?:为|是|[:：])?\s*[“‘「『《【(]*\s*(?:强烈\s*)?'
    r'(?:买\s*入|卖\s*出|增\s*持|减\s*持|持\s*有)\s*[”’」』》】)]*|'
    r'(?:给出|给予|维持)\s*[^，。；;\n]{0,12}?(?:买\s*入|卖\s*出|增\s*持|减\s*持|持\s*有)\s*[”’」』》】)]*\s*评级|'
    r'[“‘「『《【(]*\s*(?:买\s*入|卖\s*出|增\s*持|减\s*持|持\s*有)\s*[”’」』》】)]*\s*评级|'
    r'逢低\s*买\s*入|'
    r'目标价|必涨|必跌|抄底|建仓|清仓|梭哈|稳赚|翻倍|值得投资|投资价值凸显|建议配置|值得关注|'
    r'股价将|必然导致|应及时\s*(买|卖|减持|加仓)|看\s*多|看\s*空|'
    r'Alpha\s*信号|买\s*入\s*信号|卖\s*出\s*信号|买\s*卖\s*倾向',
    re.IGNORECASE,
)

CITATION_RE = re.compile(r'\[E(\d+)\]')
CHART_RE = re.compile(r'```chart\s*(\{.*?\})\s*```', re.DOTALL)


def check_compliance(text: str) -> List[Dict[str, Any]]:
    issues = []
    for m in COMPLIANCE_BLACKLIST.finditer(text or ''):
        issues.append({
            'quote': m.group(0),
            'type': 'compliance',
            'detail': f'命中合规黑名单：{m.group(0)}',
            'suggestion': '删除或改为中性事实陈述',
        })
    return issues


def check_chart_blocks(
    text: str,
    evidence_blobs: Optional[Dict[int, str]] = None,
) -> List[Dict[str, Any]]:
    """校验 chart 数据块：source_refs 存在，数值可在证据中找到（宽松包含检查）。"""
    issues: List[Dict[str, Any]] = []
    evidence_blobs = evidence_blobs or {}
    for m in CHART_RE.finditer(text or ''):
        try:
            chart = json.loads(m.group(1))
        except Exception:
            issues.append({
                'quote': m.group(0)[:80],
                'type': 'number_error',
                'detail': 'chart 数据块 JSON 无法解析',
                'suggestion': '修正 chart JSON',
            })
            continue
        refs = chart.get('source_refs') or []
        if not refs:
            issues.append({
                'quote': chart.get('title') or 'chart',
                'type': 'no_citation',
                'detail': 'chart 缺少 source_refs',
                'suggestion': '为图表补充证据角标',
            })
            continue
        series_items = chart.get('series')
        if chart.get('values') is not None or not isinstance(series_items, list) or not series_items:
            issues.append({
                'quote': chart.get('title') or 'chart',
                'type': 'chart_schema',
                'detail': 'chart 必须使用 canonical series[].data，禁止顶层 values',
                'suggestion': '将每组数值写入 series 数组并重新执行同口径校验',
            })
            continue
        x_values = chart.get('x') or []
        if any(
            not isinstance(series, dict)
            or not isinstance(series.get('data'), list)
            or len(series.get('data') or []) != len(x_values)
            for series in series_items
        ):
            issues.append({
                'quote': chart.get('title') or 'chart',
                'type': 'chart_schema',
                'detail': 'chart 的 x 与 series[].data 长度不一致或格式无效',
                'suggestion': '修正图表结构后再发布',
            })
            continue
        ref_ids = []
        for r in refs:
            s = str(r).lstrip('E')
            if s.isdigit():
                ref_ids.append(int(s))
        invalid_refs = [eid for eid in ref_ids if eid not in evidence_blobs]
        if invalid_refs or not ref_ids:
            issues.append({
                'quote': chart.get('title') or 'chart',
                'type': 'citation_mismatch',
                'detail': (
                    'chart source_refs 含无效证据：'
                    + ', '.join(f'E{eid}' for eid in invalid_refs)
                    if invalid_refs else 'chart source_refs 格式无效'
                ),
                'suggestion': '只引用当前冻结证据快照中的 E 角标',
            })
            continue
        blob = ' '.join(evidence_blobs.get(i, '') for i in ref_ids)
        for series in series_items:
            for val in series.get('data') or []:
                if val is None:
                    continue
                token = str(val)
                # 宽松：完整值或去掉小数后能在证据中找到
                if blob and token not in blob and token.split('.')[0] not in blob:
                    issues.append({
                        'quote': f'{series.get("name")}:{token}',
                        'type': 'number_error',
                        'detail': f'chart 数值 {token} 未在 source_refs 证据中找到',
                        'suggestion': '核对图表数据与证据卡',
                    })
    return issues


def check_citations(text: str, valid_ids: Set[int], require_any: bool = True) -> List[Dict[str, Any]]:
    issues = []
    ids = [int(x) for x in CITATION_RE.findall(text or '')]
    for eid in ids:
        if eid not in valid_ids:
            issues.append({
                'quote': f'[E{eid}]',
                'type': 'citation_mismatch',
                'detail': f'角标 E{eid} 不在证据索引中',
                'suggestion': '删除无效角标或改引存在的证据',
            })
    if require_any and text and len(text) > 80 and not ids:
        # 启发式：较长正文却无任何角标
        issues.append({
            'quote': (text or '')[:60],
            'type': 'no_citation',
            'detail': '章节正文未见任何 [E{id}] 角标',
            'suggestion': '为事实性句子补充证据角标',
        })
    return issues


def strip_advice_phrases(text: str) -> str:
    """兜底改写：去掉黑名单词。"""
    return COMPLIANCE_BLACKLIST.sub('（已删除不合规表述）', text or '')
