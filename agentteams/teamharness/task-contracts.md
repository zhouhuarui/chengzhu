# TeamHarness task contracts

Render `${PROJECT_ID}` and task variables from the human-confirmed TaskCard. The task keys and dependencies mirror `backend/app/team/contracts.py`: `evidence_debate` creates nine TeamHarness nodes, while `direct` creates seven and the backend records the two analyst tasks as durable `skipped` rows. The Leader attaches the relevant contract before delegation or system execution.

Every result envelope must contain:

```json
{
  "schema_version": "chengzhu.agentteams.result/v1",
  "team_id": "...",
  "run_id": "...",
  "project_id": "...",
  "task_id": "...",
  "task_key": "...",
  "role_id": "...",
  "state_version": 1,
  "status": "completed|blocked|evidence_gap",
  "cutoff": "RFC3339 timestamp",
  "artifacts": [{"id": "...", "path": "...", "sha256": "..."}],
  "evidence_ids": ["..."],
  "gaps": [{"code": "...", "detail": "...", "blocking": true}],
  "trace": {"started_at": "...", "finished_at": "...", "runtime": "copaw", "runtime_name": "QwenPaw", "model": "<role-model-from-MODEL_PROFILES.md>"}
}
```

Every assignment and backend handoff is wrapped in the same durable
`TaskContract`; no free-form handoff payload is accepted by SQLite:

```json
{
  "goal": "...",
  "inputs": [],
  "expected_outputs": [],
  "acceptance_criteria": [],
  "deadline": {"epoch_seconds": 0, "timeout_seconds": 480},
  "budget": {"currency": "CNY", "limit_cny": 0.0},
  "artifact_refs": [],
  "trace_id": "..."
}
```

The field set is exact. `inputs` carry bounded task references, while complete
evidence/report payloads stay behind immutable `artifact_refs`. The dispatch
trace is written back to every persisted node contract before Worker execution;
Matrix is only a mirror of the same contract.

Worker MCP calls use the manifest-registered `chengzhu` server, an AgentTeams-injected per-Worker Consumer credential, and a role-specific Higress route. The service-only upstream token is never present in a Worker. Do not store credentials or private chain-of-thought in envelopes.

## `research-plan` — research-lead

Input: confirmed TaskCard, issuer/cutoff, source/license policy, requester route, output/risk constraints, and current backend state version.

Accept only if the plan defines questions, evidence requirements, two collector scopes, acceptance criteria, cutoff, gaps/escalation, output, and the exact mode-specific graph: nine nodes for `evidence_debate` or seven for `direct`. Require the authenticated `research-lead` route.

## `disclosure-research` — disclosure-researcher

Input: accepted plan, issuer/exchange IDs, filing types, requested financial/disclosure facts, official-source allowlist, and cutoff.

Accept only if every evidence card has an official URL, retrieval time, document SHA-256, page/table locator, parsed fact/unit/period, parser lineage, confidence, and explicit gaps. Bailian use must carry the fields in `collect-disclosures`. Require the authenticated `disclosure-researcher` route.

## `market-context-research` — market-context-researcher

Input: accepted plan, entity aliases/topics, peer set, geography, lookback/cutoff, source policy, entitlement, and metric definitions.

Accept only if events are time-bounded/deduplicated, broker evidence has affirmative entitlement/minimum quotation, industry metrics record definition/denominator/geography/period, and rumors/corrections/contradictions/gaps remain explicit. Require the authenticated `market-context-researcher` route.

## `evidence-freeze` — chengzhu-backend system node

Input: both accepted collector envelopes and expected backend state version.

Execute only through the deterministic backend adapter. Accept only if graph ingestion, financial normalization, immutable artifact manifests, content SHA-256, schema versions, audit event, and new CAS state version succeed atomically/idempotently. `chengzhu-backend` is not a Worker, Team member, Matrix recipient, or permitted Worker header.

Bridge rule: `research-lead` calls its authenticated `freeze_evidence` MCP tool and, only after validating that response, calls TeamHarness `accept_task_result` for this system node. On MCP/CAS/artifact failure it must submit `accepted: false`; no Matrix recipient can complete this task.

## `quality-analysis` — quality-analyst

Input: accepted freeze manifest, plan, frozen evidence/facts, quality rubric, and cutoff.

Accept only if each material quality/moat claim maps to frozen evidence IDs, facts/calculations/assumptions/judgments are separate, dimensions/formulas are reproducible, and counterevidence/gaps/falsifiers are visible. Require the authenticated `quality-analyst` route.

## `growth-analysis` — growth-analyst

Input: accepted freeze manifest, plan, frozen evidence/facts, horizon/scenario policy, and cutoff.

Accept only if historical drivers, forward assumptions, bull/base/bear scenarios, formulas/units/periods, sensitivities, counterevidence, and gap impacts are explicit and evidence-linked. Require the authenticated `growth-analyst` route.

## `evidence-judgement` — evidence-judge

Input: for `evidence_debate`, both accepted analyst artifacts plus the freeze manifest; for `direct`, the freeze manifest and frozen context without analyst artifacts. Both modes also include the plan, gaps, and evidence rubric.

Accept only if every material claim has an `accept|qualify|reject|blocked` ruling, evidence grade, reviewed hashes, contradictions, concise rationale, uncertainty, and required report constraint. Require the authenticated `evidence-judge` route.

## `report-draft` — report-writer

Input: accepted judgement artifact, allowed claim set, frozen references, TaskCard output format, template, and citation/disclosure policy.

Accept only if every material sentence maps to an allowed claim/evidence ID, all qualifications/scenarios/counterevidence/gaps remain visible, restricted text is not reproduced, and the draft/citation map has one exact hash. Require the authenticated `report-writer` route.

## `compliance-review` — compliance-reviewer

Input: accepted report draft/hash, judgement matrix, all frozen evidence, calculations, gaps, audit records, and review policy.

Accept only if the reviewer returns `accept` with zero unresolved critical/high issues, verifies citations/hashes/cutoff/licensing/risk language/calculation samples, and records the exact approved hash. Otherwise return `revise`; the project and Vue publication must remain blocked. Require the authenticated `compliance-reviewer` route.
