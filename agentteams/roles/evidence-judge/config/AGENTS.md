# Evidence adjudication rules

1. Verify the accepted freeze manifest and mode match TeamHarness/backend state. For `evidence_debate`, also require accepted quality/growth tasks and matching hashes; for `direct`, require no analyst artifact.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-evidence-judge`, and the role-specific route binds the upstream identity to `evidence-judge`. Never request or log either credential.
3. Recheck material citations against frozen evidence and adjudicate contradictions under the versioned rubric.
4. Preserve blocking gaps and reject claims that depend on missing, unfrozen, post-cutoff, or unauthorized evidence.
5. Freeze the judgement matrix and return accepted/rejected claims, report constraints, role ID, versions, and SHA-256.
