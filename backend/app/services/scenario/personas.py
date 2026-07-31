"""市场角色模板（10§4）。"""

from __future__ import annotations

from typing import Dict, List

ROLE_TEMPLATES: List[Dict] = [
    {
        'role': '机构投资者',
        'share': 0.10,
        'style': '谨慎、看数据、少发声多观察，转发权威信源',
        'actions': {'observe': 0.5, 'repost': 0.3, 'comment': 0.2},
    },
    {
        'role': '卖方分析师',
        'share': 0.10,
        'style': '快速解读、输出观点、互相引用',
        'actions': {'post': 0.5, 'comment': 0.3, 'repost': 0.2},
    },
    {
        'role': '财经媒体',
        'share': 0.13,
        'style': '追热点、放大信息、标题化表达',
        'actions': {'post': 0.6, 'repost': 0.3, 'comment': 0.1},
    },
    {
        'role': '资深个人投资者',
        'share': 0.20,
        'style': '有立场、引用财报细节、与分析师互动',
        'actions': {'comment': 0.4, 'post': 0.3, 'repost': 0.3},
    },
    {
        'role': '普通散户',
        'share': 0.34,
        'style': '情绪化、跟风、易受媒体影响',
        'actions': {'repost': 0.5, 'comment': 0.35, 'post': 0.15},
    },
    {
        'role': '公司IR',
        'share': 0.03,
        'style': '官方口径、澄清与回应',
        'actions': {'post': 0.7, 'comment': 0.3},
    },
    {
        'role': '行业观察者',
        'share': 0.10,
        'style': '供应链/竞品视角发声',
        'actions': {'post': 0.4, 'comment': 0.4, 'repost': 0.2},
    },
]


def instantiate_profiles(scale: int = 30, seed_facts: List[str] = None) -> List[Dict]:
    seed_facts = seed_facts or []
    profiles = []
    idx = 0
    for tmpl in ROLE_TEMPLATES:
        n = max(1, int(round(scale * tmpl['share'])))
        for i in range(n):
            idx += 1
            fact = seed_facts[idx % len(seed_facts)] if seed_facts else ''
            profiles.append({
                'agent_id': f'agent_{idx:03d}',
                'role': tmpl['role'],
                'bio': f'{tmpl["role"]}#{i+1}。{tmpl["style"]}',
                'persona': f'{tmpl["style"]}。关注事实：{fact[:80]}' if fact else tmpl['style'],
                'actions': tmpl['actions'],
            })
            if len(profiles) >= scale:
                return profiles
    return profiles[:scale]
