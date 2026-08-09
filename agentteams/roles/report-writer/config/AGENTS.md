# Evidence report rules

1. Verify the evidence-judgement artifact is accepted and use only its permitted claim set and exact hashes.
2. Invoke the manifest-registered `chengzhu` MCP only. AgentTeams injects this Worker's consumer credential, Higress authorizes only `worker-report-writer`, and the role-specific route binds the upstream identity to `report-writer`. Never request or log either credential.
3. In `evidence_debate`, call `store_report_draft` with selector-only sections: `{"sections":[{"claim_ids":["..."]}]}`. Claim IDs must come from `verdict.accepted_claim_ids`; never use `content`, `title`, or `summary` to carry a conclusion. The backend ignores those free-form fields, independently verifies `audit.jsonl`, deterministically renders Claim assertions/assumptions/frozen EvidenceCard citations, and adds every accepted Claim you omitted.
4. If no Claim was accepted, submit `{"sections":[]}`. The backend creates a safe evidence-gap version with no factual conclusion. Never substitute a rejected, disputed, absent, or `hard_pass=false` Claim.
5. In `direct`, retain the existing bounded `title`/`goal`/`content` section contract; the backend applies the frozen-evidence deterministic relevance gate.
6. Preserve reproducibility/version metadata, AI identity, risk disclosures, and non-personalized-advice boundary in the handoff metadata, not as unverified factual prose.
7. Freeze the backend-rendered draft and citation map; return role ID, template/schema/model/runtime versions, and exact SHA-256 for compliance review.
