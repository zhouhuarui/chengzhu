# Independent review rules

1. Verify input hashes and ensure the analysis task was accepted before review began.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-compliance-reviewer`, and the role-specific route binds the upstream identity to `compliance-reviewer`. Never request or log either credential.
3. Check claim coverage, source integrity, cutoff, entitlement, calculations, risk wording, and audit completeness.
4. Sample evidence at its original page/table locator; do not rely only on summaries.
5. Return `revise` for any critical/high issue and keep the project incomplete.
6. On acceptance, record zero unresolved critical/high issues and the exact approved artifact hash.
