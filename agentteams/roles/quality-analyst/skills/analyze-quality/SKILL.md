---
name: analyze-quality
description: Analyze business quality, financial quality, governance, and competitive moat using only the accepted Chengzhu evidence-freeze manifest. Use when TeamHarness releases quality-analysis and an auditable claim-evidence matrix with counterevidence, calculations, confidence, and gaps is required.
---

# Analyze Quality

## 触发条件

- Use only after the `chengzhu-backend` `evidence-freeze` system task completes and TeamHarness releases `quality-analysis` to `quality-analyst`.
- Stop if the freeze manifest, input hash, cutoff, metric dimensions, or accepted research-plan hash does not match backend state.

## 输入

Require task/run/team IDs, backend state version, accepted research plan, immutable freeze manifest, disclosure/context evidence IDs, normalized financial facts, quality rubric, and cutoff. Use only the manifest-registered `chengzhu` MCP. AgentTeams injects the per-Worker consumer credential; Higress restricts the route to `worker-quality-analyst` and binds the upstream role to `quality-analyst`.

## 输出

Return a quality claim-evidence matrix covering business economics, financial quality, governance, competitive advantage, and durability. For each claim include fact/calculation/assumption/judgment type, supporting and contradicting evidence IDs, formulas/dimensions, confidence, materiality, gap impact, falsifier, and artifact hash.

## 工作流

1. Verify system-freeze completion, hashes, schema, cutoff, and role identity before analysis.
2. Build the matrix before prose; map raw facts and calculations without changing frozen records.
3. Assess unit economics, cash/profit quality, balance-sheet resilience, governance, market position, and moat durability under the versioned rubric.
4. Seek disconfirming evidence, preserve accounting/definition conflicts, and distinguish structural from temporary observations.
5. Validate formulas, periods, units, currencies, and consolidation scope.
6. Freeze the quality artifact and return the exact hash for independent evidence judgement.

## 失败处理

- Block claims that rely on missing, unfrozen, unauthorized, post-cutoff, or dimensionally incompatible evidence.
- Preserve contradictory values and required follow-up; never choose or impute silently.
- On calculation failure, omit the numeric claim and return formula/operand IDs.
- On stale state or MCP role/auth mismatch, stop; do not retry a mutation with the wrong `expected_version`.

## 安全边界

- Never hardcode/log the MCP token, use mutable side-channel facts, fabricate citations, hide weaknesses, write/publish the final report, place trades, personalize advice, or reveal hidden reasoning.
- Call only the `chengzhu` route assigned to `quality-analyst` and keep all claim/evidence/hash/version links auditable.

## 复用价值

Reuse the frozen-input gate, quality rubric, claim-evidence schema, dimension checks, falsifier/counterevidence requirements, and role-bound MCP contract across issuers and sectors.
