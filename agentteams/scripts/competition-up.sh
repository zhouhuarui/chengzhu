#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
runtime_dir="${project_root}/.agentteams"
project_env="${project_root}/.env"
manager_env="${AGENTTEAMS_ENV_FILE:-${runtime_dir}/agentteams-manager.env}"
mcp_token_file="${runtime_dir}/chengzhu-mcp-token"
controller_token_file="${runtime_dir}/controller-token"
owner_file="${runtime_dir}/owner"
mcp_upstream_ip_file="${runtime_dir}/mcp-upstream-ip"

readonly network_name="agentteams-net"
readonly network_bind_option="com.docker.network.bridge.host_binding_ipv4"
readonly controller_container="agentteams-controller"
readonly manager_container="agentteams-manager"
readonly controller_image="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:v1.2.0@sha256:c0de550018e51b36138a5990b1e8095eacc9d44cc7cbdb36a697785ba02c9be4"
readonly manager_image="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager:v1.2.0@sha256:a429666fcc66fa01b81da3bc9ea5af98437697ef794f811ac7d358d4286c317e"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for required_command in docker curl jq openssl awk seq tr; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"
[[ -f "${project_env}" && ! -L "${project_env}" ]] || fail "create .env from .env.example before competition-up"

owner_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '
    $1 == wanted {
      sub(/^[^=]*=/, "")
      print
      exit
    }
  ' "${owner_file}"
}

owner_matches_project() {
  [[ -f "${owner_file}" && ! -L "${owner_file}" ]] || return 1
  [[ "$(owner_value schema)" == "chengzhu-agentteams-owner-v1" ]] || return 1
  [[ "$(owner_value project_root)" == "${project_root}" ]] || return 1
  [[ "$(owner_value owns_control_plane)" == "true" ]] || return 1
  [[ "$(owner_value controller_id)" =~ ^[a-f0-9]{64}$ ]] || return 1
  [[ "$(owner_value manager_id)" =~ ^[a-f0-9]{64}$ ]] || return 1
}

owner_matches_containers() {
  owner_matches_project || return 1
  [[ "$(docker inspect --format '{{.Id}}' "${controller_container}" 2>/dev/null)" == "$(owner_value controller_id)" ]] \
    || return 1
  [[ "$(docker inspect --format '{{.Id}}' "${manager_container}" 2>/dev/null)" == "$(owner_value manager_id)" ]]
}

validate_agentteams_network() {
  local driver
  local bind_address
  driver="$(docker network inspect "${network_name}" | jq -er '.[0].Driver')" \
    || fail "cannot inspect Docker network ${network_name}"
  bind_address="$(docker network inspect "${network_name}" | jq -er --arg option "${network_bind_option}" '.[0].Options[$option] // empty')" \
    || fail "Docker network ${network_name} must set ${network_bind_option}=127.0.0.1"
  [[ "${driver}" == "bridge" ]] \
    || fail "existing Docker network ${network_name} uses driver ${driver}; expected bridge"
  [[ "${bind_address}" == "127.0.0.1" ]] \
    || fail "existing Docker network ${network_name} binds published ports to ${bind_address}; expected 127.0.0.1"
}

validate_control_plane_images() {
  local actual_controller_image
  local actual_manager_image
  actual_controller_image="$(docker inspect --format '{{.Config.Image}}' "${controller_container}")"
  actual_manager_image="$(docker inspect --format '{{.Config.Image}}' "${manager_container}")"
  [[ "${actual_controller_image}" == "${controller_image}" ]] \
    || fail "existing ${controller_container} is not the pinned v1.2.0 image; refusing automatic replacement"
  [[ "${actual_manager_image}" == "${manager_image}" ]] \
    || fail "existing ${manager_container} is not the pinned v1.2.0 OpenClaw image; refusing automatic replacement"
}

validate_control_plane_model_limit() {
  local expected="AGENTTEAMS_MODEL_MAX_TOKENS=${model_max_tokens}"
  local manager_config="${runtime_dir}/manager-workspace/openclaw.json"

  docker inspect "${controller_container}" | jq -e --arg expected "${expected}" \
    '.[0].Config.Env | index($expected) != null' >/dev/null \
    || fail "${controller_container} does not carry the requested per-call model limit ${model_max_tokens}; refusing automatic replacement"

  # In embedded mode the controller creates Manager and materializes maxTokens
  # in OpenClaw's effective model config; v1.2.0 does not retain this setting as
  # a Manager container environment variable.
  [[ -f "${manager_config}" && ! -L "${manager_config}" ]] \
    || fail "${manager_container} effective model config is missing: ${manager_config}"
  jq -e --argjson expected "${model_max_tokens}" '
    [.models.providers["agentteams-gateway"].models[]?.maxTokens] as $limits
    | ($limits | length) > 0 and all($limits[]; . == $expected)
  ' "${manager_config}" >/dev/null \
    || fail "${manager_container} effective model config does not enforce maxTokens=${model_max_tokens}; refusing automatic replacement"
}

wait_control_plane() {
  local attempt
  for attempt in $(seq 1 60); do
    if [[ "$(docker inspect --format '{{.State.Running}}' "${controller_container}" 2>/dev/null)" == "true" ]] \
      && [[ "$(docker inspect --format '{{.State.Running}}' "${manager_container}" 2>/dev/null)" == "true" ]] \
      && docker exec "${controller_container}" agt get managers default >/dev/null 2>&1; then
      return 0
    fi
    sleep 2
  done
  return 1
}

write_owner_file() {
  local temporary_owner="${owner_file}.tmp.$$"
  local controller_id
  local manager_id
  controller_id="$(docker inspect --format '{{.Id}}' "${controller_container}")"
  manager_id="$(docker inspect --format '{{.Id}}' "${manager_container}")"
  umask 077
  {
    printf 'schema=chengzhu-agentteams-owner-v1\n'
    printf 'project_root=%s\n' "${project_root}"
    printf 'owns_control_plane=true\n'
    printf 'owns_network=%s\n' "${network_created_by_project}"
    printf 'controller_id=%s\n' "${controller_id}"
    printf 'manager_id=%s\n' "${manager_id}"
  } > "${temporary_owner}"
  chmod 0600 "${temporary_owner}"
  mv "${temporary_owner}" "${owner_file}"
}

dotenv_value() {
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
  ' "${project_env}"
}

model_max_tokens="${AGENTTEAMS_MODEL_MAX_TOKENS:-$(dotenv_value AGENTTEAMS_MODEL_MAX_TOKENS)}"
model_max_tokens="${model_max_tokens:-4096}"
[[ "${model_max_tokens}" =~ ^[0-9]+$ ]] \
  || fail "AGENTTEAMS_MODEL_MAX_TOKENS must be a positive integer"
(( model_max_tokens >= 1 && model_max_tokens <= 131072 )) \
  || fail "AGENTTEAMS_MODEL_MAX_TOKENS must be between 1 and 131072"

mkdir -p "${runtime_dir}" "${runtime_dir}/manager-workspace" "${runtime_dir}/host-share"
chmod 0700 "${runtime_dir}"
[[ ! -e "${owner_file}" || ( -f "${owner_file}" && ! -L "${owner_file}" ) ]] \
  || fail "AgentTeams owner marker must be a regular file"

network_created_by_project=false
if ! docker network inspect "${network_name}" >/dev/null 2>&1; then
  docker network create \
    --driver bridge \
    --opt "${network_bind_option}=127.0.0.1" \
    "${network_name}" >/dev/null
  network_created_by_project=true
fi
validate_agentteams_network

controller_exists=false
manager_exists=false
docker inspect "${controller_container}" >/dev/null 2>&1 && controller_exists=true
docker inspect "${manager_container}" >/dev/null 2>&1 && manager_exists=true
[[ "${controller_exists}" == "${manager_exists}" ]] \
  || fail "only one AgentTeams control-plane container exists; refusing an unsafe partial replacement"

install_control_plane=false
if [[ "${controller_exists}" == "false" ]]; then
  if [[ -e "${owner_file}" ]] && ! owner_matches_project; then
    fail "existing AgentTeams owner marker does not belong to this project"
  fi
  install_control_plane=true
else
  validate_control_plane_images
  validate_control_plane_model_limit
  if [[ "$(docker inspect --format '{{.State.Running}}' "${controller_container}")" != "true" ]] \
    || [[ "$(docker inspect --format '{{.State.Running}}' "${manager_container}")" != "true" ]]; then
    owner_matches_containers \
      || fail "existing AgentTeams control plane is stopped but is not owned by this project"
    docker start "${controller_container}" >/dev/null
    docker start "${manager_container}" >/dev/null
  elif [[ -e "${owner_file}" ]] && ! owner_matches_containers; then
    fail "AgentTeams owner marker does not match the running control-plane containers"
  fi
  wait_control_plane \
    || fail "existing pinned AgentTeams control plane is not healthy; refusing automatic reinstallation"
  [[ -f "${manager_env}" && ! -L "${manager_env}" ]] \
    || fail "reuse requires a regular AgentTeams manager env file: ${manager_env}"
  printf 'Reusing healthy pinned AgentTeams v1.2.0 control plane; installer skipped.\n'
fi

if [[ ! -f "${mcp_token_file}" ]]; then
  umask 077
  openssl rand -hex 32 > "${mcp_token_file}"
fi
[[ ! -L "${mcp_token_file}" ]] || fail "MCP token file must not be a symlink"
chmod 0600 "${mcp_token_file}"

dashscope_key="${AGENTTEAMS_LLM_API_KEY:-}"
for key_name in AGENTTEAMS_LLM_API_KEY DASHSCOPE_API_KEY VISION_LLM_API_KEY; do
  if [[ -z "${dashscope_key}" ]]; then
    dashscope_key="$(dotenv_value "${key_name}")"
  fi
done
[[ -n "${dashscope_key}" ]] || fail "DASHSCOPE_API_KEY or VISION_LLM_API_KEY is required for live AgentTeams"

export AGENTTEAMS_ENV_FILE="${manager_env}"
export AGENTTEAMS_NON_INTERACTIVE=1
export AGENTTEAMS_LOCAL_ONLY=1
export AGENTTEAMS_LANGUAGE=zh
export AGENTTEAMS_LLM_PROVIDER=qwen
export AGENTTEAMS_LLM_API_KEY="${dashscope_key}"
export AGENTTEAMS_DEFAULT_MODEL=qwen3-30b-a3b-instruct-2507
export AGENTTEAMS_MODEL_MAX_TOKENS="${model_max_tokens}"
export AGENTTEAMS_MANAGER_RUNTIME=openclaw
export AGENTTEAMS_DEFAULT_WORKER_RUNTIME=copaw
export AGENTTEAMS_DASHBOARD=0
export AGENTTEAMS_UPGRADE_KEEP_ALL=1
export AGENTTEAMS_DATA_DIR=agentteams-data
export AGENTTEAMS_WORKSPACE_DIR="${runtime_dir}/manager-workspace"
export AGENTTEAMS_HOST_SHARE_DIR="${runtime_dir}/host-share"

"${script_dir}/fetch-official-skills.sh"
"${script_dir}/verify.sh"
if [[ "${install_control_plane}" == "true" ]]; then
  "${script_dir}/install-agentteams-v1.2.0.sh" manager
  validate_control_plane_images
  validate_control_plane_model_limit
  wait_control_plane || fail "new AgentTeams control plane did not become healthy"
  [[ -f "${manager_env}" && ! -L "${manager_env}" ]] \
    || fail "AgentTeams installer did not create a regular manager env file"
  write_owner_file
fi
chmod 0600 "${manager_env}"

temporary_controller_token="${runtime_dir}/.controller-token.tmp"
rm -f -- "${temporary_controller_token}"
docker cp \
  agentteams-controller:/var/run/agentteams/cli-token \
  "${temporary_controller_token}"
[[ -s "${temporary_controller_token}" && ! -L "${temporary_controller_token}" ]] \
  || fail "AgentTeams controller token was not created"
chmod 0600 "${temporary_controller_token}"
mv "${temporary_controller_token}" "${controller_token_file}"

compose=(
  docker compose
  --env-file "${project_env}"
  --env-file "${manager_env}"
  -f "${project_root}/docker-compose.yml"
  -f "${project_root}/docker-compose.agentteams.yml"
)
"${compose[@]}" up -d --build

mcp_container_id="$("${compose[@]}" ps -q chengzhu-mcp)"
[[ "${mcp_container_id}" =~ ^[a-f0-9]{64}$ ]] \
  || fail "cannot resolve the Chengzhu MCP container"
mcp_upstream_ip="$(docker inspect "${mcp_container_id}" | jq -er \
  --arg network "${network_name}" '.[0].NetworkSettings.Networks[$network].IPAddress')" \
  || fail "cannot resolve the Chengzhu MCP address on ${network_name}"
[[ "${mcp_upstream_ip}" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] \
  || fail "invalid Chengzhu MCP address on ${network_name}"
previous_mcp_upstream_ip=''
if [[ -f "${mcp_upstream_ip_file}" && ! -L "${mcp_upstream_ip_file}" ]]; then
  previous_mcp_upstream_ip="$(tr -d '\r\n' < "${mcp_upstream_ip_file}")"
elif [[ -e "${mcp_upstream_ip_file}" ]]; then
  fail "MCP upstream address marker must be a regular file"
fi
refresh_higress_dns=false
if [[ "${previous_mcp_upstream_ip}" != "${mcp_upstream_ip}" ]]; then
  refresh_higress_dns=true
fi

# The backend must reach the trusted AgentTeams control plane, but it must not
# share agentteams-net with untrusted Worker containers. Attach only the
# controller to Chengzhu's application network; Workers remain isolated behind
# the role-scoped MCP gateway.
app_network="$("${compose[@]}" config --format json | jq -er '.networks.default.name')" \
  || fail "cannot resolve the Chengzhu application network"
docker network inspect "${app_network}" >/dev/null 2>&1 \
  || fail "Chengzhu application network ${app_network} does not exist"
if ! docker inspect "${controller_container}" | jq -e --arg network "${app_network}" \
  '.[0].NetworkSettings.Networks[$network] != null' >/dev/null; then
  docker network connect --alias agentteams-controller \
    "${app_network}" "${controller_container}"
fi
docker inspect "${controller_container}" | jq -e --arg network "${app_network}" '
  .[0].NetworkSettings.Networks[$network] as $attachment
  | $attachment != null
  and (($attachment.Aliases // []) | index("agentteams-controller") != null)
' >/dev/null || fail "AgentTeams controller is missing its Chengzhu application-network alias"

reconcile_manifests="${AGENTTEAMS_RECONCILE_MANIFESTS:-$(dotenv_value AGENTTEAMS_RECONCILE_MANIFESTS)}"
reconcile_manifests="${reconcile_manifests:-0}"
[[ "${reconcile_manifests}" == "0" || "${reconcile_manifests}" == "1" ]] \
  || fail "AGENTTEAMS_RECONCILE_MANIFESTS must be 0 or 1"

team_already_exists=false
if docker exec "${controller_container}" agt get teams chengzhu-research-team >/dev/null 2>&1; then
  team_already_exists=true
fi

if [[ "${team_already_exists}" == "false" ]]; then
  "${script_dir}/apply-manifests.sh"
elif [[ "${reconcile_manifests}" == "1" ]]; then
  workers_json="$(docker exec "${controller_container}" agt get workers --team chengzhu-research-team -o json)"
  jq -e '
    [.workers[]
      | select(.name != "research-lead")
      | (.phase | ascii_downcase)
      | select(. == "running" or . == "ready" or . == "starting")]
    | length == 0
  ' <<< "${workers_json}" >/dev/null \
    || fail "cannot reconcile Worker manifests while a non-leader Worker is active"
  "${compose[@]}" exec -T backend python - <<'PY' \
    || fail "cannot reconcile Worker manifests while a Chengzhu Team run is active"
from app.utils.db import get_connection

with get_connection() as connection:
    active = connection.execute(
        """SELECT COUNT(*) FROM agent_team_run
           WHERE status NOT IN ('published', 'rejected_terminal', 'failed')"""
    ).fetchone()[0]
if int(active):
    raise SystemExit('active_agent_team_runs')
PY
  "${script_dir}/apply-manifests.sh"
else
  printf 'Existing Chengzhu Team detected; skipping manifest replay to preserve live DAG state.\n'
  docker exec "${controller_container}" agt worker ensure-ready --name research-lead >/dev/null \
    || fail "failed to restore the research-lead after a safe competition-down"
fi
AGENTTEAMS_MCP_GATEWAY_TOKEN_FILE="${mcp_token_file}" \
  "${script_dir}/configure-higress-mcp.sh"

# Higress v1.2.0 resolves DNS service sources when its embedded gateway starts
# but can retain a removed Compose container's address after the MCP container
# is recreated. Refresh only when the observed upstream address changes so a
# routine idempotent startup does not interrupt the control plane.
if [[ "${refresh_higress_dns}" == "true" ]]; then
  printf 'Chengzhu MCP address changed; refreshing the embedded Higress gateway.\n'
  docker restart "${controller_container}" >/dev/null
  wait_control_plane \
    || fail "AgentTeams control plane did not recover after refreshing Higress DNS"
  docker exec "${controller_container}" agt worker ensure-ready --name research-lead >/dev/null \
    || fail "failed to restore research-lead after refreshing Higress DNS"
fi
"${script_dir}/competition-preflight.sh"

temporary_mcp_upstream_ip="${mcp_upstream_ip_file}.tmp.$$"
umask 077
printf '%s\n' "${mcp_upstream_ip}" > "${temporary_mcp_upstream_ip}"
chmod 0600 "${temporary_mcp_upstream_ip}"
mv "${temporary_mcp_upstream_ip}" "${mcp_upstream_ip_file}"

printf 'Chengzhu AgentTeams competition stack is ready.\n'
