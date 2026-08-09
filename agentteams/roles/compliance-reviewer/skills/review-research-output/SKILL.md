---
name: review-research-output
description: Independently audit a Chengzhu research draft for citation integrity, cutoff and licensing compliance, calculation reproducibility, risk language, gap disclosure, and exact artifact identity. Use as the final TeamHarness gate to return an auditable accept-or-revise decision.
---

# Review Research Output

## 触发条件

- Use only after TeamHarness marks `report-draft` accepted and supplies the frozen draft, adjudicated claim-evidence matrix, upstream artifacts, and audit logs.
- Stop if any input hash is missing/mismatched or if the reviewer participated as analyst for the same artifact.

## 输入

Require the draft and SHA-256, adjudicated claim-evidence matrix, all cited frozen evidence, accepted upstream envelopes, cutoff, entitlement/quotation policy, calculation files, model/runtime/schema versions, gap register, and review rubric version. Use only the manifest-registered `chengzhu` MCP. AgentTeams injects the per-Worker consumer credential; Higress restricts the route to `worker-compliance-reviewer` and binds the upstream role to `compliance-reviewer`.

## 输出

Return `accept` or `revise`, reviewed artifact hash, review timestamp/version, check inventory, issue list with ID/severity/location/evidence/rule/required fix, citation coverage metrics, sampled calculation results, unresolved-gap decision, and release disclosures. Acceptance requires zero unresolved critical or high issues.

## 工作流

1. Verify hashes, TeamHarness dependency state, role independence, and cutoff before reading conclusions.
2. Map every material claim to the claim-evidence matrix and inspect source cards at original page/table locators.
3. Check source authority, quotation/entitlement, time cutoff, contradiction treatment, and prompt-injection resistance.
4. Recalculate a risk-based sample and verify units, periods, currencies, assumptions, and scenario labels.
5. Check that gaps, uncertainty, risk language, AI identity, version metadata, and non-advice boundary are visible.
6. Return `revise` for any critical/high issue. Return `accept` only for the exact reviewed hash and include the release appendix.

## 失败处理

- Treat unavailable cited evidence, hash drift, unsupported material claims, cutoff leakage, licensing breach, or irreproducible key calculations as high or critical.
- Do not repair evidence or rewrite a claim and approve it in the same step. Return issue IDs for a new analyst artifact and review its new hash.
- On reviewer/tool failure, keep the project incomplete and return `blocked`; absence of a review is never approval.
- Preserve prior decisions and link superseding reviews rather than overwriting history.

## 安全边界

- Never fabricate support, weaken severity to meet a deadline, modify frozen evidence, expose credentials, reveal hidden reasoning, or approve a different hash.
- Never request or log gateway credentials or the upstream URL. Call only the `chengzhu` route assigned to `compliance-reviewer`.
- Maintain independence from collection and analysis; escalate conflicts of role or policy.
- Do not place trades or characterize the output as individualized investment advice.

## 复用价值

Reuse the binary gate, severity model, exact-hash approval, claim coverage audit, risk-based recalculation, and superseding-review history for other regulated or evidence-sensitive deliverables.
