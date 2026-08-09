#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agentteams_dir="$(cd "${script_dir}/.." && pwd)"
manifests_dir="${agentteams_dir}/manifests"
dist_dir="${agentteams_dir}/dist"

readonly pinned_version="v1.2.0"
readonly controller_default="agentteams-controller"
readonly manager_default="agentteams-manager"
readonly remote_stage_dir="/tmp/chengzhu-agentteams-v1.2.0"
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

bash "${script_dir}/verify.sh"

container_cli="${AGENTTEAMS_CONTAINER_CLI:-}"
if [[ -z "${container_cli}" ]]; then
  if command -v docker >/dev/null 2>&1; then
    container_cli="docker"
  elif command -v podman >/dev/null 2>&1; then
    container_cli="podman"
  else
    fail "docker or podman is required"
  fi
fi
[[ "${container_cli}" == "docker" || "${container_cli}" == "podman" ]] || fail "AGENTTEAMS_CONTAINER_CLI must be docker or podman"
command -v "${container_cli}" >/dev/null 2>&1 || fail "container CLI not found: ${container_cli}"

controller_container="${AGENTTEAMS_CONTROLLER_CONTAINER:-${controller_default}}"
manager_container="${AGENTTEAMS_MANAGER_CONTAINER:-${manager_default}}"
[[ "${controller_container}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail "unsafe controller container name: ${controller_container}"
[[ "${manager_container}" =~ ^[A-Za-z0-9][A-Za-z0-9_.-]*$ ]] || fail "unsafe manager container name: ${manager_container}"
"${container_cli}" inspect "${controller_container}" >/dev/null 2>&1 || fail "controller container not found: ${controller_container}"
"${container_cli}" inspect "${manager_container}" >/dev/null 2>&1 || fail "manager container not found: ${manager_container}"

controller_running="$("${container_cli}" inspect --format '{{.State.Running}}' "${controller_container}")"
manager_running="$("${container_cli}" inspect --format '{{.State.Running}}' "${manager_container}")"
[[ "${controller_running}" == "true" ]] || fail "controller container is not running: ${controller_container}"
[[ "${manager_running}" == "true" ]] || fail "manager container is not running: ${manager_container}"

controller_image="$("${container_cli}" inspect --format '{{.Config.Image}}' "${controller_container}")"
manager_image="$("${container_cli}" inspect --format '{{.Config.Image}}' "${manager_container}")"
case "${controller_image}" in
  *agentteams-embedded:"${pinned_version}"@sha256:c0de550018e51b36138a5990b1e8095eacc9d44cc7cbdb36a697785ba02c9be4) ;;
  *) fail "controller image must be tagged ${pinned_version}; got ${controller_image}" ;;
esac
case "${manager_image}" in
  *agentteams-manager:"${pinned_version}"@sha256:a429666fcc66fa01b81da3bc9ea5af98437697ef794f811ac7d358d4286c317e) ;;
  *) fail "manager image must be tagged ${pinned_version}; got ${manager_image}" ;;
esac

"${container_cli}" exec "${controller_container}" agt get managers default >/dev/null

"${container_cli}" exec "${controller_container}" mkdir -p "${remote_stage_dir}"

printf 'Applying eight pinned Worker manifests...\n'
for worker_name in "${worker_names[@]}"; do
  worker_manifest="${manifests_dir}/workers/${worker_name}.yaml"
  remote_manifest="${remote_stage_dir}/${worker_name}.yaml"
  "${container_cli}" cp "${worker_manifest}" "${controller_container}:${remote_manifest}"
  "${container_cli}" exec "${controller_container}" agt apply -f "${remote_manifest}"
done

printf 'Uploading eight content-addressed Worker packages...\n'
for worker_name in "${worker_names[@]}"; do
  worker_archive="${dist_dir}/${worker_name}.zip"
  remote_archive="${remote_stage_dir}/${worker_name}.zip"
  [[ -f "${worker_archive}" ]] || fail "missing package: ${worker_archive}"
  "${container_cli}" cp "${worker_archive}" "${controller_container}:${remote_archive}"
  "${container_cli}" exec "${controller_container}" agt apply worker \
    --name "${worker_name}" \
    --zip "${remote_archive}" \
    --runtime copaw
done

printf 'Applying Team manifest after all Workers and packages...\n'
remote_team_manifest="${remote_stage_dir}/team.yaml"
"${container_cli}" cp "${manifests_dir}/team.yaml" "${controller_container}:${remote_team_manifest}"
"${container_cli}" exec "${controller_container}" agt apply -f "${remote_team_manifest}"

printf '\nDeclared state:\n'
"${container_cli}" exec "${controller_container}" agt get teams chengzhu-research-team
"${container_cli}" exec "${controller_container}" agt get workers --team chengzhu-research-team
printf '\nApply completed. Worker readiness and TeamHarness model calls converge asynchronously; capture runtime evidence before submission.\n'
