"""图谱检索的本地替代：在 Neo4j/Graphiti 就绪前，检索任务证据库。"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from .schema import ToolResult
from ..services.evidence_store import EvidenceStore


def graph_quick_search(
    task_id: str,
    query: str,
    limit: int = 10,
    source_type: Optional[str] = None,
    run_id: Optional[str] = None,
) -> ToolResult:
    store = EvidenceStore(task_id, run_id=run_id)
    cards = store.search(query, limit=limit, source_type=source_type)
    graph_bits = []
    # A run must remain bound to its frozen snapshot. The task-level graph can
    # contain facts ingested by older/newer runs, so it is a legacy-only aid.
    if not run_id:
        try:
            from ..utils.graph_client import get_graph_client, project_group_id
            ghits = get_graph_client(project_group_id(task_id)).quick_search(query, limit=min(5, limit))
            for h in ghits:
                graph_bits.append(h.get('fact') or '')
        except Exception:
            pass
    text = store.format_cards(cards)
    if graph_bits:
        text = text + '\n\n## 图谱命中\n' + '\n'.join(f'- {b[:300]}' for b in graph_bits if b)
    return ToolResult(
        ok=True,
        tool='graph_quick_search',
        data={
            'query': query,
            'count': len(cards),
            'text': text,
            'card_ids': [c.card_id for c in cards],
        },
        meta={'backend': 'local_evidence+graph', 'stats': store.statistics()},
    )


def graph_panorama(
    task_id: str,
    query: str = '',
    limit: int = 20,
    run_id: Optional[str] = None,
) -> ToolResult:
    store = EvidenceStore(task_id, run_id=run_id)
    cards = store.search(query or '财务 公告 新闻', limit=limit)
    by_type: Dict[str, List] = {}
    for c in cards:
        by_type.setdefault(c.source_type, []).append(c)
    sections = []
    for st, group in by_type.items():
        sections.append(f'## {st}\n' + store.format_cards(group))
    return ToolResult(
        ok=True,
        tool='graph_panorama',
        data={
            'text': '\n\n'.join(sections),
            'new_facts': store.format_cards(cards[: max(1, limit // 2)]),
            'invalidated_facts': '（本地模式暂无失效边；待 Graphiti 接入）',
            'card_ids': [c.card_id for c in cards],
        },
        meta={'backend': 'local_evidence', 'stats': store.statistics()},
    )


def graph_insight_forge(
    task_id: str,
    question: str,
    run_id: Optional[str] = None,
) -> ToolResult:
    """拆分子问题的简化版：按关键词切分后分别检索再聚合。"""
    store = EvidenceStore(task_id, run_id=run_id)
    sub_qs = [s.strip() for s in question.replace('？', '?').split('?') if s.strip()]
    if len(sub_qs) < 2:
        # 启发式切分
        for sep in ['以及', '和', '与', '、', '，']:
            if sep in question:
                sub_qs = [s.strip() for s in question.split(sep) if s.strip()]
                break
    if not sub_qs:
        sub_qs = [question]
    sub_qs = sub_qs[:5]

    blocks = []
    all_ids = []
    for sq in sub_qs:
        cards = store.search(sq, limit=5)
        all_ids.extend(c.card_id for c in cards)
        blocks.append(f'### 子问题：{sq}\n{store.format_cards(cards)}')

    return ToolResult(
        ok=True,
        tool='graph_insight_forge',
        data={
            'sub_questions': sub_qs,
            'text': '\n\n'.join(blocks),
            'card_ids': list(dict.fromkeys(all_ids)),
        },
        meta={'backend': 'local_evidence'},
    )


def tools_description(frozen: bool = False) -> str:
    description = """
- graph_quick_search(query, limit=10, source_type?): 混合检索证据卡，返回带 [E{id}] 的摘录
- graph_panorama(query?): 按来源类型分栏返回证据全景
- graph_insight_forge(question): 将问题拆成子问题分别检索后聚合
""".strip()
    if not frozen:
        description += '\n- read_announcement(url): 拉取公告全文（如需）'
    return description


def call_analyze_tool(
    task_id: str,
    name: str,
    params: Dict[str, Any],
    run_id: Optional[str] = None,
) -> ToolResult:
    params = dict(params or {})
    if name == 'graph_quick_search':
        return graph_quick_search(task_id, params.get('query', ''), int(params.get('limit', 10)), params.get('source_type'), run_id=run_id)
    if name == 'graph_panorama':
        return graph_panorama(task_id, params.get('query', ''), int(params.get('limit', 20)), run_id=run_id)
    if name == 'graph_insight_forge':
        return graph_insight_forge(task_id, params.get('question') or params.get('query') or '', run_id=run_id)
    if run_id:
        return ToolResult(
            ok=False,
            tool=name,
            error='当前 run 已冻结证据，分析阶段禁止联网补采',
        )
    if name == 'read_announcement':
        from .read_announcement import read_announcement
        return read_announcement(params.get('url', ''), run_id=run_id)
    if name == 'web_search':
        from .web_search import web_search
        return web_search(params.get('query', ''), int(params.get('count', 5)))
    if name == 'fetch_financial_statements':
        from .financial import fetch_financial_statements
        cards = fetch_financial_statements(
            params.get('symbol', ''),
            params.get('statement') or params.get('report_type') or 'income',
            int(params.get('period_count', 4)),
        )
        for i, c in enumerate(cards, 1):
            c.card_id = i
        tmp = EvidenceStore(task_id, run_id=run_id)
        return ToolResult(
            ok=True,
            tool='fetch_financial_statements',
            data={
                'text': tmp.format_cards(cards),
                'cards': [c.to_dict() for c in cards],
            },
        )
    return ToolResult(ok=False, tool=name, error=f'未知分析工具: {name}')
