"""图谱写入/检索冒烟（本地后端）。"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from app.tools.schema import EvidenceCard
from app.utils.graph_client import GraphClient
from app.services.graph_ingest import card_to_episode_text


def test_local_graph_roundtrip():
    gid = 'project_test_roundtrip'
    client = GraphClient(gid)
    client.delete_group()
    client = GraphClient(gid)

    for i in range(10):
        card = EvidenceCard(
            source_type='announcement',
            title=f'测试公告{i} 净利润披露',
            url=f'https://example.com/{i}',
            publish_time=f'2026-01-{i+1:02d}T00:00:00',
            source_name='巨潮',
            symbol='600519',
            excerpt=f'公司披露要点{i}，归母净利润相关',
            structured={'i': i},
            card_id=i + 1,
        )
        body = card_to_episode_text(card)
        client.add_episode(
            body=body,
            reference_time=card.publish_time,
            episode_id=f'test_{i}',
            meta={'symbol': card.symbol, 'source_type': card.source_type, 'title': card.title, 'card_id': card.card_id},
        )

    hits = client.quick_search('净利润', limit=5)
    assert hits, 'quick_search 应命中'
    assert client.quick_search('绝对不存在的关键词XYZ', limit=5) == []
    stats = client.get_graph_statistics()
    assert stats['nodes'] > 0
    assert stats['episodes'] >= 10

    # 写入更新版：标记旧边 invalid（本地实现通过 invalidate_stale）
    client._local.data['edges'][0]['dedup_key'] = 'k1'
    client._local.data['edges'][0]['created_at'] = '2026-01-01T00:00:00'
    client._local.save()
    n = client._local.invalidate_stale('k1', '2026-02-01T00:00:00')
    assert n >= 1
    pan = client.panorama()
    assert pan.get('historical') or pan.get('invalidated_facts')
    client.delete_group()
