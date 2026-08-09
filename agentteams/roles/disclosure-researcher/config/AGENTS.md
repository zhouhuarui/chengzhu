# Disclosure collection rules

1. Validate issuer/exchange IDs, requested filing types, date cutoff, and official-source allowlist.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-disclosure-researcher`, and the role-specific route binds the upstream identity to `disclosure-researcher`. Never request or log either credential or the DashScope key.
3. Retrieve, hash, and index the original before parsing; the separate `chengzhu-backend` system node owns the authoritative freeze side effect.
4. Use deterministic text/table parsing first. Use the approved Bailian PDF Agent only for layout-heavy or scanned material under the Skill contract.
5. Emit evidence cards and financial facts with exact page/table locators and complete audit fields.
6. Return a structured `EvidenceGap` on unresolved retrieval or parsing failure. Never guess.
