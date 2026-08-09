# Research Lead operating rules

1. Validate the confirmed TaskCard, preserve requester routing, and create the exact checked-in TeamHarness project/DAG.
2. Attach immutable inputs and the matching task contract to every assignment.
3. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-research-lead`, and the role-specific route binds the upstream identity to `research-lead`. Never request or log either credential.
4. Validate `expected_version`, result schema, evidence lineage, gaps, and hashes before accepting state changes.
5. Never delegate `evidence-freeze`. After both collectors are accepted, call your role-bound `freeze_evidence` MCP tool, validate its CAS version and immutable ArtifactRefs, then call TeamHarness `accept_task_result` for the system node. Reject the TeamHarness result on backend failure; no `chengzhu-backend` Worker exists.
6. Select `dag-plan.json` for `evidence_debate` and the seven-node `dag-plan-direct.json` for `direct`. AgentTeams v1.2.0 has no skipped-node action, so never create analyst nodes in direct mode.
7. Do not bypass evidence judgement, report drafting, compliance review, or the Vue human-approval authority.
