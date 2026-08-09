---
name: plan-research
description: Plan a human-confirmed Chengzhu TaskCard and orchestrate its backend-aligned AgentTeams project. Use when the research-lead must create the exact mode graph (nine TeamHarness nodes for evidence_debate or seven for direct), define evidence requirements, enforce state-version and acceptance gates, and route the final reviewed artifact to Vue approval.
---

# Plan Research

## 触发条件

- Trigger only after the TaskCard has a durable human-confirmation event and `execution_mode: agentteams`.
- Use Project Work with the checked-in mode graph: nine tasks for `evidence_debate`, seven TeamHarness tasks for `direct`. Stop when issuer, cutoff, source/license policy, output, or risk constraints remain materially ambiguous.
- Treat `research-lead` as both the Team Leader Worker and owner of the first `research-plan` task; Manager is external control plane and not a ninth role.

## 输入

Require task/run/team IDs, backend `state_version`, confirmed TaskCard, requester route, issuer IDs, questions, cutoff, source/entitlement policy, output schema, and risk constraints. Use only the manifest-registered `chengzhu` MCP. AgentTeams injects the per-Worker consumer credential; Higress restricts the route to `worker-research-lead` and binds the upstream role to `research-lead`.

## 输出

Return an accepted research plan with evidence questions and acceptance rules, the TeamHarness project ID, mode-specific task states, assignments, CAS state versions, accepted artifact hashes, escalations, compliance decision, and requester-delivery receipt. Never output secrets or private chain-of-thought.

## 工作流

1. Validate human confirmation, identifiers, route, cutoff, and current backend `state_version`.
2. Create the dedicated task-room project. Render `dag-plan.json` for `evidence_debate`; render the seven-node `dag-plan-direct.json` for `direct`. Do not emulate skipping because AgentTeams v1.2.0 does not expose a skipped-node action.
3. Complete and accept `research-plan`; then delegate the two ready collector tasks to their exact role IDs.
4. After both collectors are accepted, invoke your role-bound `freeze_evidence` MCP tool with the expected state version. Validate the returned CAS version and immutable refs, then call TeamHarness `accept_task_result` for the system node. Do not assign this node or wait for a nonexistent backend Worker; reject its TeamHarness result if the MCP operation fails.
5. For `evidence_debate`, delegate quality and growth only after freeze and Judge only after both. For `direct`, the Judge depends directly on freeze and analyst Workers remain asleep. In both modes, draft only after judgement and compliance only after the draft.
6. Release exactly the compliance-accepted hash to the Vue approval authority. Preserve approval/rejection/rollback events and mark requester delivery before project completion.

## 失败处理

- Treat missing confirmation, stale `expected_version`, role/header mismatch, route ambiguity, or graph mismatch as a blocker.
- On MCP conflict, refresh state and re-evaluate; never blind-retry a mutation with a changed state version. Preserve idempotency keys.
- Reject malformed results without mutating their evidence. Keep blocking gaps or pause for human scope changes.
- Resume from TeamHarness/backend durable state and suppress duplicate assignments after interruption.

## 安全边界

- Never hardcode/log MCP credentials, freeze evidence directly, fabricate sources, modify frozen artifacts, bypass a dependency, approve the report, publish without Vue approval, place orders, or reveal hidden reasoning.
- Keep the `research-lead` route identity, task/run IDs, state versions, AgentTeams v1.2.0, package/image hashes, and decisions in the audit trail.
- Treat output as general research, not personalized investment advice.

## 复用价值

Reuse the confirmation gate, role-bound MCP contract, mode-specific orchestration, system-node boundary, CAS/idempotency rules, immutable artifacts, and independent approval gates for other evidence-heavy workflows.
