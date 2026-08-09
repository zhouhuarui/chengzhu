# Market-context collection rules

1. Validate entity aliases, topics, peer set, geography, lookback, cutoff, source allowlist, entitlement, and quotation policy.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-market-context-researcher`, and the role-specific route binds the upstream identity to `market-context-researcher`. Never request or log either credential.
3. Freeze allowed references, deduplicate shared origins, distinguish all relevant timestamps, and preserve source definitions.
4. Use minimum necessary excerpts and affirmative entitlement; never bypass access controls.
5. Return context evidence cards, contradictions, access/coverage gaps, schema version, role ID, and artifact hashes.
