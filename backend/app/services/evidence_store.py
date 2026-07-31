"""任务证据库：兼容旧 evidence/*.jsonl 与 run 级冻结快照。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional, Union

from ..config import Config
from ..models.research_task import ResearchTask
from ..tools.schema import EvidenceCard


def evidence_uid_for(data: Dict[str, Any]) -> str:
    """生成跨 run 稳定的证据 ID，不依赖文件名或加载顺序。"""
    existing = str(data.get('evidence_uid') or '').strip()
    if existing:
        return existing
    provenance = data.get('provenance') if isinstance(data.get('provenance'), dict) else {}
    structured = data.get('structured') if isinstance(data.get('structured'), dict) else {}
    record_key = provenance.get('record_key') or provenance.get('business_key')
    identity = {
        'source_type': data.get('source_type') or '',
        'provider': provenance.get('provider') or data.get('source_name') or '',
        'record_key': record_key or '',
        'url': data.get('url') or '',
        'publish_time': data.get('publish_time') or '',
        'symbol': data.get('symbol') or '',
        'title': data.get('title') or '',
        # 无 record_key/URL 的结构化数据使用上游行指纹；仍无指纹时
        # 摘录参与身份，避免同标题不同事实冲突。
        'row_fingerprint': provenance.get('row_fingerprint') or structured.get('row_fingerprint') or '',
        'excerpt': '' if record_key or data.get('url') else (data.get('excerpt') or ''),
    }
    raw = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
    return 'ev_' + hashlib.sha256(raw.encode('utf-8')).hexdigest()[:24]


class EvidenceStore:
    def __init__(
        self,
        task_id: str,
        run_id: Optional[str] = None,
        *,
        allow_staging: bool = False,
    ):
        self.task_id = task_id
        self.run_id = run_id
        self.allow_staging = allow_staging
        self.is_frozen = False
        self.cards: List[EvidenceCard] = []
        self._by_id: Dict[int, EvidenceCard] = {}
        self._by_uid: Dict[str, EvidenceCard] = {}
        self._display_ids: Dict[int, str] = {}
        self._index_path: Optional[str] = None
        self._folder = self._resolve_folder(run_id)
        self.load()

    @property
    def legacy_folder(self) -> str:
        return os.path.join(Config.UPLOAD_FOLDER, 'tasks', self.task_id, 'evidence')

    @property
    def folder(self) -> str:
        return self._folder

    def _resolve_folder(self, requested_run_id: Optional[str]) -> str:
        task = ResearchTask.load(self.task_id)
        if requested_run_id:
            if requested_run_id == self.task_id:
                # 过渡期允许显式访问以 task_id 标识的旧产物。
                self.run_id = self.task_id
                return self.legacy_folder
            if not task:
                # API 层会返回任务不存在；服务层仍保持空库语义。
                return os.path.join(Config.UPLOAD_FOLDER, 'tasks', self.task_id, 'runs', '__missing__', 'evidence')
            try:
                run_folder = task.run_folder(requested_run_id)
            except ValueError:
                return os.path.join(task.runs_folder, '__invalid__', 'evidence')
            self._index_path = os.path.join(run_folder, 'evidence_index.json')
            if os.path.isfile(self._index_path) or self.allow_staging:
                return os.path.join(run_folder, 'evidence')
            return os.path.join(run_folder, '__unpublished_evidence__')

        # 无 run_id 保持旧 API 行为：仅当 latest run 已发布证据时切换到它，
        # 否则读取任务根目录的历史证据。
        if task and task.current_run_id:
            try:
                run_folder = task.run_folder(task.current_run_id)
            except ValueError:
                run_folder = ''
            index_path = os.path.join(run_folder, 'evidence_index.json') if run_folder else ''
            run_evidence = os.path.join(run_folder, 'evidence') if run_folder else ''
            if index_path and os.path.isfile(index_path):
                self.run_id = task.current_run_id
                self._index_path = index_path
                return run_evidence
        return self.legacy_folder

    def _append(self, data: Dict[str, Any], display_id: Optional[str] = None) -> None:
        global_id = len(self.cards) + 1
        uid = evidence_uid_for(data)
        card = EvidenceCard(
            source_type=data.get('source_type', ''),
            title=data.get('title', ''),
            url=data.get('url') or None,
            publish_time=data.get('publish_time', ''),
            source_name=data.get('source_name', ''),
            symbol=data.get('symbol'),
            excerpt=data.get('excerpt', ''),
            structured=data.get('structured') or {},
            provenance=data.get('provenance') or None,
            reliability=int(data.get('reliability') or 3),
            fetch_tool=data.get('fetch_tool', ''),
            card_id=global_id,
            evidence_uid=uid,
        )
        self.cards.append(card)
        self._by_id[global_id] = card
        self._by_uid.setdefault(uid, card)
        self._display_ids[global_id] = display_id or f'E{global_id}'

    def _load_index(self, path: str) -> bool:
        if not os.path.isfile(path):
            return False
        with open(path, 'r', encoding='utf-8') as f:
            payload = json.load(f)
        if isinstance(payload, list):
            items = payload
        elif isinstance(payload, dict):
            items = payload.get('items') or payload.get('cards') or payload.get('evidence') or []
        else:
            items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            raw_card = item.get('card') if isinstance(item.get('card'), dict) else item
            data = dict(raw_card)
            if item.get('evidence_uid') and not data.get('evidence_uid'):
                data['evidence_uid'] = item['evidence_uid']
            self._append(data, str(item.get('display_id') or '') or None)
        self.is_frozen = True
        return True

    def load(self) -> None:
        self.cards = []
        self._by_id = {}
        self._by_uid = {}
        self._display_ids = {}
        if self._index_path and self._load_index(self._index_path):
            return
        if not os.path.isdir(self.folder):
            return
        for name in sorted(os.listdir(self.folder)):
            if not name.endswith('.jsonl'):
                continue
            path = os.path.join(self.folder, name)
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    data = json.loads(line)
                    if isinstance(data, dict):
                        self._append(data)

    def get(self, eid: Union[int, str]) -> Optional[EvidenceCard]:
        if isinstance(eid, int):
            return self._by_id.get(eid)
        value = str(eid or '')
        if value in self._by_uid:
            return self._by_uid[value]
        if value.upper().startswith('E') and value[1:].isdigit():
            return self._by_id.get(int(value[1:]))
        if value.isdigit():
            return self._by_id.get(int(value))
        return None

    def display_id(self, card: EvidenceCard) -> str:
        return self._display_ids.get(card.card_id or 0, f'E{card.card_id}')

    def statistics(self) -> Dict[str, Any]:
        by_type: Dict[str, int] = {}
        for card in self.cards:
            by_type[card.source_type] = by_type.get(card.source_type, 0) + 1
        return {
            'total_cards': len(self.cards),
            'by_type': by_type,
            'symbols': sorted({card.symbol for card in self.cards if card.symbol}),
            'run_id': self.run_id,
        }

    def search(self, query: str, limit: int = 10, source_type: Optional[str] = None) -> List[EvidenceCard]:
        tokens = [token for token in re.split(r'\s+|，|。|,|\.|？|\?', query) if token]
        expand = {
            '财务': ['利润', '营收', '净利润', 'income', 'financial', '报表'],
            '指标': ['营收', '净利润', '现金流', 'financial', '报表'],
            '报告期': ['REPORT_DATE', '年度', '半年度', '季度', '报表'],
            '公告': ['announcement', '披露', '董事会'],
            '新闻': ['news', '快讯'],
            '研报': ['research', '评级', '券商'],
        }
        for token in list(tokens):
            tokens.extend(expand.get(token, []))
        # 中文章节名往往没有空格（如“财务表现”），对领域词做子串扩展，
        # 但仍要求扩展词真正命中证据，不会回退到无关卡片。
        for keyword, synonyms in expand.items():
            if keyword in query:
                tokens.append(keyword)
                tokens.extend(synonyms)

        pool = self.cards
        if source_type:
            pool = [card for card in pool if card.source_type == source_type]
        scored = []
        for card in pool:
            blob = (
                f'{card.title}\n{card.excerpt}\n{card.source_type}\n'
                f'{json.dumps(card.structured, ensure_ascii=False)}'
            )
            score = sum(1 for token in tokens if token and token in blob)
            if score > 0:
                scored.append((score, card))
        # 证据检索无命中必须返回空，不得附加无关高可信来源。
        scored.sort(key=lambda item: (-item[0], -(item[1].reliability or 0)))
        return [card for _, card in scored[:limit]]

    def format_cards(self, cards: List[EvidenceCard]) -> str:
        parts = []
        for card in cards:
            provenance = card.to_dict().get('provenance') or {}
            provenance_line = (
                f'\n溯源: {json.dumps(provenance, ensure_ascii=False)[:500]}'
                if provenance else ''
            )
            parts.append(
                f'[{self.display_id(card)}] ({card.source_type}|{card.source_name}|'
                f'{card.publish_time}|{card.symbol})\n'
                f'标题: {card.title}\n'
                f'摘录: {card.excerpt}\n'
                f'结构化: {json.dumps(card.structured, ensure_ascii=False)[:500]}'
                f'{provenance_line}'
            )
        return '\n\n'.join(parts) if parts else '（无匹配证据）'

    def sources_index(self) -> List[Dict[str, Any]]:
        items = []
        for card in self.cards:
            item = card.to_dict()
            item['display_id'] = self.display_id(card)
            items.append(item)
        return items

    def freeze_to_run(self, run_id: str) -> str:
        """原子发布证据快照；已存在的快照永不覆盖。"""
        task = ResearchTask.load(self.task_id)
        if not task:
            raise ValueError('任务不存在')
        run_folder = task.run_folder(run_id)
        if not os.path.isdir(run_folder):
            raise ValueError('run 不存在')
        target = os.path.join(run_folder, 'evidence_index.json')
        payload = {
            'schema_version': 1,
            'task_id': self.task_id,
            'run_id': run_id,
            'created_at': datetime.now().astimezone().isoformat(timespec='seconds'),
            'items': [
                {
                    'evidence_uid': card.evidence_uid,
                    'display_id': self.display_id(card),
                    'card': card.to_dict(),
                }
                for card in self.cards
            ],
        }
        tmp = os.path.join(run_folder, f'.evidence-index-{uuid.uuid4().hex}.tmp')
        try:
            with open(tmp, 'x', encoding='utf-8') as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            # hard-link 只会在 target 不存在时成功，避免竞态覆盖。
            os.link(tmp, target)
        except FileExistsError as exc:
            raise FileExistsError(f'证据快照已冻结: {run_id}') from exc
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
        return target
