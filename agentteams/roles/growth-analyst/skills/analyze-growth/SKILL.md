---
name: analyze-growth
description: Analyze historical growth, forward drivers, catalysts, constraints, and bull/base/bear scenarios using only the accepted Chengzhu evidence-freeze manifest. Use when TeamHarness releases growth-analysis and reproducible assumptions, sensitivities, counterevidence, and gap impacts are required.
---

# Analyze Growth

## 触发条件

- Use only after the `chengzhu-backend` `evidence-freeze` system task completes and TeamHarness releases `growth-analysis` to `growth-analyst`.
- Stop if the freeze manifest, input hash, cutoff, forecast horizon, units, or accepted research-plan hash is inconsistent.

## 输入

Require task/run/team IDs, backend state version, accepted plan, immutable freeze manifest, disclosure/context facts, historical normalized metrics, forecast horizon, scenario/rounding policy, and cutoff. Use only the manifest-registered `chengzhu` MCP. AgentTeams injects the per-Worker consumer credential; Higress restricts the route to `worker-growth-analyst` and binds the upstream role to `growth-analyst`.

## 输出

Return a growth claim-evidence matrix, historical driver decomposition, forward indicators, catalysts/constraints, bull/base/bear scenarios, formulas/operands, sensitivities, assumptions and sources, supporting/contradicting evidence, confidence, gap impacts, and artifact hash.

## 工作流

1. Verify system-freeze completion, hashes, schema, cutoff, horizon, and exact role identity.
2. Separate reported history, deterministic calculations, explicit assumptions, and scenario judgments.
3. Decompose volume, price, mix, capacity, share, unit economics, and other task-approved drivers with visible dimensions.
4. Build scenarios and sensitivities without false precision; attach an evidence ID or assumption source to every driver.
5. Seek disconfirming signals, preserve conflicts, and state what would invalidate each material growth view.
6. Freeze the growth artifact and return the exact hash for independent evidence judgement.

## 失败处理

- Do not generate a forecast when key operands, time alignment, or definitions are missing; return a blocking gap or bounded qualitative statement.
- Preserve contradictory indicators and scenario ranges; never hide uncertainty or choose the favorable source silently.
- On formula failure, omit the numeric result and return formula/operand IDs.
- On stale state or MCP role/auth mismatch, stop without mutating backend state.

## 安全边界

- Never hardcode/log the MCP token, fabricate forecasts/citations, use unfrozen or post-cutoff facts, hide assumptions, write/publish the final report, place trades, personalize advice, or expose hidden reasoning.
- Call only the `chengzhu` route assigned to `growth-analyst` and retain all formula/evidence/hash/version links.

## 复用价值

Reuse the frozen-input gate, driver decomposition, explicit scenario taxonomy, sensitivity schema, counterevidence/falsifier rules, and role-bound MCP contract for other forecast-sensitive analyses.
