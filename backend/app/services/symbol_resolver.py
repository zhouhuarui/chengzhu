"""TaskCard 标的与本地 DataYes 证券主表的一致性校验。"""

from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, Iterable, List, Tuple

from ..config import Config
from ..models.task_card import SymbolRef, TaskCard


class SecurityValidationUnavailable(RuntimeError):
    """本地证券主表不可用，不能安全确认任务。"""


def _normal_text(value: Any) -> str:
    return unicodedata.normalize('NFKC', str(value or '')).strip()


def _same_text(left: Any, right: Any) -> bool:
    return _normal_text(left).casefold() == _normal_text(right).casefold()


def _canonical_symbol(item: Dict[str, Any]) -> SymbolRef:
    return SymbolRef(
        code=_normal_text(item.get('code')) or None,
        name=_normal_text(item.get('name')),
        sec_id=_normal_text(item.get('sec_id')) or None,
        exchange=_normal_text(item.get('exchange')),
        market=_normal_text(item.get('market')),
        list_status=_normal_text(item.get('list_status')),
    )


def _security_master():
    try:
        from .security_master import get_security_master

        return get_security_master()
    except Exception as exc:
        # 这里不能静默接受未校验的 code/name 组合；确认接口会转成 503。
        raise SecurityValidationUnavailable('本地证券信息表不可用') from exc


def _lookup(master: Any, symbol: SymbolRef) -> Tuple[Any, Any, Any]:
    code = _normal_text(symbol.code)
    name = _normal_text(symbol.name)
    sec_id = _normal_text(symbol.sec_id)
    try:
        from .security_master import SecurityIdentityAmbiguousError

        by_sec_id = master.get_by_sec_id(sec_id) if sec_id else None
        by_code = master.get_by_code(code) if code else None
        try:
            by_name = master.get_by_name(name) if name else None
        except SecurityIdentityAmbiguousError:
            # 重名不能自动决定，但本地主表本身仍然可用；保留为未解析项。
            by_name = None
    except Exception as exc:
        raise SecurityValidationUnavailable('本地证券信息表查询失败') from exc
    return by_sec_id, by_code, by_name


def _same_security(left: Any, right: Any) -> bool:
    if not left or not right:
        return False
    left_id = _normal_text(left.get('sec_id'))
    right_id = _normal_text(right.get('sec_id'))
    if left_id and right_id:
        return left_id == right_id
    return _normal_text(left.get('code')) == _normal_text(right.get('code'))


def _contains_code(requirement: str, code: str) -> bool:
    return bool(code and re.search(rf'(?<!\d){re.escape(code)}(?!\d)', requirement or ''))


def _append_once(values: List[str], message: str) -> None:
    if message not in values:
        values.append(message)


def _canonical_mentions(master: Any, requirement: str) -> List[SymbolRef]:
    try:
        items = master.find_mentions(requirement or '', limit=5)
    except Exception as exc:
        raise SecurityValidationUnavailable('本地证券信息表查询失败') from exc
    return [_canonical_symbol(item) for item in items]


def reconcile_planned_symbols(card: TaskCard, requirement: str) -> TaskCard:
    """用本地主表补全 Planner 结果；显式冲突保留给用户重新选择。"""

    if not Config.DATAYES_ENABLED:
        return card

    try:
        master = _security_master()
    except SecurityValidationUnavailable:
        _append_once(card.clarifications, '本地证券信息表暂不可用，请在确认页重新选择标的')
        return card

    resolved: List[SymbolRef] = []
    unresolved: List[SymbolRef] = []
    for symbol in card.symbols:
        code = _normal_text(symbol.code)
        name = _normal_text(symbol.name)
        if not code and not name:
            continue

        try:
            by_sec_id, by_code, by_name = _lookup(master, symbol)
        except SecurityValidationUnavailable:
            _append_once(card.clarifications, '本地证券信息表暂不可用，请在确认页重新选择标的')
            return card

        candidates = [item for item in (by_sec_id, by_code, by_name) if item]
        if candidates and all(_same_security(candidates[0], item) for item in candidates[1:]):
            candidate = candidates[0]
            identifiers_match = (
                (not symbol.sec_id or _same_text(symbol.sec_id, candidate.get('sec_id')))
                and (not code or _same_text(code, candidate.get('code')))
                and (not name or _same_text(name, candidate.get('name')))
            )
            if identifiers_match:
                resolved.append(_canonical_symbol(candidate))
                continue

        if by_code and by_name and not _same_security(by_code, by_name):
            code_is_explicit = _contains_code(requirement, code)
            name_is_explicit = bool(name and name in (requirement or ''))
            if name_is_explicit and not code_is_explicit:
                resolved.append(_canonical_symbol(by_name))
                _append_once(card.clarifications, f'已按证券名称将 {name} 匹配为 {by_name["code"]}')
                continue
            if code_is_explicit and not name_is_explicit:
                resolved.append(_canonical_symbol(by_code))
                _append_once(
                    card.clarifications,
                    f'已按证券代码 {code} 规范为 {by_code["name"]}',
                )
                continue
            unresolved.append(SymbolRef(code=code or None, name=name))
            _append_once(
                card.clarifications,
                f'证券名称“{name}”与代码 {code} 不匹配，请重新选择',
            )
            continue

        if by_sec_id or by_code or by_name:
            candidate = by_sec_id or by_code or by_name
            # 一个字段命中、另一个字段未命中时，只有用户原文明确给出的字段可作准。
            if by_name and name in (requirement or '') and not _contains_code(requirement, code):
                resolved.append(_canonical_symbol(by_name))
                continue
            if by_code and _contains_code(requirement, code) and not (name and name in (requirement or '')):
                resolved.append(_canonical_symbol(by_code))
                continue
            if (not code or _same_text(code, candidate.get('code'))) and (
                not name or _same_text(name, candidate.get('name'))
            ):
                resolved.append(_canonical_symbol(candidate))
                continue

        unresolved.append(SymbolRef(code=code or None, name=name))
        label = name or code
        _append_once(card.clarifications, f'未能在证券信息表中确认“{label}”，请重新选择')

    if not resolved and not unresolved:
        try:
            resolved = _canonical_mentions(master, requirement)
        except SecurityValidationUnavailable:
            _append_once(card.clarifications, '本地证券信息表暂不可用，请在确认页重新选择标的')
            return card

    # 同一证券只保留一次，同时保留未解析条目供确认页展示上下文。
    unique: List[SymbolRef] = []
    seen = set()
    for symbol in [*resolved, *unresolved]:
        key = symbol.sec_id or symbol.code or f'name:{symbol.name}'
        if key in seen:
            continue
        seen.add(key)
        unique.append(symbol)
    if unique:
        card.symbols = unique

    if resolved and not unresolved:
        card.clarifications = [
            item for item in card.clarifications
            if '未识别到股票代码或公司简称' not in item
        ]
    return card


def canonicalize_confirmed_symbols(card: TaskCard) -> List[str]:
    """严格校验并原子替换确认卡中的证券；失败时不修改原列表。"""

    if not Config.DATAYES_ENABLED:
        return []

    master = _security_master()
    errors: List[str] = []
    canonical: List[SymbolRef] = []
    seen = set()

    for symbol in card.symbols:
        code = _normal_text(symbol.code)
        name = _normal_text(symbol.name)
        sec_id = _normal_text(symbol.sec_id)
        if not code or not name:
            errors.append('请从证券搜索结果中选择全部标的')
            continue

        by_sec_id, by_code, _ = _lookup(master, symbol)
        item = by_sec_id or by_code
        if not item:
            errors.append(f'证券代码 {code} 不在当前 A 股在市证券表中')
            continue
        canonical_item = _canonical_symbol(item)

        if by_sec_id and by_code and not _same_security(by_sec_id, by_code):
            errors.append(f'证券标识与代码 {code} 不匹配，请重新选择')
            continue
        if sec_id and not _same_text(sec_id, item.get('sec_id')):
            errors.append(f'证券标识与代码 {code} 不匹配，请重新选择')
            continue
        if code != _normal_text(item.get('code')):
            errors.append(f'证券代码 {code} 与所选证券不匹配，请重新选择')
            continue
        if not _same_text(name, item.get('name')):
            errors.append(
                f'证券名称“{name}”与代码 {code} 不匹配（该代码对应“{item.get("name", "")}”）'
            )
            continue

        unique_key = canonical_item.sec_id or canonical_item.code
        if unique_key in seen:
            errors.append(f'请勿重复添加标的 {canonical_item.name}（{canonical_item.code}）')
            continue
        seen.add(unique_key)
        canonical.append(canonical_item)

    if not errors:
        card.symbols = canonical
    return errors


def symbols_are_canonical(symbols: Iterable[SymbolRef]) -> bool:
    """供小范围诊断/测试使用，不触发主表查询。"""

    return all(bool(item.sec_id and item.code and item.name) for item in symbols)
