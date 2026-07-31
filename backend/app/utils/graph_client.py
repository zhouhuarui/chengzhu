"""图谱客户端：优先 Graphiti+Neo4j，否则本地 JSON 图谱（无 Key/未装 Neo4j 可演示）。"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import Config

_lock = threading.Lock()
_instances: Dict[str, 'GraphClient'] = {}


def _after(value: Optional[str], watermark: Optional[str]) -> bool:
    if not value or not watermark:
        return bool(value)
    try:
        observed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
        lower = datetime.fromisoformat(str(watermark).replace('Z', '+00:00'))
        if observed.tzinfo is None:
            observed = observed.astimezone()
        if lower.tzinfo is None:
            lower = lower.astimezone()
        return observed > lower
    except (TypeError, ValueError):
        return str(value) > str(watermark)


def _local_graph_root() -> str:
    path = os.path.join(Config.UPLOAD_FOLDER, 'graphs')
    os.makedirs(path, exist_ok=True)
    return path


class LocalGraphStore:
    """轻量本地图谱：episodes + 从证据启发式抽取的 nodes/edges。"""

    def __init__(self, group_id: str):
        self.group_id = group_id
        self.path = os.path.join(_local_graph_root(), f'{group_id}.json')
        self.data = self._load()

    def _load(self) -> Dict[str, Any]:
        if os.path.isfile(self.path):
            with open(self.path, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {'episodes': [], 'nodes': {}, 'edges': []}

    def save(self) -> None:
        with open(self.path, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)

    def add_episode(
        self,
        body: str,
        reference_time: Optional[str] = None,
        episode_id: Optional[str] = None,
        meta: Optional[Dict[str, Any]] = None,
    ) -> str:
        eid = episode_id or f'ep_{uuid.uuid4().hex[:10]}'
        incoming_meta = meta or {}
        incoming_business_key = incoming_meta.get('business_key')
        incoming_fingerprint = incoming_meta.get('row_fingerprint')
        # 有业务版本标识时只按 id 或 business_key+fingerprint 去重；两个版本
        # 的可读正文可能相同，但仍必须在时序图谱中分别保留。
        for ep in self.data['episodes']:
            ep_meta = ep.get('meta') or {}
            same_version = (
                incoming_business_key
                and incoming_fingerprint
                and ep_meta.get('business_key') == incoming_business_key
                and ep_meta.get('row_fingerprint') == incoming_fingerprint
            )
            if ep.get('id') == eid or same_version:
                return ep['id']
            if not incoming_business_key and ep.get('body') == body:
                return ep['id']
        ref = reference_time or datetime.now().isoformat(timespec='seconds')
        self.data['episodes'].append({
            'id': eid,
            'body': body,
            'reference_time': ref,
            'meta': incoming_meta,
            'created_at': datetime.now().astimezone().isoformat(timespec='microseconds'),
        })
        self._extract_from_body(body, ref, incoming_meta)
        self.save()
        return eid

    def _extract_from_body(self, body: str, ref: str, meta: Dict[str, Any]) -> None:
        symbol = meta.get('symbol') or ''
        source_type = meta.get('source_type') or 'Disclosure'
        title = meta.get('title') or body.split('\n', 1)[0][:80]
        card_id = meta.get('card_id')
        business_key = meta.get('business_key')
        row_fingerprint = meta.get('row_fingerprint')
        version_time = meta.get('version_time') or ref
        observed_at = meta.get('observed_at') or datetime.now().astimezone().isoformat(timespec='microseconds')
        valid = bool(meta.get('valid', True))
        invalid_at = meta.get('invalid_at')

        company_id = None
        if symbol:
            company_id = f'company_{symbol}'
            self.data['nodes'][company_id] = {
                'id': company_id,
                'name': symbol,
                'type': 'Company',
                'summary': f'标的 {symbol}',
            }

        node_type = {
            'announcement': 'Disclosure',
            'financial_report': 'FinancialMetric',
            'news': 'Event',
            'research_report': 'Opinion',
            'industry_data': 'Industry',
            'web_search': 'Event',
        }.get(source_type, 'Disclosure')

        nid = f'n_{uuid.uuid4().hex[:8]}'
        self.data['nodes'][nid] = {
            'id': nid,
            'name': title[:60],
            'type': node_type,
            'summary': body[:200],
            'card_id': card_id,
            'valid': valid,
            'created_at': ref,
            'invalid_at': invalid_at,
            'business_key': business_key,
            'row_fingerprint': row_fingerprint,
            'version_time': version_time,
            'observed_at': observed_at,
        }
        if company_id:
            self.data['edges'].append({
                'id': f'e_{uuid.uuid4().hex[:8]}',
                'source': company_id,
                'target': nid,
                'name': 'DISCLOSES' if node_type == 'Disclosure' else 'INVOLVES',
                'fact': title[:120],
                'valid': valid,
                'created_at': ref,
                'invalid_at': invalid_at,
                'invalidated_at': meta.get('invalidated_at'),
                'card_id': card_id,
                'business_key': business_key,
                'row_fingerprint': row_fingerprint,
                'dedup_key': business_key,
                'version_time': version_time,
                'observed_at': observed_at,
            })

    def revision_status(
        self,
        business_key: str,
        row_fingerprint: str,
        version_time: str = '',
    ) -> Dict[str, Any]:
        """判断结构化事实是新增、重复、修订还是迟到的历史版本。"""
        records = []
        for edge in self.data['edges']:
            if edge.get('business_key') == business_key:
                records.append(edge)
        # 没有 symbol 的证据不会生成边，仍从 episode 元数据判断。
        if not records:
            for ep in self.data['episodes']:
                meta = ep.get('meta') or {}
                if meta.get('business_key') == business_key:
                    records.append({
                        'row_fingerprint': meta.get('row_fingerprint'),
                        'version_time': meta.get('version_time'),
                        'valid': meta.get('valid', True),
                        'invalid_at': meta.get('invalid_at'),
                    })
        if any(r.get('row_fingerprint') == row_fingerprint for r in records):
            return {'status': 'unchanged'}
        active = [r for r in records if r.get('valid', True) and not r.get('invalid_at')]
        if not active:
            return {'status': 'new'}
        current = max(active, key=lambda r: r.get('version_time') or r.get('created_at') or '')
        current_time = current.get('version_time') or current.get('created_at') or ''
        if version_time and current_time and version_time < current_time:
            return {'status': 'historical', 'current_version_time': current_time}
        return {'status': 'revised', 'current_version_time': current_time}

    def invalidate_revision(
        self,
        business_key: str,
        newer_fingerprint: str,
        invalid_at: str,
        invalidated_at: Optional[str] = None,
    ) -> int:
        """让同一业务键的旧当前版本失效，同时保留历史节点和 episode。"""
        changed_ids = set()
        changed = False
        invalidated_at = invalidated_at or datetime.now().astimezone().isoformat(timespec='microseconds')
        for edge in self.data['edges']:
            if (
                edge.get('business_key') == business_key
                and edge.get('row_fingerprint') != newer_fingerprint
                and edge.get('valid', True)
                and not edge.get('invalid_at')
            ):
                edge['valid'] = False
                edge['invalid_at'] = invalid_at
                edge['invalidated_at'] = invalidated_at
                changed_ids.add(edge.get('target'))
                changed = True
        for node_id in changed_ids:
            node = self.data['nodes'].get(node_id)
            if node:
                node['valid'] = False
                node['invalid_at'] = invalid_at
                node['invalidated_at'] = invalidated_at
        for ep in self.data['episodes']:
            meta = ep.get('meta') or {}
            if (
                meta.get('business_key') == business_key
                and meta.get('row_fingerprint') != newer_fingerprint
                and meta.get('valid', True)
                and not meta.get('invalid_at')
            ):
                meta['valid'] = False
                meta['invalid_at'] = invalid_at
                meta['invalidated_at'] = invalidated_at
                changed = True
        if changed:
            self.save()
        return len(changed_ids)

    def invalidate_stale(self, newer_key: str, newer_time: str) -> int:
        """同 key 的旧边标记 invalid_at。"""
        n = 0
        for edge in self.data['edges']:
            if edge.get('dedup_key') == newer_key and edge.get('valid') and not edge.get('invalid_at'):
                if (edge.get('created_at') or '') < newer_time:
                    edge['valid'] = False
                    edge['invalid_at'] = newer_time
                    n += 1
        if n:
            self.save()
        return n

    def search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        tokens = [t for t in re.split(r'\s+|，|。', query) if t]
        scored = []
        for ep in self.data['episodes']:
            body = ep.get('body') or ''
            score = sum(1 for t in tokens if t in body) if tokens else 1
            if score > 0:
                scored.append((score, {
                    'fact': body[:300],
                    'reference_time': ep.get('reference_time'),
                    'episode_id': ep.get('id'),
                    'meta': ep.get('meta') or {},
                }))
        for edge in self.data['edges']:
            if not edge.get('valid', True):
                continue
            blob = f"{edge.get('fact','')} {edge.get('name','')}"
            score = sum(1 for t in tokens if t in blob) if tokens else 1
            if score > 0:
                scored.append((score, {
                    'fact': edge.get('fact'),
                    'name': edge.get('name'),
                    'created_at': edge.get('created_at'),
                    'card_id': edge.get('card_id'),
                    'valid': True,
                }))
        scored.sort(key=lambda x: -x[0])
        return [x for _, x in scored[:limit]]

    def panorama(self, query: str = '', limit: int = 20, since: Optional[str] = None) -> Dict[str, Any]:
        current, historical = [], []
        for edge in self.data['edges']:
            item = {
                'fact': edge.get('fact'),
                'name': edge.get('name'),
                'created_at': edge.get('created_at'),
                'invalid_at': edge.get('invalid_at'),
                'card_id': edge.get('card_id'),
                'business_key': edge.get('business_key'),
                'row_fingerprint': edge.get('row_fingerprint'),
                'observed_at': edge.get('observed_at'),
                'invalidated_at': edge.get('invalidated_at'),
            }
            if query and query not in (edge.get('fact') or ''):
                continue
            if edge.get('invalid_at'):
                changed_at = edge.get('invalidated_at') or edge.get('invalid_at') or ''
                if since and not _after(changed_at, since):
                    continue
                historical.append(item)
            elif edge.get('valid', True):
                observed_at = edge.get('observed_at') or edge.get('created_at') or ''
                if since and not _after(observed_at, since):
                    continue
                current.append(item)
        current.sort(key=lambda item: item.get('observed_at') or item.get('created_at') or '', reverse=True)
        historical.sort(key=lambda item: item.get('invalidated_at') or item.get('invalid_at') or '', reverse=True)
        return {
            'new_facts': current[:limit],
            'invalidated_facts': historical[:limit],
            'current': current[:limit],
            'historical': historical[:limit],
        }

    def statistics(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for n in self.data['nodes'].values():
            t = n.get('type', 'Unknown')
            by_type[t] = by_type.get(t, 0) + 1
        return {
            'nodes': len(self.data['nodes']),
            'edges': len(self.data['edges']),
            'episodes': len(self.data['episodes']),
            'by_type': by_type,
            'backend': 'local',
        }

    def get_all_nodes(self) -> List[Dict[str, Any]]:
        return list(self.data['nodes'].values())

    def get_all_edges(self) -> List[Dict[str, Any]]:
        return list(self.data['edges'])

    def delete(self) -> None:
        if os.path.isfile(self.path):
            os.remove(self.path)
        self.data = {'episodes': [], 'nodes': {}, 'edges': []}


class GraphClient:
    def __init__(self, group_id: str):
        self.group_id = group_id
        self.backend = 'local'
        self._local = LocalGraphStore(group_id)
        self._neo4j = None
        self._try_init_neo4j()

    def _try_init_neo4j(self) -> None:
        try:
            from .neo4j_store import Neo4jEpisodeStore, neo4j_available
            if neo4j_available():
                self._neo4j = Neo4jEpisodeStore(self.group_id)
                try:
                    self._neo4j.ensure_constraints()
                except Exception:
                    pass
                self.backend = 'neo4j+local'
        except Exception:
            self._neo4j = None

    def add_episode(self, body: str, reference_time: Optional[str] = None,
                    episode_id: Optional[str] = None, meta: Optional[Dict[str, Any]] = None) -> str:
        eid = self._local.add_episode(body, reference_time, episode_id, meta)
        if self._neo4j:
            try:
                self._neo4j.add_episode(eid, body, reference_time or '', meta or {})
            except Exception:
                pass
        return eid

    def quick_search(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        hits = self._local.search(query, limit)
        if self._neo4j and len(hits) < limit:
            try:
                for r in self._neo4j.search(query, limit=limit):
                    hits.append({
                        'fact': r.get('body', '')[:300],
                        'episode_id': r.get('id'),
                        'reference_time': r.get('reference_time'),
                    })
            except Exception:
                pass
        return hits[:limit]

    def revision_status(self, business_key: str, row_fingerprint: str, version_time: str = '') -> Dict[str, Any]:
        return self._local.revision_status(business_key, row_fingerprint, version_time)

    def invalidate_revision(
        self,
        business_key: str,
        newer_fingerprint: str,
        invalid_at: str,
        invalidated_at: Optional[str] = None,
    ) -> int:
        return self._local.invalidate_revision(
            business_key,
            newer_fingerprint,
            invalid_at,
            invalidated_at,
        )

    def panorama(
        self,
        query: str = '',
        limit: int = 20,
        since: Optional[str] = None,
    ) -> Dict[str, Any]:
        return self._local.panorama(query, limit, since=since)

    def get_all_nodes(self) -> List[Dict[str, Any]]:
        return self._local.get_all_nodes()

    def get_all_edges(self) -> List[Dict[str, Any]]:
        return self._local.get_all_edges()

    def get_entity_summary(self, name: str) -> Dict[str, Any]:
        for n in self.get_all_nodes():
            if name in (n.get('name') or ''):
                return n
        hits = self.quick_search(name, 3)
        return {'name': name, 'facts': hits}

    def get_graph_statistics(self) -> Dict[str, Any]:
        stats = self._local.statistics()
        if self._neo4j:
            try:
                nstats = self._neo4j.statistics()
                stats['neo4j_episodes'] = nstats.get('episodes')
                stats['backend'] = self.backend
            except Exception:
                pass
        return stats

    def visualization_payload(self) -> Dict[str, Any]:
        return {
            'nodes': [
                {
                    'id': n['id'],
                    'name': n.get('name'),
                    'type': n.get('type'),
                    'summary': n.get('summary'),
                }
                for n in self.get_all_nodes()
            ],
            'edges': [
                {
                    'source': e.get('source'),
                    'target': e.get('target'),
                    'name': e.get('name'),
                    'fact': e.get('fact'),
                    'valid': e.get('valid', True),
                    'created_at': e.get('created_at'),
                    'invalid_at': e.get('invalid_at'),
                    'business_key': e.get('business_key'),
                    'row_fingerprint': e.get('row_fingerprint'),
                    'observed_at': e.get('observed_at'),
                }
                for e in self.get_all_edges()
            ],
            'statistics': self.get_graph_statistics(),
        }

    def delete_group(self) -> None:
        self._local.delete()
        if self._neo4j:
            try:
                self._neo4j.delete_group()
            except Exception:
                pass
        with _lock:
            _instances.pop(self.group_id, None)


def get_graph_client(group_id: str) -> GraphClient:
    with _lock:
        if group_id not in _instances:
            _instances[group_id] = GraphClient(group_id)
        return _instances[group_id]


def project_group_id(task_id: str) -> str:
    return f'project_{task_id}'


def user_group_id(user_id: str = 'default') -> str:
    return f'user_{user_id}'


def scenario_group_id(scenario_id: str) -> str:
    return f'scenario_{scenario_id}'
