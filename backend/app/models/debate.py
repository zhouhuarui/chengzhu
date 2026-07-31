"""Evidence-grounded fundamental debate contracts.

The contracts in this module deliberately use dataclasses instead of a web
framework specific schema.  They are shared by the deterministic auditor, the
LLM orchestrator and the JSONL artefacts written for a research run.

``Decimal`` values are serialised as strings.  This is intentional: converting
financial facts back to binary floats at the persistence boundary would undo
the normaliser's exact-number guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Dict, Iterable, List, Optional


class ClaimStatus(str, Enum):
    PROPOSED = 'proposed'
    CHALLENGED = 'challenged'
    REVISED = 'revised'
    ACCEPTED = 'accepted'
    DISPUTED = 'disputed'
    WITHDRAWN = 'withdrawn'
    REJECTED = 'rejected'


class ChallengeStatus(str, Enum):
    OPEN = 'open'
    RESOLVED = 'resolved'
    UPHELD = 'upheld'
    DISMISSED = 'dismissed'


class VerdictStatus(str, Enum):
    COMPLETE = 'complete'
    DEGRADED = 'degraded'


def _decimal(value: Any) -> Optional[Decimal]:
    if value is None or value == '':
        return None
    if isinstance(value, Decimal):
        return value
    try:
        parsed = Decimal(str(value).strip().replace(',', ''))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, Enum) else value


def _string_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value] if value.strip() else []
    if not isinstance(value, Iterable) or isinstance(value, (dict, bytes)):
        return [str(value)]
    return [str(item) for item in value if item not in (None, '')]


def _quote_map(value: Any) -> Dict[str, List[str]]:
    if not isinstance(value, dict):
        return {}
    result: Dict[str, List[str]] = {}
    for key, quotes in value.items():
        uid = str(key or '').strip()
        items = list(dict.fromkeys(_string_list(quotes)))
        if uid and items:
            result[uid] = items
    return result


@dataclass
class FinancialFact:
    """A single exact financial observation with comparison metadata."""

    fact_uid: str
    evidence_uid: str
    subject: str
    metric: str
    value: Optional[Decimal]
    unit: str
    currency: str
    period: str
    period_type: str
    accumulation_basis: str
    consolidation_scope: str
    disclosure_time: str
    quality_flags: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.value = _decimal(self.value)
        self.quality_flags = list(dict.fromkeys(_string_list(self.quality_flags)))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'fact_uid': self.fact_uid,
            'evidence_uid': self.evidence_uid,
            'subject': self.subject,
            'metric': self.metric,
            'value': format(self.value, 'f') if self.value is not None else None,
            'unit': self.unit,
            'currency': self.currency,
            'period': self.period,
            'period_type': self.period_type,
            'accumulation_basis': self.accumulation_basis,
            'consolidation_scope': self.consolidation_scope,
            'disclosure_time': self.disclosure_time,
            'quality_flags': list(self.quality_flags),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FinancialFact':
        return cls(
            fact_uid=str(data.get('fact_uid') or ''),
            evidence_uid=str(data.get('evidence_uid') or ''),
            subject=str(data.get('subject') or data.get('symbol') or ''),
            metric=str(data.get('metric') or ''),
            value=data.get('value'),
            unit=str(data.get('unit') or ''),
            currency=str(data.get('currency') or ''),
            period=str(data.get('period') or ''),
            period_type=str(data.get('period_type') or data.get('report_period_type') or ''),
            accumulation_basis=str(data.get('accumulation_basis') or ''),
            consolidation_scope=str(data.get('consolidation_scope') or ''),
            disclosure_time=str(data.get('disclosure_time') or ''),
            quality_flags=_string_list(data.get('quality_flags')),
        )

    @property
    def comparison_signature(self) -> tuple:
        """Fields which must agree before two observations may be compared."""

        return (
            self.metric,
            self.unit,
            self.currency,
            self.period,
            self.period_type,
            self.accumulation_basis,
            self.consolidation_scope,
        )

    def is_comparable_with(self, other: 'FinancialFact') -> bool:
        return self.comparison_signature == other.comparison_signature


@dataclass
class FactAssertion:
    """The exact FinancialFact fields a role says its prose represents."""

    fact_uid: str
    subject: str
    metric: str
    value: Optional[Decimal]
    unit: str
    currency: str
    period: str
    period_type: str
    accumulation_basis: str
    consolidation_scope: str

    def __post_init__(self) -> None:
        self.value = _decimal(self.value)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'fact_uid': self.fact_uid,
            'subject': self.subject,
            'metric': self.metric,
            'value': format(self.value, 'f') if self.value is not None else None,
            'unit': self.unit,
            'currency': self.currency,
            'period': self.period,
            'period_type': self.period_type,
            'accumulation_basis': self.accumulation_basis,
            'consolidation_scope': self.consolidation_scope,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'FactAssertion':
        return cls(
            fact_uid=str(data.get('fact_uid') or ''),
            subject=str(data.get('subject') or ''),
            metric=str(data.get('metric') or ''),
            value=data.get('value'),
            unit=str(data.get('unit') or ''),
            currency=str(data.get('currency') or ''),
            period=str(data.get('period') or ''),
            period_type=str(data.get('period_type') or ''),
            accumulation_basis=str(data.get('accumulation_basis') or ''),
            consolidation_scope=str(data.get('consolidation_scope') or ''),
        )


@dataclass
class ClaimCard:
    claim_id: str
    dimension: str
    assertion: str
    role: str
    round_number: int
    evidence_uids: List[str] = field(default_factory=list)
    fact_uids: List[str] = field(default_factory=list)
    fact_assertions: List[FactAssertion] = field(default_factory=list)
    supporting_quotes: Dict[str, List[str]] = field(default_factory=dict)
    assumptions: List[str] = field(default_factory=list)
    status: ClaimStatus = ClaimStatus.PROPOSED
    parent_claim_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, ClaimStatus):
            try:
                self.status = ClaimStatus(str(self.status))
            except ValueError:
                self.status = ClaimStatus.PROPOSED
        self.evidence_uids = list(dict.fromkeys(_string_list(self.evidence_uids)))
        self.fact_uids = list(dict.fromkeys(_string_list(self.fact_uids)))
        self.fact_assertions = [
            item if isinstance(item, FactAssertion) else FactAssertion.from_dict(item)
            for item in (self.fact_assertions or []) if isinstance(item, (FactAssertion, dict))
        ]
        self.supporting_quotes = _quote_map(self.supporting_quotes)
        self.assumptions = _string_list(self.assumptions)
        try:
            self.round_number = int(self.round_number)
        except (TypeError, ValueError):
            self.round_number = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'claim_id': self.claim_id,
            'parent_claim_id': self.parent_claim_id,
            'dimension': self.dimension,
            'assertion': self.assertion,
            'role': self.role,
            'round': self.round_number,
            'evidence_uids': list(self.evidence_uids),
            'fact_uids': list(self.fact_uids),
            'fact_assertions': [item.to_dict() for item in self.fact_assertions],
            'supporting_quotes': {
                key: list(value) for key, value in self.supporting_quotes.items()
            },
            'assumptions': list(self.assumptions),
            'status': _enum_value(self.status),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], **defaults: Any) -> 'ClaimCard':
        payload = {**defaults, **(data or {})}
        return cls(
            claim_id=str(payload.get('claim_id') or ''),
            parent_claim_id=(
                str(payload.get('parent_claim_id'))
                if payload.get('parent_claim_id') not in (None, '') else None
            ),
            dimension=str(payload.get('dimension') or ''),
            assertion=str(payload.get('assertion') or payload.get('claim') or payload.get('text') or ''),
            role=str(payload.get('role') or ''),
            round_number=payload.get('round', payload.get('round_number', 0)),
            evidence_uids=_string_list(payload.get('evidence_uids') or payload.get('citations')),
            fact_uids=_string_list(payload.get('fact_uids')),
            fact_assertions=payload.get('fact_assertions') or [],
            supporting_quotes=payload.get('supporting_quotes') or payload.get('evidence_quotes') or {},
            assumptions=_string_list(payload.get('assumptions')),
            status=payload.get('status') or ClaimStatus.PROPOSED,
        )


@dataclass
class Challenge:
    challenge_id: str
    target_claim_id: str
    challenge_type: str
    argument: str
    evidence_uids: List[str] = field(default_factory=list)
    fact_uids: List[str] = field(default_factory=list)
    fact_assertions: List[FactAssertion] = field(default_factory=list)
    fact_basis_statement: str = ''
    supporting_quotes: Dict[str, List[str]] = field(default_factory=dict)
    response: str = ''
    response_role: str = ''
    resolution_status: ChallengeStatus = ChallengeStatus.OPEN
    resolution_role: str = ''
    response_claim_id: Optional[str] = None
    role: str = ''
    round_number: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.resolution_status, ChallengeStatus):
            try:
                self.resolution_status = ChallengeStatus(str(self.resolution_status))
            except ValueError:
                self.resolution_status = ChallengeStatus.OPEN
        self.evidence_uids = list(dict.fromkeys(_string_list(self.evidence_uids)))
        self.fact_uids = list(dict.fromkeys(_string_list(self.fact_uids)))
        self.fact_assertions = [
            item if isinstance(item, FactAssertion) else FactAssertion.from_dict(item)
            for item in (self.fact_assertions or []) if isinstance(item, (FactAssertion, dict))
        ]
        self.supporting_quotes = _quote_map(self.supporting_quotes)
        try:
            self.round_number = int(self.round_number)
        except (TypeError, ValueError):
            self.round_number = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            'challenge_id': self.challenge_id,
            'target_claim_id': self.target_claim_id,
            'challenge_type': self.challenge_type,
            'argument': self.argument,
            'evidence_uids': list(self.evidence_uids),
            'fact_uids': list(self.fact_uids),
            'fact_assertions': [item.to_dict() for item in self.fact_assertions],
            'fact_basis_statement': self.fact_basis_statement,
            'supporting_quotes': {
                key: list(value) for key, value in self.supporting_quotes.items()
            },
            'response': self.response,
            'response_role': self.response_role,
            'resolution_status': _enum_value(self.resolution_status),
            'resolution_role': self.resolution_role,
            'response_claim_id': self.response_claim_id,
            'role': self.role,
            'round': self.round_number,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any], **defaults: Any) -> 'Challenge':
        payload = {**defaults, **(data or {})}
        return cls(
            challenge_id=str(payload.get('challenge_id') or ''),
            target_claim_id=str(payload.get('target_claim_id') or ''),
            challenge_type=str(payload.get('challenge_type') or payload.get('type') or 'counterevidence'),
            argument=str(payload.get('argument') or payload.get('challenge') or payload.get('text') or ''),
            evidence_uids=_string_list(payload.get('evidence_uids') or payload.get('citations')),
            fact_uids=_string_list(payload.get('fact_uids')),
            fact_assertions=payload.get('fact_assertions') or [],
            fact_basis_statement=str(payload.get('fact_basis_statement') or ''),
            supporting_quotes=payload.get('supporting_quotes') or payload.get('evidence_quotes') or {},
            response=str(payload.get('response') or ''),
            response_role=str(payload.get('response_role') or ''),
            resolution_status=payload.get('resolution_status') or payload.get('status') or ChallengeStatus.OPEN,
            resolution_role=str(payload.get('resolution_role') or ''),
            response_claim_id=(
                str(payload.get('response_claim_id'))
                if payload.get('response_claim_id') not in (None, '') else None
            ),
            role=str(payload.get('role') or ''),
            round_number=payload.get('round', payload.get('round_number', 0)),
        )


@dataclass
class JudgeScore:
    claim_id: str
    citation_pass: bool
    numeric_pass: bool
    comparability_pass: bool
    currentness_pass: bool
    compliance_pass: bool
    evidence_coverage: float = 0.0
    counterevidence_resilience: float = 0.0
    relevance: float = 0.0
    issues: List[str] = field(default_factory=list)

    @property
    def hard_pass(self) -> bool:
        return all((
            self.citation_pass,
            self.numeric_pass,
            self.comparability_pass,
            self.currentness_pass,
            self.compliance_pass,
        ))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'claim_id': self.claim_id,
            'citation_pass': bool(self.citation_pass),
            'numeric_pass': bool(self.numeric_pass),
            'comparability_pass': bool(self.comparability_pass),
            'currentness_pass': bool(self.currentness_pass),
            'compliance_pass': bool(self.compliance_pass),
            'hard_pass': self.hard_pass,
            'evidence_coverage': float(self.evidence_coverage),
            'counterevidence_resilience': float(self.counterevidence_resilience),
            'relevance': float(self.relevance),
            'issues': list(self.issues),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'JudgeScore':
        return cls(
            claim_id=str(data.get('claim_id') or ''),
            citation_pass=bool(data.get('citation_pass')),
            numeric_pass=bool(data.get('numeric_pass')),
            comparability_pass=bool(data.get('comparability_pass')),
            currentness_pass=bool(data.get('currentness_pass')),
            compliance_pass=bool(data.get('compliance_pass')),
            evidence_coverage=float(data.get('evidence_coverage') or 0),
            counterevidence_resilience=float(data.get('counterevidence_resilience') or 0),
            relevance=float(data.get('relevance') or 0),
            issues=_string_list(data.get('issues')),
        )


@dataclass
class EvidenceRequest:
    dimension: str
    description: str
    reason: str = ''
    requested_fields: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            'dimension': self.dimension,
            'description': self.description,
            'reason': self.reason,
            'requested_fields': list(self.requested_fields),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'EvidenceRequest':
        return cls(
            dimension=str(data.get('dimension') or '全局'),
            description=str(data.get('description') or ''),
            reason=str(data.get('reason') or ''),
            requested_fields=_string_list(data.get('requested_fields')),
        )


@dataclass
class DebateVerdict:
    consensus_facts: List[str] = field(default_factory=list)
    supported_interpretations: List[str] = field(default_factory=list)
    unresolved_disputes: List[str] = field(default_factory=list)
    withdrawn_claims: List[str] = field(default_factory=list)
    evidence_gaps: List[str] = field(default_factory=list)
    evidence_requests: List[EvidenceRequest] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    follow_up_public_items: List[str] = field(default_factory=list)
    accepted_claim_ids: List[str] = field(default_factory=list)
    status: VerdictStatus = VerdictStatus.COMPLETE
    generated_by: str = 'judge'
    degradation_reason: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, VerdictStatus):
            try:
                self.status = VerdictStatus(str(self.status))
            except ValueError:
                self.status = VerdictStatus.DEGRADED
        for name in (
            'consensus_facts', 'supported_interpretations', 'unresolved_disputes',
            'withdrawn_claims', 'evidence_gaps', 'assumptions',
            'follow_up_public_items', 'accepted_claim_ids',
        ):
            setattr(self, name, list(dict.fromkeys(_string_list(getattr(self, name)))))
        self.evidence_requests = [
            item if isinstance(item, EvidenceRequest) else EvidenceRequest.from_dict(item)
            for item in (self.evidence_requests or [])
            if isinstance(item, (EvidenceRequest, dict))
        ]
        if self.evidence_requests:
            # ``evidence_gaps`` is a backwards-compatible rendering only;
            # EvidenceRequest is the authoritative machine-readable contract.
            self.evidence_gaps = list(dict.fromkeys(
                item.description for item in self.evidence_requests if item.description
            ))

    def to_dict(self) -> Dict[str, Any]:
        return {
            'status': _enum_value(self.status),
            'generated_by': self.generated_by,
            'degradation_reason': self.degradation_reason,
            'accepted_claim_ids': list(self.accepted_claim_ids),
            'consensus_facts': list(self.consensus_facts),
            'supported_interpretations': list(self.supported_interpretations),
            'unresolved_disputes': list(self.unresolved_disputes),
            'withdrawn_claims': list(self.withdrawn_claims),
            'evidence_gaps': list(self.evidence_gaps),
            'evidence_requests': [item.to_dict() for item in self.evidence_requests],
            'assumptions': list(self.assumptions),
            'follow_up_public_items': list(self.follow_up_public_items),
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'DebateVerdict':
        return cls(
            consensus_facts=_string_list(data.get('consensus_facts')),
            supported_interpretations=_string_list(data.get('supported_interpretations')),
            unresolved_disputes=_string_list(data.get('unresolved_disputes')),
            withdrawn_claims=_string_list(data.get('withdrawn_claims')),
            evidence_gaps=_string_list(data.get('evidence_gaps')),
            evidence_requests=data.get('evidence_requests') or [],
            assumptions=_string_list(data.get('assumptions')),
            follow_up_public_items=_string_list(data.get('follow_up_public_items')),
            accepted_claim_ids=_string_list(data.get('accepted_claim_ids')),
            status=data.get('status') or VerdictStatus.COMPLETE,
            generated_by=str(data.get('generated_by') or 'judge'),
            degradation_reason=(
                str(data.get('degradation_reason'))
                if data.get('degradation_reason') not in (None, '') else None
            ),
        )


__all__ = [
    'Challenge', 'ChallengeStatus', 'ClaimCard', 'ClaimStatus', 'DebateVerdict',
    'EvidenceRequest', 'FactAssertion', 'FinancialFact', 'JudgeScore', 'VerdictStatus',
]
