#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
runtime_dir="${project_root}/.agentteams"
project_env="${project_root}/.env"
manager_env="${AGENTTEAMS_ENV_FILE:-${runtime_dir}/agentteams-manager.env}"

readonly network_name="agentteams-net"
readonly network_bind_option="com.docker.network.bridge.host_binding_ipv4"
readonly copaw_image="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-copaw-worker:v1.2.0@sha256:dcdd9103535cfac247267e0f69661820c801396d58e2c8e0c14eefd40b63b7bc"
readonly -a worker_names=(
  research-lead
  disclosure-researcher
  market-context-researcher
  quality-analyst
  growth-analyst
  evidence-judge
  report-writer
  compliance-reviewer
)

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for required_command in docker curl jq mktemp awk grep seq python3; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done
[[ -f "${project_env}" && ! -L "${project_env}" ]] || fail "competition project env is not prepared"
[[ -f "${manager_env}" && ! -L "${manager_env}" ]] || fail "competition manager env is not prepared"

env_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '
    $1 == wanted {
      sub(/^[^=]*=/, "")
      value=$0
      if ((value ~ /^".*"$/) || (value ~ /^\047.*\047$/)) {
        value=substr(value, 2, length(value)-2)
      }
      print value
      exit
    }
  ' "${manager_env}"
}

model_max_tokens="${AGENTTEAMS_MODEL_MAX_TOKENS:-$(env_value AGENTTEAMS_MODEL_MAX_TOKENS)}"
model_max_tokens="${model_max_tokens:-4096}"
[[ "${model_max_tokens}" =~ ^[0-9]+$ ]] \
  || fail "invalid AGENTTEAMS_MODEL_MAX_TOKENS"

check_loopback_bindings() {
  local container="$1"
  docker inspect "${container}" | jq -e '
    [.[0].NetworkSettings.Ports // {} | to_entries[]? | .value[]? | .HostIp] as $bindings
    | all($bindings[]; . == "127.0.0.1" or . == "::1")
  ' >/dev/null || fail "${container} has a published port that is not loopback-bound"
}

cookie_file="$(mktemp "${TMPDIR:-/tmp}/chengzhu-higress-preflight-cookie.XXXXXX")"
cleanup() {
  rm -f -- "${cookie_file}"
}
trap cleanup EXIT HUP INT TERM

compose=(
  docker compose
  --env-file "${project_env}"
  --env-file "${manager_env}"
  -f "${project_root}/docker-compose.yml"
  -f "${project_root}/docker-compose.agentteams.yml"
)
app_network="$("${compose[@]}" config --format json | jq -er '.networks.default.name')" \
  || fail "cannot resolve the Chengzhu application network"

wait_url() {
  local url="$1"
  local label="$2"
  local attempt
  for attempt in $(seq 1 60); do
    if curl --fail --silent --show-error --connect-timeout 2 --max-time 5 "${url}" >/dev/null 2>&1; then
      return
    fi
    sleep 2
  done
  fail "${label} did not become ready"
}

for container in agentteams-controller agentteams-manager; do
  [[ "$(docker inspect --format '{{.State.Running}}' "${container}" 2>/dev/null)" == "true" ]] \
    || fail "${container} is not running"
done

network_json="$(docker network inspect "${network_name}" 2>/dev/null)" \
  || fail "Docker network ${network_name} does not exist"
NETWORK_JSON="${network_json}" NETWORK_OPTION="${network_bind_option}" python3 -c \
  "import json,os; n=json.loads(os.environ['NETWORK_JSON'])[0]; assert n.get('Driver')=='bridge', n.get('Driver'); assert (n.get('Options') or {}).get(os.environ['NETWORK_OPTION'])=='127.0.0.1', n.get('Options')"

wait_url http://127.0.0.1:5001/health "Chengzhu backend"
wait_url http://127.0.0.1:3000/ "Chengzhu frontend"

manager_image="$(docker inspect --format '{{.Config.Image}}' agentteams-manager)"
case "${manager_image}" in
  *agentteams-manager:v1.2.0@sha256:a429666fcc66fa01b81da3bc9ea5af98437697ef794f811ac7d358d4286c317e) ;;
  *) fail "Manager is not the pinned OpenClaw v1.2.0 image" ;;
esac
controller_image="$(docker inspect --format '{{.Config.Image}}' agentteams-controller)"
case "${controller_image}" in
  *agentteams-embedded:v1.2.0@sha256:c0de550018e51b36138a5990b1e8095eacc9d44cc7cbdb36a697785ba02c9be4) ;;
  *) fail "Controller is not the pinned embedded v1.2.0 image" ;;
esac

docker inspect agentteams-controller | jq -e \
  --arg expected "AGENTTEAMS_MODEL_MAX_TOKENS=${model_max_tokens}" \
  '.[0].Config.Env | index($expected) != null' >/dev/null \
  || fail "agentteams-controller is missing the requested AgentTeams per-call model limit"

# AgentTeams v1.2.0 materializes the Manager limit in OpenClaw's effective
# model configuration instead of retaining it as a container environment value.
manager_config="${runtime_dir}/manager-workspace/openclaw.json"
[[ -f "${manager_config}" && ! -L "${manager_config}" ]] \
  || fail "agentteams-manager effective model configuration is missing"
jq -e --argjson expected "${model_max_tokens}" '
  [.models.providers["agentteams-gateway"].models[]?.maxTokens] as $limits
  | ($limits | length) > 0 and all($limits[]; . == $expected)
' "${manager_config}" >/dev/null \
  || fail "agentteams-manager effective model configuration does not enforce maxTokens=${model_max_tokens}"

check_loopback_bindings agentteams-controller
check_loopback_bindings agentteams-manager
for container in chengzhu-neo4j-1 chengzhu-backend-1 chengzhu-frontend-1; do
  check_loopback_bindings "${container}"
done

docker inspect agentteams-controller | jq -e --arg network "${app_network}" \
  '.[0].NetworkSettings.Networks[$network] != null' >/dev/null \
  || fail "AgentTeams controller is not attached to the trusted Chengzhu application network"
docker inspect chengzhu-backend-1 | jq -e \
  --arg app_network "${app_network}" --arg worker_network "${network_name}" '
    .[0].NetworkSettings.Networks[$app_network] != null
    and .[0].NetworkSettings.Networks[$worker_network] == null
  ' >/dev/null \
  || fail "Chengzhu backend network isolation from Worker containers is not enforced"
docker inspect chengzhu-chengzhu-mcp-1 | jq -e \
  --arg app_network "${app_network}" --arg worker_network "${network_name}" '
    .[0].NetworkSettings.Networks[$app_network] != null
    and .[0].NetworkSettings.Networks[$worker_network] != null
  ' >/dev/null \
  || fail "Chengzhu MCP bridge is not attached to both required networks"

"${compose[@]}" exec -T backend python -c \
  "import json,urllib.request; value=json.load(urllib.request.urlopen('http://127.0.0.1:5001/health', timeout=5)); assert value['status']=='ok'"
"${compose[@]}" exec -T chengzhu-mcp python -c \
  "import json,urllib.request; value=json.load(urllib.request.urlopen('http://127.0.0.1:5002/health', timeout=5)); assert value['protocol_version']=='2024-11-05'"
docker exec agentteams-controller agt get managers default >/dev/null

team_ready=false
team_json=''
for _attempt in $(seq 1 60); do
  team_json="$(docker exec agentteams-controller agt get teams chengzhu-research-team -o json 2>/dev/null || true)"
  if jq -e '
    .name == "chengzhu-research-team"
    and .phase == "Active"
    and .leaderReady == true
    and .leaderName == "research-lead"
    # AgentTeams counts non-leader members in totalWorkers; workerMembers
    # below remains the authoritative full eight-role roster.
    and .totalWorkers == 7
    and ([.workerMembers[]?.name] | sort) == ([
      "research-lead",
      "disclosure-researcher",
      "market-context-researcher",
      "quality-analyst",
      "growth-analyst",
      "evidence-judge",
      "report-writer",
      "compliance-reviewer"
    ] | sort)
    and ([.workerMembers[]? | select(.role == "team_leader") | .name] == ["research-lead"])
    and ([.workerMembers[]? | select(.name != "research-lead") | .role] | all(. == "worker"))
  ' <<< "${team_json}" >/dev/null 2>&1; then
    team_ready=true
    break
  fi
  sleep 2
done
[[ "${team_ready}" == "true" ]] || fail "AgentTeams Team did not reach Active with the exact eight-role roster and research-lead leader"

workers_json="$(docker exec agentteams-controller agt get workers --team chengzhu-research-team -o json)"
jq -e '
  (.workers | length) == 8
  and ([.workers[].name] | unique | length) == 8
  and (([.workers[] | select(.name == "research-lead") | (.phase | ascii_downcase)] | first) == "running")
  and ([.workers[] | (.phase | ascii_downcase) | select(. == "running" or . == "ready" or . == "starting")] | length) <= 3
' <<< "${workers_json}" >/dev/null \
  || fail "Worker lifecycle state is inconsistent with the fixed Team or three-active-Worker limit"

for worker_name in "${worker_names[@]}"; do
  worker_container="agentteams-worker-${worker_name}"
  worker_phase="$(jq -r --arg name "${worker_name}" '.workers[] | select(.name == $name) | .phase' <<< "${workers_json}")"
  if docker inspect "${worker_container}" >/dev/null 2>&1; then
    actual_worker_image="$(docker inspect --format '{{.Config.Image}}' "${worker_container}")"
    [[ "${actual_worker_image}" == "${copaw_image}" ]] \
      || fail "${worker_container} is not using the pinned v1.2.0 CoPaw/QwenPaw image"
    if [[ "$(docker inspect --format '{{.State.Running}}' "${worker_container}")" == "true" ]]; then
      docker inspect "${worker_container}" | jq -e --arg network "${network_name}" \
        '.[0].NetworkSettings.Networks[$network] != null' >/dev/null \
        || fail "${worker_container} is not attached to ${network_name}"
      check_loopback_bindings "${worker_container}"
    fi
  else
    case "${worker_phase}" in
      Running|running|Ready|ready|Starting|starting) fail "${worker_name} reports ${worker_phase} but its container is missing" ;;
    esac
  fi
done

admin_user="${AGENTTEAMS_ADMIN_USER:-$(env_value AGENTTEAMS_ADMIN_USER)}"
admin_password="${AGENTTEAMS_ADMIN_PASSWORD:-$(env_value AGENTTEAMS_ADMIN_PASSWORD)}"
console_port="${AGENTTEAMS_PORT_CONSOLE:-$(env_value AGENTTEAMS_PORT_CONSOLE)}"
console_port="${console_port:-18001}"
[[ "${admin_user}" =~ ^[A-Za-z0-9_.@-]{1,80}$ ]] || fail "invalid AgentTeams admin user"
[[ ${#admin_password} -ge 8 ]] || fail "invalid AgentTeams admin password"
[[ "${console_port}" =~ ^[0-9]{2,5}$ ]] || fail "invalid Higress console port"
console_url="http://127.0.0.1:${console_port}"
wait_url "${console_url}/" "Higress console"
login_body="$(jq -cn --arg username "${admin_user}" --arg password "${admin_password}" '{username:$username,password:$password}')"
curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
  --request POST "${console_url}/session/login" \
  --header 'Content-Type: application/json' \
  --cookie-jar "${cookie_file}" \
  --data "${login_body}" >/dev/null

service_source_json="$(curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
  --cookie "${cookie_file}" "${console_url}/v1/service-sources/chengzhu-mcp-proxy")"
jq -e '
  .success != false
  and ((.data // .) as $source
    | $source.name == "chengzhu-mcp-proxy"
    and $source.type == "dns"
    and $source.domain == "chengzhu-mcp.agentteams.io"
    and ($source.port | tonumber) == 5002
    and $source.protocol == "http")
' <<< "${service_source_json}" >/dev/null \
  || fail "Higress Chengzhu MCP service source does not match the expected upstream"

for worker_name in "${worker_names[@]}"; do
  server_name="mcp-chengzhu-${worker_name}"
  consumer_name="worker-${worker_name}"
  route_json="$(curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
    --cookie "${cookie_file}" "${console_url}/v1/mcpServer?mcpServerName=${server_name}")"
  jq -e --arg name "${server_name}" '
    .success != false
    and ((.total // 0) | tonumber) == 1
    and ((.data // []) as $routes
      | ($routes | length) == 1
      and $routes[0].name == $name
      and $routes[0].type == "OPEN_API"
      and $routes[0].domains == ["aigw-local.agentteams.io"]
      and ($routes[0].services | length) == 1
      and $routes[0].services[0].name == "chengzhu-mcp-proxy.dns"
      and ($routes[0].services[0].port | tonumber) == 5002
      and ($routes[0].services[0].weight | tonumber) == 100)
  ' <<< "${route_json}" >/dev/null \
    || fail "Higress MCP route ${server_name} does not match its dedicated service binding"

  consumers_json="$(curl --fail --silent --show-error --connect-timeout 3 --max-time 10 \
    --cookie "${cookie_file}" \
    "${console_url}/v1/mcpServer/consumers?mcpServerName=${server_name}&consumerName=${consumer_name}")"
  jq -e --arg consumer "${consumer_name}" '
    .success != false
    and ((.total // 0) | tonumber) == 1
    and (.data | length) == 1
    and .data[0].consumerName == $consumer
  ' <<< "${consumers_json}" >/dev/null \
    || fail "Higress MCP route ${server_name} is not authorized for ${consumer_name}"
done

# The AgentTeams v1.2.0 SDK may persist a route while silently disabling its
# mcp-server Wasm match rule. Assert each Chengzhu role is actively enabled so
# a plain upstream route cannot masquerade as a working MCP proxy.
mcp_plugin_states="$(docker exec agentteams-controller awk '
  /^[[:space:]]+configDisable:/ { enabled = $2 }
  /^[[:space:]]+- mcp-server-mcp-chengzhu-/ { print $2 "=" enabled }
' /data/wasmplugins/mcp-server.internal.yaml)" \
  || fail "cannot inspect the active Higress MCP plugin rules"
for worker_name in "${worker_names[@]}"; do
  expected_plugin_state="mcp-server-mcp-chengzhu-${worker_name}.internal=false"
  [[ "$(grep -Fxc "${expected_plugin_state}" <<< "${mcp_plugin_states}")" == "1" ]] \
    || fail "Higress MCP plugin rule for ${worker_name} is missing or disabled"
done

# Use the Leader's injected Higress Consumer credential to perform an actual
# MCP initialize/tools-list round trip. This proves the authorized gateway path
# reaches the role-scoped Chengzhu MCP upstream; anonymous-denial alone cannot.
leader_mcp_config='/root/.copaw-worker/research-lead/.copaw/workspaces/default/config/mcporter.json'
leader_tools_json=''
leader_probe_ready=false
for _attempt in $(seq 1 60); do
  leader_tools_json="$(docker exec agentteams-worker-research-lead \
    mcporter --config "${leader_mcp_config}" list chengzhu --json 2>/dev/null || true)"
  if jq -e '
    .name == "chengzhu"
    and (.tools | type) == "array"
    and ([.tools[].name] | sort) == ([
      "freeze_evidence",
      "get_frozen_context",
      "request_publish_approval",
      "start_team_run"
    ] | sort)
  ' <<< "${leader_tools_json}" >/dev/null 2>&1; then
    leader_probe_ready=true
    break
  fi
  sleep 2
done
[[ "${leader_probe_ready}" == "true" ]] \
  || fail "authorized research-lead MCP gateway probe failed"
jq -e '
  .name == "chengzhu"
  and (.tools | type) == "array"
  and ([.tools[].name] | sort) == ([
    "freeze_evidence",
    "get_frozen_context",
    "request_publish_approval",
    "start_team_run"
  ] | sort)
' <<< "${leader_tools_json}" >/dev/null \
  || fail "authorized research-lead MCP gateway probe returned the wrong role-scoped tools"

"${compose[@]}" exec -T backend python -m app.integrations.agentteams.preflight

printf 'Competition preflight passed: backend, MCP routes, exact eight-role Team, MinIO put/get, and loopback bindings are ready.\n'
