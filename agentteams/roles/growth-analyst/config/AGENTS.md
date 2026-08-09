# Growth analysis rules

1. Verify that `evidence-freeze` completed and all input IDs/hashes match its immutable manifest.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-growth-analyst`, and the role-specific route binds the upstream identity to `growth-analyst`. Never request or log either credential.
3. Separate reported history, deterministic calculations, assumptions, and scenario judgments.
4. Preserve counterevidence, gaps, periods, units, formulas, sensitivities, and confidence.
5. Freeze the analysis artifact and return its role ID, schema/model/runtime versions, and SHA-256 for evidence judgement.
