# Quality analysis rules

1. Verify that `evidence-freeze` completed and all input IDs/hashes match its immutable manifest.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-quality-analyst`, and the role-specific route binds the upstream identity to `quality-analyst`. Never request or log either credential.
3. Build the quality claim-evidence matrix before conclusions; label fact, calculation, assumption, and judgment.
4. Preserve accounting conflicts, counterevidence, gaps, formulas, dimensions, and confidence.
5. Freeze the analysis artifact and return its role ID, schema/model/runtime versions, and SHA-256 for evidence judgement.
