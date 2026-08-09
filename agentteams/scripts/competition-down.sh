#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
runtime_dir="${project_root}/.agentteams"
project_env="${project_root}/.env"
manager_env="${AGENTTEAMS_ENV_FILE:-${runtime_dir}/agentteams-manager.env}"
owner_file="${runtime_dir}/owner"

readonly controller_container="agentteams-controller"
readonly manager_container="agentteams-manager"
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

warn() {
  printf 'WARNING: %s\n' "$*" >&2
}

for required_command in docker jq awk seq; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done
docker compose version >/dev/null 2>&1 || fail "Docker Compose v2 is required"

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

owner_controls_current_plane() {
  [[ -f "${owner_file}" && ! -L "${owner_file}" ]] || return 1
  [[ "$(owner_value schema)" == "chengzhu-agentteams-owner-v1" ]] || return 1
  [[ "$(owner_value project_root)" == "${project_root}" ]] || return 1
  [[ "$(owner_value owns_control_plane)" == "true" ]] || return 1
  [[ "$(owner_value controller_id)" =~ ^[a-f0-9]{64}$ ]] || return 1
  [[ "$(owner_value manager_id)" =~ ^[a-f0-9]{64}$ ]] || return 1
  [[ "$(docker inspect --format '{{.Id}}' "${controller_container}" 2>/dev/null)" == "$(owner_value controller_id)" ]] \
    || return 1
  [[ "$(docker inspect --format '{{.Id}}' "${manager_container}" 2>/dev/null)" == "$(owner_value manager_id)" ]]
}

compose=(docker compose)
if [[ -f "${project_env}" && ! -L "${project_env}" ]]; then
  compose+=(--env-file "${project_env}")
fi
if [[ -f "${manager_env}" && ! -L "${manager_env}" ]]; then
  compose+=(--env-file "${manager_env}")
fi
compose+=(
  -f "${project_root}/docker-compose.yml"
  -f "${project_root}/docker-compose.agentteams.yml"
)

app_network="$("${compose[@]}" config --format json 2>/dev/null | jq -er '.networks.default.name' 2>/dev/null || true)"

sleep_failed=false
controller_running="$(docker inspect --format '{{.State.Running}}' "${controller_container}" 2>/dev/null || true)"
if [[ "${controller_running}" == "true" ]]; then
  team_json="$(docker exec "${controller_container}" agt get teams chengzhu-research-team -o json 2>/dev/null || true)"
  if ! jq -e '
    .name == "chengzhu-research-team"
    and .leaderName == "research-lead"
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
    warn "Team roster could not be proven to be the fixed Chengzhu eight; no Worker was modified"
    sleep_failed=true
  else
    for worker_name in "${worker_names[@]}"; do
      if ! docker exec "${controller_container}" agt worker sleep --name "${worker_name}" >/dev/null; then
        warn "failed to put Worker ${worker_name} to sleep"
        sleep_failed=true
      fi
    done
  fi
else
  for worker_name in "${worker_names[@]}"; do
    if [[ "$(docker inspect --format '{{.State.Running}}' "agentteams-worker-${worker_name}" 2>/dev/null || true)" == "true" ]]; then
      warn "${controller_container} is unavailable while ${worker_name} is still running"
      sleep_failed=true
    fi
  done
fi

if [[ -n "${app_network}" ]] \
  && docker inspect "${controller_container}" >/dev/null 2>&1 \
  && docker inspect "${controller_container}" | jq -e --arg network "${app_network}" \
    '.[0].NetworkSettings.Networks[$network] != null' >/dev/null 2>&1; then
  docker network disconnect "${app_network}" "${controller_container}"
fi

"${compose[@]}" down

if [[ "${sleep_failed}" == "false" && "${controller_running}" == "true" ]]; then
  workers_stopped=false
  for _attempt in $(seq 1 30); do
    workers_stopped=true
    for worker_name in "${worker_names[@]}"; do
      if [[ "$(docker inspect --format '{{.State.Running}}' "agentteams-worker-${worker_name}" 2>/dev/null || true)" == "true" ]]; then
        workers_stopped=false
        break
      fi
    done
    [[ "${workers_stopped}" == "true" ]] && break
    sleep 2
  done
  if [[ "${workers_stopped}" != "true" ]]; then
    warn "one or more Chengzhu Workers did not reach the stopped state"
    sleep_failed=true
  fi
fi

if owner_controls_current_plane; then
  if [[ "${sleep_failed}" == "false" ]]; then
    docker stop "${controller_container}" >/dev/null
    docker stop "${manager_container}" >/dev/null
    printf 'Stopped the Chengzhu-owned AgentTeams control plane without removing containers or data.\n'
  else
    warn "leaving the owned control plane running because Worker sleep was not fully verified"
  fi
else
  printf 'AgentTeams control plane is shared or unowned by this project; leaving it running.\n'
fi

if [[ "${sleep_failed}" == "true" ]]; then
  fail "competition services stopped, but AgentTeams Worker shutdown requires attention"
fi

printf 'Chengzhu competition services are down; no AgentTeams data was deleted.\n'
