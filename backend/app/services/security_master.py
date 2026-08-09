"""Fast, read-only lookup over the local Datayes security master snapshot.

The licensed ``sec_master`` parquet files remain outside Chengzhu's SQLite
database and public demo bundle.  This service loads only the currently listed
mainland equity universe into an immutable in-process snapshot, then performs
all user-query matching in Python.  Consequently, typeahead input is never
interpolated into SQL.
"""

from __future__ import annotations

import json
import re
import threading
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from ..config import Config


_A_SHARE_EXCHANGES = ('XSHG', 'XSHE', 'XBEI')
_MARKET_SUFFIX = {'XSHG': 'SH', 'XSHE': 'SZ', 'XBEI': 'BJ'}
_SIX_DIGIT_CODE = re.compile(r'^\d{6}$')
_SEC_ID = re.compile(r'^\d{6}\.(?:XSHG|XSHE|XBEI)$')
_MAX_QUERY_LENGTH = 64
_MAX_RESULTS = 20


class SecurityMasterUnavailableError(RuntimeError):
    """The configured local security master cannot currently be read."""


class SecurityIdentityMismatchError(ValueError):
    """Two exact security identifiers resolve to different instruments."""


class SecurityIdentityAmbiguousError(ValueError):
    """An exact name maps to more than one active instrument."""


def _text(value: object) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def _code_term(value: object, *, exact: bool) -> str:
    """Normalize common market-qualified code forms without guessing digits."""

    text = _text(value).upper()
    for prefix in ('SH', 'SZ', 'BJ'):
        if text.startswith(prefix):
            text = text[len(prefix):]
            break
    for suffix in ('.SH', '.SZ', '.BJ', '.XSHG', '.XSHE', '.XBEI'):
        if text.endswith(suffix):
            text = text[:-len(suffix)]
            break
    if not text.isdigit():
        return ''
    if exact and not _SIX_DIGIT_CODE.fullmatch(text):
        return ''
    return text


def _bounded_limit(value: object) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = 10
    return max(1, min(parsed, _MAX_RESULTS))


@dataclass(frozen=True)
class _SecurityRecord:
    sec_id: str
    code: str
    name: str
    cn_spell: str
    exchange: str
    list_status: str

    def public(self) -> Dict[str, str]:
        market = _MARKET_SUFFIX[self.exchange]
        return {
            'sec_id': self.sec_id,
            'code': self.code,
            'name': self.name,
            'exchange': self.exchange,
            'market': market,
            'market_symbol': f'{self.code}.{market}',
            'list_status': self.list_status,
        }


@dataclass(frozen=True)
class _Snapshot:
    signature: Tuple[Tuple[str, int, int], ...]
    records: Tuple[_SecurityRecord, ...]
    by_code: Mapping[str, _SecurityRecord]
    by_sec_id: Mapping[str, _SecurityRecord]
    by_name: Mapping[str, Tuple[_SecurityRecord, ...]]
    as_of: Optional[str]


class SecurityMaster:
    """Thread-safe active A-share lookup backed by local parquet files."""

    max_query_length = _MAX_QUERY_LENGTH
    max_results = _MAX_RESULTS

    def __init__(self, data_dir: Optional[str] = None):
        configured = Config.DATAYES_DATA_DIR if data_dir is None else data_dir
        self.data_dir = str(configured or '').strip()
        self._lock = threading.RLock()
        self._snapshot: Optional[_Snapshot] = None
        self._last_refresh_error: Optional[str] = None

    @property
    def last_refresh_error(self) -> Optional[str]:
        return self._last_refresh_error

    @property
    def as_of(self) -> Optional[str]:
        return self._get_snapshot().as_of

    @property
    def size(self) -> int:
        return len(self._get_snapshot().records)

    def _master_dir(self) -> Optional[Path]:
        if not self.data_dir:
            return None
        return Path(self.data_dir).expanduser().resolve() / 'sec_master'

    def _source_files(self) -> Tuple[Path, ...]:
        folder = self._master_dir()
        if folder is None or not folder.is_dir():
            return ()
        return tuple(sorted(
            (path for path in folder.rglob('*.parquet') if path.is_file()),
            key=lambda path: str(path),
        ))

    @staticmethod
    def _signature(files: Iterable[Path]) -> Tuple[Tuple[str, int, int], ...]:
        signature = []
        for path in files:
            stat = path.stat()
            signature.append((str(path), int(stat.st_mtime_ns), int(stat.st_size)))
        return tuple(signature)

    def _read_as_of(self, files: Sequence[Path]) -> Optional[str]:
        if self.data_dir:
            status_path = Path(self.data_dir).expanduser().resolve() / '_status.json'
            try:
                payload = json.loads(status_path.read_text(encoding='utf-8'))
                value = (payload.get('sec_master') or {}).get('last_run')
                if value:
                    return str(value)
            except (OSError, TypeError, ValueError):
                pass
        if not files:
            return None
        stamp = max(path.stat().st_mtime for path in files)
        return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec='seconds')

    def _load_snapshot(
        self,
        files: Sequence[Path],
        signature: Tuple[Tuple[str, int, int], ...],
    ) -> _Snapshot:
        if not files:
            raise SecurityMasterUnavailableError('本地证券主表未配置或不存在')
        try:
            import duckdb
        except ImportError as exc:
            raise SecurityMasterUnavailableError('duckdb 未安装') from exc

        connection = duckdb.connect(database=':memory:')
        try:
            rows = connection.execute(
                """
                SELECT sec_id, ticker, sec_short_name, cn_spell,
                       exchange_cd, list_status_cd
                FROM read_parquet(?, union_by_name=true, hive_partitioning=true)
                WHERE asset_class = ?
                  AND exchange_cd IN (?, ?, ?)
                  AND list_status_cd = ?
                  AND regexp_full_match(ticker, '^[0-9]{6}$')
                ORDER BY ticker, sec_id
                """,
                [
                    [str(path) for path in files],
                    'E',
                    *_A_SHARE_EXCHANGES,
                    'L',
                ],
            ).fetchall()
        except Exception as exc:
            raise SecurityMasterUnavailableError(
                f'本地证券主表读取失败: {type(exc).__name__}'
            ) from exc
        finally:
            connection.close()

        records: List[_SecurityRecord] = []
        by_code: Dict[str, _SecurityRecord] = {}
        by_sec_id: Dict[str, _SecurityRecord] = {}
        names: Dict[str, List[_SecurityRecord]] = {}
        for raw in rows:
            sec_id = _text(raw[0]).upper()
            code = _text(raw[1])
            name = _text(raw[2])
            spell = _text(raw[3]).upper()
            exchange = _text(raw[4]).upper()
            status = _text(raw[5]).upper()
            if (
                not _SEC_ID.fullmatch(sec_id)
                or not _SIX_DIGIT_CODE.fullmatch(code)
                or not name
                or exchange not in _A_SHARE_EXCHANGES
                or status != 'L'
            ):
                continue
            record = _SecurityRecord(
                sec_id=sec_id,
                code=code,
                name=name,
                cn_spell=spell,
                exchange=exchange,
                list_status=status,
            )
            if code in by_code and by_code[code].sec_id != sec_id:
                raise SecurityMasterUnavailableError(f'证券代码重复: {code}')
            if sec_id in by_sec_id:
                raise SecurityMasterUnavailableError(f'证券标识重复: {sec_id}')
            records.append(record)
            by_code[code] = record
            by_sec_id[sec_id] = record
            names.setdefault(name.casefold(), []).append(record)

        if not records:
            raise SecurityMasterUnavailableError('本地证券主表没有有效的在市 A 股')
        by_name = {
            name: tuple(items)
            for name, items in names.items()
        }
        return _Snapshot(
            signature=signature,
            records=tuple(records),
            by_code=MappingProxyType(by_code),
            by_sec_id=MappingProxyType(by_sec_id),
            by_name=MappingProxyType(by_name),
            as_of=self._read_as_of(files),
        )

    def _get_snapshot(self) -> _Snapshot:
        try:
            files = self._source_files()
            signature = self._signature(files)
        except OSError as exc:
            if self._snapshot is not None:
                self._last_refresh_error = type(exc).__name__
                return self._snapshot
            raise SecurityMasterUnavailableError('本地证券主表不可访问') from exc

        current = self._snapshot
        if current is not None and current.signature == signature:
            return current
        with self._lock:
            current = self._snapshot
            if current is not None and current.signature == signature:
                return current
            try:
                loaded = self._load_snapshot(files, signature)
                # If an ETL replacement raced this read, retry once against the
                # completed file set instead of publishing a mixed snapshot.
                next_files = self._source_files()
                next_signature = self._signature(next_files)
                if next_signature != signature:
                    loaded = self._load_snapshot(next_files, next_signature)
                self._snapshot = loaded
                self._last_refresh_error = None
                return loaded
            except (OSError, SecurityMasterUnavailableError) as exc:
                self._last_refresh_error = type(exc).__name__
                if current is not None:
                    return current
                if isinstance(exc, SecurityMasterUnavailableError):
                    raise
                raise SecurityMasterUnavailableError('本地证券主表刷新失败') from exc

    @staticmethod
    def _one_name(
        snapshot: _Snapshot,
        name: object,
    ) -> Optional[_SecurityRecord]:
        normalized = _text(name).casefold()
        if not normalized:
            return None
        records = snapshot.by_name.get(normalized) or ()
        if len(records) > 1:
            raise SecurityIdentityAmbiguousError(f'证券简称不唯一: {_text(name)}')
        return records[0] if records else None

    def get_by_code(self, code: object) -> Optional[Dict[str, str]]:
        normalized = _code_term(code, exact=True)
        if not normalized:
            return None
        record = self._get_snapshot().by_code.get(normalized)
        return record.public() if record else None

    def get_by_sec_id(self, sec_id: object) -> Optional[Dict[str, str]]:
        normalized = _text(sec_id).upper()
        if not _SEC_ID.fullmatch(normalized):
            return None
        record = self._get_snapshot().by_sec_id.get(normalized)
        return record.public() if record else None

    def get_by_name(self, name: object) -> Optional[Dict[str, str]]:
        record = self._one_name(self._get_snapshot(), name)
        return record.public() if record else None

    def resolve_exact(
        self,
        *,
        code: object = None,
        name: object = None,
        sec_id: object = None,
    ) -> Optional[Dict[str, str]]:
        """Resolve exact identifiers and reject, rather than hide, mismatch."""

        supplied = [value for value in (code, name, sec_id) if _text(value)]
        if not supplied:
            return None
        snapshot = self._get_snapshot()
        resolved: List[Optional[_SecurityRecord]] = []
        if _text(code):
            normalized_code = _code_term(code, exact=True)
            resolved.append(snapshot.by_code.get(normalized_code) if normalized_code else None)
        if _text(name):
            resolved.append(self._one_name(snapshot, name))
        if _text(sec_id):
            normalized_sec_id = _text(sec_id).upper()
            resolved.append(
                snapshot.by_sec_id.get(normalized_sec_id)
                if _SEC_ID.fullmatch(normalized_sec_id) else None
            )
        found = [record for record in resolved if record is not None]
        if not found:
            return None
        if len(found) != len(resolved) or len({record.sec_id for record in found}) != 1:
            raise SecurityIdentityMismatchError('证券代码、简称或市场标识不匹配')
        return found[0].public()

    def search(self, query: object, limit: object = 10) -> List[Dict[str, str]]:
        normalized = _text(query)
        if not normalized:
            return []
        if len(normalized) > self.max_query_length:
            raise ValueError(f'搜索词不能超过 {self.max_query_length} 个字符')
        snapshot = self._get_snapshot()
        code_query = _code_term(normalized, exact=False)
        folded = normalized.casefold()
        spell_query = normalized.upper()
        matches = []
        for record in snapshot.records:
            name_folded = record.name.casefold()
            if code_query and record.code == code_query:
                rank = 0
            elif name_folded == folded:
                rank = 1
            elif record.cn_spell and record.cn_spell == spell_query:
                rank = 2
            elif code_query and record.code.startswith(code_query):
                rank = 3
            elif name_folded.startswith(folded):
                rank = 4
            elif record.cn_spell and record.cn_spell.startswith(spell_query):
                rank = 5
            elif folded in name_folded:
                rank = 6
            else:
                continue
            matches.append((rank, len(record.name), record.code, record))
        matches.sort(key=lambda item: item[:3])
        return [item[3].public() for item in matches[:_bounded_limit(limit)]]

    def find_mentions(self, text: object, limit: object = 5) -> List[Dict[str, str]]:
        """Find full active-security names in text, preferring longest overlaps."""

        normalized = _text(text)
        if not normalized:
            return []
        snapshot = self._get_snapshot()
        candidates = []
        for record in snapshot.records:
            start = normalized.find(record.name)
            if start >= 0:
                candidates.append((
                    -len(record.name),
                    start,
                    start + len(record.name),
                    record.code,
                    record,
                ))
        candidates.sort(key=lambda item: item[:4])
        accepted = []
        occupied: List[Tuple[int, int]] = []
        seen = set()
        for _, start, end, _, record in candidates:
            if record.sec_id in seen:
                continue
            if any(start < used_end and end > used_start for used_start, used_end in occupied):
                continue
            accepted.append(record.public())
            occupied.append((start, end))
            seen.add(record.sec_id)
            if len(accepted) >= _bounded_limit(limit):
                break
        return accepted


_service: Optional[SecurityMaster] = None
_service_lock = threading.Lock()


def get_security_master() -> SecurityMaster:
    """Return the process singleton, rebuilding it when the configured path changes."""

    global _service
    configured = str(Config.DATAYES_DATA_DIR or '').strip()
    if _service is None or _service.data_dir != configured:
        with _service_lock:
            if _service is None or _service.data_dir != configured:
                _service = SecurityMaster(configured)
    return _service


def reset_security_master() -> None:
    """Drop the process cache (primarily useful for isolated tests)."""

    global _service
    with _service_lock:
        _service = None


def get_security_by_code(code: object) -> Optional[Dict[str, str]]:
    return get_security_master().get_by_code(code)


def get_security_by_name(name: object) -> Optional[Dict[str, str]]:
    return get_security_master().get_by_name(name)


def resolve_exact(
    *,
    code: object = None,
    name: object = None,
    sec_id: object = None,
) -> Optional[Dict[str, str]]:
    return get_security_master().resolve_exact(code=code, name=name, sec_id=sec_id)


def find_mentions(text: object, limit: object = 5) -> List[Dict[str, str]]:
    return get_security_master().find_mentions(text, limit=limit)
