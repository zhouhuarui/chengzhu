#!/usr/bin/env bash
set -euo pipefail

readonly AGENTTEAMS_PINNED_VERSION="v1.2.0"
readonly AGENTTEAMS_INSTALLER_URL="https://raw.githubusercontent.com/agentscope-ai/AgentTeams/v1.2.0/install/agentteams-install.sh"
readonly AGENTTEAMS_INSTALLER_SHA256="701f53c53dc476d8ca7f33428e231c1706d967ac2b517ec4c1c59d742864331d"
readonly AGENTTEAMS_EMBEDDED_IMAGE="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-embedded:v1.2.0@sha256:c0de550018e51b36138a5990b1e8095eacc9d44cc7cbdb36a697785ba02c9be4"
readonly AGENTTEAMS_MANAGER_OPENCLAW_IMAGE="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-manager:v1.2.0@sha256:a429666fcc66fa01b81da3bc9ea5af98437697ef794f811ac7d358d4286c317e"
readonly AGENTTEAMS_COPAW_IMAGE="higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-copaw-worker:v1.2.0@sha256:dcdd9103535cfac247267e0f69661820c801396d58e2c8e0c14eefd40b63b7bc"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for required_command in curl bash mktemp awk grep chmod; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done

if [[ -n "${AGENTTEAMS_VERSION:-}" && "${AGENTTEAMS_VERSION}" != "${AGENTTEAMS_PINNED_VERSION}" ]]; then
  fail "AGENTTEAMS_VERSION must be ${AGENTTEAMS_PINNED_VERSION}; got ${AGENTTEAMS_VERSION}"
fi
if [[ -n "${AGENTTEAMS_MANAGER_RUNTIME:-}" && "${AGENTTEAMS_MANAGER_RUNTIME}" != "openclaw" ]]; then
  fail "competition Manager runtime must be openclaw"
fi
if [[ -n "${AGENTTEAMS_DEFAULT_WORKER_RUNTIME:-}" && "${AGENTTEAMS_DEFAULT_WORKER_RUNTIME}" != "copaw" ]]; then
  fail "competition Worker runtime must be copaw (QwenPaw)"
fi

readonly -a disallowed_image_overrides=(
  AGENTTEAMS_INSTALL_CONTROLLER_IMAGE
  AGENTTEAMS_INSTALL_COPAW_WORKER_IMAGE
  AGENTTEAMS_INSTALL_DOCKER_PROXY_IMAGE
  AGENTTEAMS_INSTALL_EMBEDDED_IMAGE
  AGENTTEAMS_INSTALL_HERMES_WORKER_IMAGE
  AGENTTEAMS_INSTALL_MANAGER_COPAW_IMAGE
  AGENTTEAMS_INSTALL_MANAGER_IMAGE
  AGENTTEAMS_INSTALL_WORKER_IMAGE
)
for override_name in "${disallowed_image_overrides[@]}"; do
  if [[ -n "${!override_name:-}" ]]; then
    fail "${override_name} is not allowed by the pinned competition installer"
  fi
done

install_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/chengzhu-agentteams-install.XXXXXX")"
installer_path="${install_tmp_dir}/agentteams-install.sh"
sanitized_installer_path="${install_tmp_dir}/agentteams-install-no-unsafe-tee.sh"

cleanup() {
  rm -f -- "${installer_path}" "${sanitized_installer_path}"
  rmdir -- "${install_tmp_dir}" 2>/dev/null || true
}
trap cleanup EXIT HUP INT TERM

curl \
  --proto '=https' \
  --tlsv1.2 \
  --fail \
  --silent \
  --show-error \
  --location \
  --retry 3 \
  --output "${installer_path}" \
  "${AGENTTEAMS_INSTALLER_URL}"

if command -v shasum >/dev/null 2>&1; then
  actual_sha256="$(shasum -a 256 "${installer_path}" | awk '{print $1}')"
elif command -v sha256sum >/dev/null 2>&1; then
  actual_sha256="$(sha256sum "${installer_path}" | awk '{print $1}')"
else
  fail "missing SHA-256 tool: install shasum or sha256sum"
fi

if [[ "${actual_sha256}" != "${AGENTTEAMS_INSTALLER_SHA256}" ]]; then
  fail "installer checksum mismatch; expected ${AGENTTEAMS_INSTALLER_SHA256}, got ${actual_sha256}"
fi

# The checksum-locked upstream script tees all output to a fixed, potentially
# world-readable $HOME log and later prints the generated admin password.
# Disable that unsafe tee, and pass the upstream-supported per-call maxTokens
# setting into the embedded controller (the v1.2.0 installer writes it only to
# the Manager env file). The wrapper below still emits a redacted stream.
unsafe_tee='    exec > >(tee -a "${AGENTTEAMS_LOG_FILE}") 2>&1'
[[ "$(grep -Fxc "${unsafe_tee}" "${installer_path}")" == "1" ]] \
  || fail "pinned installer logging contract changed"
controller_model_anchor='            -e "${_ctrl_env_prefix}DEFAULT_MODEL=${AGENTTEAMS_DEFAULT_MODEL}"'
[[ "$(grep -Fxc "${controller_model_anchor}" "${installer_path}")" == "1" ]] \
  || fail "pinned installer controller model contract changed"
socket_detection_anchor='        CONTAINER_SOCK=$(detect_socket)'
[[ "$(grep -Fxc "${socket_detection_anchor}" "${installer_path}")" == "1" ]] \
  || fail "pinned installer socket detection contract changed"
awk \
  -v unsafe="${unsafe_tee}" \
  -v model_anchor="${controller_model_anchor}" \
  -v socket_anchor="${socket_detection_anchor}" '
  {
    if ($0 == unsafe) {
      print "    exec 2>&1"
      next
    }
    print
    if ($0 == socket_anchor) {
      print "        # Colima exposes a host-side client socket, but bind sources are"
      print "        # resolved inside its Linux VM where the daemon socket lives here."
      print "        if [ \"$(uname -s)\" = \"Darwin\" ]; then"
      print "            case \"${CONTAINER_SOCK}\" in"
      print "                \"${HOME}\"/.colima/*/docker.sock) CONTAINER_SOCK=/var/run/docker.sock ;;"
      print "            esac"
      print "        fi"
    }
    if ($0 == model_anchor) {
      print "            -e \"${_ctrl_env_prefix}MODEL_MAX_TOKENS=${AGENTTEAMS_MODEL_MAX_TOKENS:-}\""
    }
  }
' \
  "${installer_path}" > "${sanitized_installer_path}"
chmod 0700 "${sanitized_installer_path}"

AGENTTEAMS_MODEL_MAX_TOKENS="${AGENTTEAMS_MODEL_MAX_TOKENS:-4096}"
[[ "${AGENTTEAMS_MODEL_MAX_TOKENS}" =~ ^[0-9]+$ ]] \
  || fail "AGENTTEAMS_MODEL_MAX_TOKENS must be a positive integer"
(( AGENTTEAMS_MODEL_MAX_TOKENS >= 1 && AGENTTEAMS_MODEL_MAX_TOKENS <= 131072 )) \
  || fail "AGENTTEAMS_MODEL_MAX_TOKENS must be between 1 and 131072"

export AGENTTEAMS_VERSION="${AGENTTEAMS_PINNED_VERSION}"
export AGENTTEAMS_MANAGER_RUNTIME="openclaw"
export AGENTTEAMS_DEFAULT_WORKER_RUNTIME="copaw"
export AGENTTEAMS_MODEL_MAX_TOKENS
export AGENTTEAMS_UPGRADE_KEEP_ALL=1
export AGENTTEAMS_INSTALL_EMBEDDED_IMAGE="${AGENTTEAMS_EMBEDDED_IMAGE}"
export AGENTTEAMS_INSTALL_MANAGER_IMAGE="${AGENTTEAMS_MANAGER_OPENCLAW_IMAGE}"
export AGENTTEAMS_INSTALL_COPAW_WORKER_IMAGE="${AGENTTEAMS_COPAW_IMAGE}"
printf 'Verified AgentTeams installer %s (%s).\n' "${AGENTTEAMS_VERSION}" "${actual_sha256}"
bash "${sanitized_installer_path}" "$@" 2>&1 | awk '
  {
    plain=$0
    gsub(/\033\[[0-9;]*[[:alpha:]]/, "", plain)
    lower=tolower(plain)
    if (lower ~ /(password|secret|api[ _-]*key|access[ _-]*token)/ || plain ~ /(密码|密钥|令牌)/) {
      print "[AgentTeams installer credential output redacted]"
      next
    }
    gsub(/admin[[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]][[:xdigit:]]/, "[REDACTED]", $0)
    print
  }
'
