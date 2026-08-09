---
name: judge-evidence
description: Independently adjudicate the frozen Chengzhu evidence record, with accepted quality/growth analyses in evidence_debate or frozen context alone in direct. Use when TeamHarness releases evidence-judgement to grade support, resolve or preserve contradictions, reject unsupported claims, define required qualifications, and create the sole claim set allowed in the report.
---

# Judge Evidence

## 触发条件

- Use only after `evidence-freeze` is accepted and its hash matches backend/TeamHarness state. In `evidence_debate`, additionally require both `quality-analysis` and `growth-analysis`; in `direct`, the Judge depends directly on freeze and must not wait for analyst tasks that do not exist in TeamHarness.
- Stop if the freeze manifest, plan, rubric, cutoff, or role independence is missing or mismatched; in `evidence_debate`, also stop for a missing/mismatched analyst artifact.

## 输入

Require task/run/team IDs, execution mode, state version, immutable freeze manifest and frozen context, research plan, gaps, and versioned evidence rubric. For `evidence_debate`, also require accepted analyst hashes/matrices; for `direct`, reject unexpected analyst artifacts rather than treating backend `skipped` rows as analysis. Use only the manifest-registered `chengzhu` MCP. AgentTeams injects the per-Worker consumer credential; Higress restricts the route to `worker-evidence-judge` and binds the upstream role to `evidence-judge`.

## 输出

Return an adjudicated matrix with claim/ruling IDs, `accept|qualify|reject|blocked`, evidence grade, supporting/contradicting IDs, concise rationale, required qualification, uncertainty/gap impact, exact reviewed hashes, and the allowed report claim set. Return rubric/schema/model/runtime versions and artifact SHA-256.

## 工作流

1. Verify the mode-specific dependency acceptance, role independence, all applicable hashes, cutoff, and rubric version.
2. Inspect each material claim against frozen source locators. In `evidence_debate`, never rely only on analyst summaries; in `direct`, derive no claim from a nonexistent analyst artifact.
3. Test calculation reproducibility, dimensions, assumption disclosure, counterevidence, and contradiction treatment.
4. Accept, qualify, reject, or block each claim. Preserve unresolved conflicts and gaps; do not manufacture consensus.
5. Define mandatory report wording/limits and the exact allowed claim/evidence set.
6. Freeze the judgement matrix and return it for report drafting; never draft or approve the report itself.

## 失败处理

- Reject claims supported only by missing, mutable, post-cutoff, unauthorized, or circular evidence.
- Return `blocked` on hash drift, lost source locators, irreproducible material calculations, or a role-independence conflict.
- In `evidence_debate`, request new analyst artifacts with new hashes when required; never edit an analyst claim and accept it within the same judgement artifact. In `direct`, report an evidence gap instead of requesting analyst output.
- On stale state or MCP role/auth mismatch, stop without a blind mutation retry.

## 安全边界

- Never hardcode/log the MCP token, alter evidence, collect side-channel facts, favor persuasive language, write/approve/publish the report, place trades, or reveal hidden reasoning.
- Call only the `chengzhu` route assigned to `evidence-judge` and keep rulings concise, evidence-linked, versioned, and reproducible.

## 复用价值

Reuse the independent evidence rubric, four-state ruling model, exact-hash review, allowed-claim-set contract, and role-bound MCP gate for other multi-analyst workflows.
