# Chengzhu MCP runtime contract

This contract connects AgentTeams Workers to the Chengzhu deterministic execution plane through AgentTeams v1.2.0's native `spec.mcpServers` field.

## Endpoint and authentication

- Every Worker declares one MCP server named `chengzhu`; its URL is an AgentTeams/Higress route dedicated to that exact role.
- AgentTeams injects the Worker's own gateway Consumer credential into `mcporter`. It is never copied into a TaskCard, prompt, Skill file, Matrix message, shared artifact, manifest, URL, or log.
- Higress authorizes only `worker-<role-id>` on that route. It authenticates upstream with a distinct service-only `AGENTTEAMS_MCP_GATEWAY_TOKEN` that no Worker receives.
- The upstream path `/mcp/<role-id>` binds the trusted role identity. A caller-supplied role header can only confirm the path and can never override it.
- `http://chengzhu-mcp.agentteams.io:5002` is a Docker-network alias reachable only on `agentteams-net`; Workers never call it directly.
- Send task ID, run ID, team ID, idempotency key, and `expected_version` on every state-changing call. Treat a CAS conflict as a refresh/re-evaluate event, not a blind retry.

The controller renders runtime-native MCP configuration from the checked-in `spec.mcpServers` declarations. TeamHarness remains an image-bundled local plugin.

## Allowed Worker role IDs

| Worker CR | Higress route binding |
|---|---|
| `research-lead` | `/mcp/research-lead` |
| `disclosure-researcher` | `/mcp/disclosure-researcher` |
| `market-context-researcher` | `/mcp/market-context-researcher` |
| `quality-analyst` | `/mcp/quality-analyst` |
| `growth-analyst` | `/mcp/growth-analyst` |
| `evidence-judge` | `/mcp/evidence-judge` |
| `report-writer` | `/mcp/report-writer` |
| `compliance-reviewer` | `/mcp/compliance-reviewer` |

`chengzhu-backend` is not a Worker CR and is not a Team member. It is the trusted deterministic executor for `evidence-freeze`; no Worker route exists for that identity.

The `research-lead` bridges that system node explicitly: call its own authenticated `freeze_evidence` MCP tool, validate the returned CAS version and ArtifactRefs, and then acknowledge `${PROJECT_ID}-evidence-freeze` through TeamHarness `accept_task_result`. A Matrix message or MCP success alone does not release dependencies. There is no Worker to acknowledge this node and no v1.2.0 `skipped` action.

## Runtime naming

AgentTeams v1.2.0 manifests use the official compatibility value `runtime: copaw`. The user-facing runtime is QwenPaw and the image is pinned to `agentteams-copaw-worker:v1.2.0`. Manager remains the installer-provided OpenClaw control plane and is not counted among the eight Worker roles.
