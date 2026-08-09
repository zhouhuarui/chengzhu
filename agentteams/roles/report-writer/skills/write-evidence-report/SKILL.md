---
name: write-evidence-report
description: Draft a traceable Chengzhu research report from the accepted evidence-judgement artifact and its allowed claim set. Use when TeamHarness releases report-draft to map claims to sections and citations, preserve qualifications and uncertainty, and create one exact hash for independent compliance review.
---

# Write Evidence Report

## 触发条件

- Use only after `evidence-judgement` is accepted and TeamHarness releases `report-draft` to `report-writer`.
- Stop when the allowed claim set, judgement hash, template, cutoff, citation policy, or required disclosure is missing or mismatched.

## 输入

Require task/run/team IDs, backend state version, accepted judgement hash/matrix, allowed claim set, frozen evidence references, confirmed TaskCard/output format, template/schema version, citation/quotation policy, and disclosure rules. Use only the manifest-registered `chengzhu` MCP. AgentTeams injects the per-Worker consumer credential; Higress restricts the route to `worker-report-writer` and binds the upstream role to `report-writer`.

For `evidence_debate`, the write contract is selector-only: each section carries `claim_ids`, and every ID must occur in `verdict.accepted_claim_ids`. Free-form `title`, `summary`, `goal`, and `content` are not an evidence channel and are ignored by the backend. For `direct`, sections continue to use bounded `title`/`goal`/`content` and pass the frozen-evidence relevance gate.

## 输出

Return a versioned backend-rendered report draft, claim-to-section/evidence citation map, reproducible calculation appendix, uncertainty/gap/risk disclosures, AI identity and non-advice notice, template/schema/model/runtime versions, and exact artifact SHA-256. In `evidence_debate`, report prose is made only from durable accepted Claim assertions, their audited assumptions, and frozen EvidenceCard references. If the accepted set is empty, return the backend safe gap version without a factual conclusion.

## 工作流

1. Verify judgement acceptance, exact hashes, allowed claims, template, cutoff, and role identity.
2. In `evidence_debate`, plan only Claim grouping and submit `{"sections":[{"claim_ids":["accepted-id"]}]}`. Do not draft narrative content; the backend reconstructs the exact accepted assertions, audited assumptions, and frozen citations.
3. Treat an unknown, disputed, rejected, absent, or `hard_pass=false` Claim as a stop condition. The backend rechecks the durable component audit booleans and rejects a mismatched verdict.
4. Omitted accepted Claims are automatically added by the backend. When no Claim is accepted, submit an empty sections list and use the safe evidence-gap result.
5. In `direct`, prepare bounded source-grounded sections under the existing content contract; never reuse this path to bypass debate Claim selection.
6. Verify the returned Claim coverage/citation map, source locators, cutoff, disclosures, and exact artifact hash.
7. Freeze the draft/citation map and return the exact hash for `compliance-reviewer`; never self-approve or publish.

## 失败处理

- Do not try to remove, rewrite, or add a sentence in `evidence_debate`; submit Claim selectors only. The backend ignores free-form narrative fields and rebuilds all prose.
- On rejected/unknown Claim ID or durable verdict/audit mismatch, stop and return the offending Claim ID for upstream repair; do not seek new evidence as report writer.
- Preserve contradictory evidence and mandatory uncertainty instead of smoothing the narrative.
- On missing citation/locator, return the claim ID for upstream revision and produce no approval-ready artifact.
- On stale state or MCP role/auth mismatch, stop without mutating backend state.

## 安全边界

- Never hardcode/log the MCP token, put conclusions in free-form title/summary/content fields during `evidence_debate`, add unsupported claims, alter evidence, remove qualifications, exceed quotation rights, self-approve/publish, place trades, personalize advice, or reveal hidden reasoning.
- Call only the `chengzhu` route assigned to `report-writer`; approval applies only to the exact returned hash.

## 复用价值

Reuse the allowed-claim-set input, claim-to-section map, exact-hash handoff, required-disclosure checks, and role-bound MCP contract for other regulated or evidence-constrained reports.
