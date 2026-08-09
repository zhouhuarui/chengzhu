# Chengzhu AgentTeams competition package

This directory packages Chengzhu's locked Agent Team contract for AgentTeams **v1.2.0**. One Team references exactly eight Worker CRs, including the Team Leader:

1. `research-lead` (`team_leader`)
2. `disclosure-researcher`
3. `market-context-researcher`
4. `quality-analyst`
5. `growth-analyst`
6. `evidence-judge`
7. `report-writer`
8. `compliance-reviewer`

Manager remains the external OpenClaw control plane and is not a Worker role. `evidence_debate` contains nine TeamHarness nodes. `direct` contains seven nodes and omits the two analyst task nodes while retaining the same eight-member Team roster. In both modes, `evidence-freeze` is executed by the trusted deterministic identity `chengzhu-backend`; it is not a Worker, Team member, or Matrix assignee.

## What is pinned

- Upstream AgentTeams tag: `v1.2.0`
- Upstream review commit: `793db242257a569d911b1aa59c1cd554af78511f`
- Installer URL and SHA-256: [`UPSTREAM.lock`](UPSTREAM.lock)
- Resource API: `agentteams.io/v1beta1`
- Worker runtime/model profiles: `copaw` / locked non-thinking and reasoning models
- User-facing runtime name: QwenPaw
- Embedded controller, OpenClaw Manager, and CoPaw Worker images: tag plus OCI index digest in [`UPSTREAM.lock`](UPSTREAM.lock)
- TeamHarness plugin contract from the pinned release

`copaw` is the official compatibility enum used by the locked Chengzhu/AgentTeams contract; QwenPaw is its user-facing name. Do not silently change the API, role IDs, task keys, runtime, model, image, or installer. Upgrade them as one compatibility change and regenerate runtime evidence.

Role models are intentionally split: non-thinking work uses
`qwen3-30b-a3b-instruct-2507`, while the two analysts and Evidence Judge use
reasoning-capable `qwen3.5-plus`. Only Research Lead starts `Running`; all
other Workers start `Sleeping` and TeamHarness enforces a maximum of three
active Workers. See [`MODEL_PROFILES.md`](MODEL_PROFILES.md).

The recommended single-machine competition host is **8 CPU / 16 GB RAM**;
the supported minimum is **4 CPU / 8 GB RAM**. Actual model calls remain
remote, but the controller, Manager, Workers, Matrix, Higress, MinIO, Chengzhu
services, and observability stack still require local capacity.

## Layout

- `manifests/workers/`: eight declarative Worker CRs with complete Agent Identity fields
- `manifests/team.yaml`: one Team with exactly one `team_leader`
- `roles/<worker>/`: importable package source (`manifest.json`, `config/`, and a custom Skill)
- `teamharness/dag-plan.json`: nine-node `evidence_debate` `projectflow.plan_dag` payload aligned to `backend/app/team/contracts.py`
- `teamharness/dag-plan-direct.json`: seven-node `direct` payload; the backend still records the two analyst tasks as durable `skipped` rows for API/history compatibility
- `teamharness/task-contracts.md`: acceptance contract for eight Worker tasks plus one backend system task
- `teamharness/runtime-contract.md`: deployment-injected MCP URL/auth/header and role-ID contract
- `scripts/install-agentteams-v1.2.0.sh`: checksum-verified installer wrapper with no download-to-shell pipe
- `scripts/build-worker-packages.sh`: deterministic ZIP builder and `SHA256SUMS`
- `scripts/apply-manifests.sh`: applies Worker state, uploads content-addressed packages, then applies the Team
- `scripts/verify.sh`: offline shell/YAML/JSON/identity/Skill/DAG/MCP/package verification

Worker YAML intentionally omits `spec.package`. First `agt apply -f` creates the Worker with its pinned runtime/image; then `agt apply worker --zip` uploads a content-addressed package and updates only package/model/runtime, preserving identity, resources, and image.

## Chengzhu MCP boundary

Each Worker CR declares one role-specific `chengzhu` MCP route:

- Worker endpoint: `spec.mcpServers[name=chengzhu]` through Higress
- Worker auth: AgentTeams-injected per-Worker Consumer credential
- Upstream endpoint: `http://chengzhu-mcp.agentteams.io:5002/mcp/<role-id>` on `agentteams-net` only
- Upstream auth: service-only `AGENTTEAMS_MCP_GATEWAY_TOKEN`, stored in Higress and Chengzhu MCP but never injected into a Worker
- Role binding: immutable route path plus one allowed `worker-<role-id>` Consumer

Every mutation also carries task/run/team ID, idempotency key, and `expected_version`. Never put any credential in prompts, manifests, Matrix messages, URLs, shared artifacts, or logs. See [`teamharness/runtime-contract.md`](teamharness/runtime-contract.md).

## Safe local workflow

The competition deployment is the locked single-machine Docker path. The recommended entry point performs version checks, installs or verifies AgentTeams, starts Chengzhu/Neo4j/MinIO/Higress/MCP, applies the roster when safe, and runs live service/model probes:

```bash
make competition-up
```

Prerequisites are Bash, Ruby, `zip`, `unzip`, Docker, and the credentials listed in `.env.example`. Installing AgentTeams changes local container state, so inspect [`UPSTREAM.lock`](UPSTREAM.lock) and the wrapper first. The wrapper downloads the exact tagged installer to a temporary file, verifies its SHA-256, sets `AGENTTEAMS_VERSION=v1.2.0`, and only then invokes Bash; it never uses an unverified download-to-shell pipe.

If the exact Team already exists, startup verifies it and **does not re-apply Worker manifests by default**, because applying the declarative initial `Sleeping` states could interrupt an active project. Set `AGENTTEAMS_RECONCILE_MANIFESTS=1` only during an explicit maintenance window after verifying that the Team has no active tasks. Reconciliation is an upsert and never a license to prune unknown resources.

## Runtime proof for submission

Static validation does not prove an end-to-end Agent Team. Preserve at least:

- `agt get workers --team chengzhu-research-team` and `agt get teams chengzhu-research-team` showing eight members and one Leader;
- Team room handoffs with exact role IDs plus backend `state_version` transitions;
- the mode-appropriate project proof: nine TeamHarness nodes for `evidence_debate`, or seven for `direct` plus the backend's two durable analyst `skipped` rows;
- `shared/tasks/<taskId>/` specs/results, immutable artifact manifests, and hashes;
- MCP gateway logs proving role headers without exposing tokens;
- package `SHA256SUMS`, runtime/model/image/version metadata, and Bailian audit fields;
- failure injection for CAS conflict, evidence gap, Bailian degradation, review rejection, and rollback/replay.

## Integration boundary

AgentTeams owns role identity, isolation, communication, and TeamHarness task orchestration. Chengzhu owns deterministic source side effects, evidence freezing/normalization, durable state/version CAS, report artifacts, and the Vue human publication authority. To cross this boundary, Research Lead calls its role-bound `freeze_evidence` MCP tool, validates the returned immutable ArtifactRef and state version, and only then calls TeamHarness `accept_task_result` for `evidence-freeze`. The node must never be assigned to or simulated by a Worker.

The wider migration and competition mapping are documented in [`../docs/competition-agentteams.md`](../docs/competition-agentteams.md). This remains a research workflow, not autonomous trading or personalized investment advice.

## Competition delivery documents

- [`../docs/agentteams-operations.md`](../docs/agentteams-operations.md): startup/shutdown, preflight, backup/restore, safe reconciliation, incidents, secret rotation, OTel, and keyless replay
- [`../docs/agentteams-threat-model.md`](../docs/agentteams-threat-model.md): assets, trust boundaries, controls, tests, and residual risk
- [`../docs/agentteams-third-party.md`](../docs/agentteams-third-party.md): pinned provenance, licensing boundaries, and deployment-time SBOM requirements
- [`../docs/agentteams-demo-script.md`](../docs/agentteams-demo-script.md): the three-minute `evidence_debate` competition demo and evidence checklist

The repository contains the deployment and verification implementation, but this workstation has not produced Docker E2E evidence or the final recording. The all-Worker, per-run CNY 2 hard budget gate also remains unimplemented; see the linked documents before making competition claims.
