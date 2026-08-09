#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd "${script_dir}/../.." && pwd)"
runtime_dir="${project_root}/.agentteams"
manager_env="${AGENTTEAMS_ENV_FILE:-${runtime_dir}/agentteams-manager.env}"
token_file="${AGENTTEAMS_MCP_GATEWAY_TOKEN_FILE:-${runtime_dir}/chengzhu-mcp-token}"
readonly -a roles=(
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

for required_command in curl jq mktemp awk; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done
[[ -f "${manager_env}" && ! -L "${manager_env}" ]] || fail "missing AgentTeams environment file"
[[ -f "${token_file}" && ! -L "${token_file}" ]] || fail "missing Chengzhu MCP service token file"

env_value() {
  local key="$1"
  awk -F= -v wanted="${key}" '$1 == wanted {sub(/^[^=]*=/, ""); print; exit}' "${manager_env}"
}

admin_user="${AGENTTEAMS_ADMIN_USER:-$(env_value AGENTTEAMS_ADMIN_USER)}"
admin_password="${AGENTTEAMS_ADMIN_PASSWORD:-$(env_value AGENTTEAMS_ADMIN_PASSWORD)}"
console_port="${AGENTTEAMS_PORT_CONSOLE:-$(env_value AGENTTEAMS_PORT_CONSOLE)}"
console_port="${console_port:-18001}"
service_token="$(tr -d '\r\n' < "${token_file}")"
mcp_timeout_ms="${AGENTTEAMS_MCP_TIMEOUT_MS:-470000}"

[[ "${admin_user}" =~ ^[A-Za-z0-9_.@-]{1,80}$ ]] || fail "invalid AgentTeams admin user"
[[ ${#admin_password} -ge 8 ]] || fail "invalid AgentTeams admin password"
[[ "${console_port}" =~ ^[0-9]{2,5}$ ]] || fail "invalid Higress console port"
[[ "${service_token}" =~ ^[A-Za-z0-9_.:-]{32,200}$ ]] || fail "invalid MCP service token"
[[ "${mcp_timeout_ms}" =~ ^[0-9]+$ ]] || fail "invalid MCP timeout"
(( mcp_timeout_ms >= 70000 && mcp_timeout_ms <= 480000 )) \
  || fail "AGENTTEAMS_MCP_TIMEOUT_MS must be between 70000 and 480000"

cookie_file="$(mktemp "${TMPDIR:-/tmp}/chengzhu-higress-cookie.XXXXXX")"
response_file="$(mktemp "${TMPDIR:-/tmp}/chengzhu-higress-response.XXXXXX")"
cleanup() {
  rm -f -- "${cookie_file}" "${response_file}"
}
trap cleanup EXIT HUP INT TERM

console_url="http://127.0.0.1:${console_port}"
login_body="$(jq -cn --arg username "${admin_user}" --arg password "${admin_password}" '{username:$username,password:$password}')"
curl --fail --silent --show-error \
  --request POST "${console_url}/session/login" \
  --header 'Content-Type: application/json' \
  --cookie-jar "${cookie_file}" \
  --data "${login_body}" \
  --output "${response_file}"

api_write() {
  local method="$1"
  local path="$2"
  local body="$3"
  local status
  status="$(curl --silent --show-error \
    --request "${method}" "${console_url}${path}" \
    --header 'Content-Type: application/json' \
    --cookie "${cookie_file}" \
    --data "${body}" \
    --output "${response_file}" \
    --write-out '%{http_code}')"
  case "${status}" in
    200|201|204) ;;
    # Creating the DNS service source is intentionally idempotent. Higress
    # reports an existing source as 409 and may include success=false in that
    # response body; that is not a failed configuration on a rerun.
    409) return ;;
    *) fail "Higress API ${path} returned HTTP ${status}" ;;
  esac
  if jq -e '.success == false' "${response_file}" >/dev/null 2>&1; then
    fail "Higress API ${path} rejected the configuration"
  fi
}

service_source_name='chengzhu-mcp-proxy'
service_source='{"type":"dns","name":"chengzhu-mcp-proxy","domain":"chengzhu-mcp.agentteams.io","port":5002,"protocol":"http"}'
source_lookup_status="$(curl --silent --show-error \
  --request GET "${console_url}/v1/service-sources/${service_source_name}" \
  --cookie "${cookie_file}" \
  --output "${response_file}" \
  --write-out '%{http_code}')"
case "${source_lookup_status}" in
  200)
    service_source_version="$(jq -er '(.data // .).version' "${response_file}")" \
      || fail "existing Higress Chengzhu service source has no version"
    service_source="$(jq -c --arg version "${service_source_version}" \
      '. + {version:$version}' <<< "${service_source}")"
    api_write PUT "/v1/service-sources/${service_source_name}" "${service_source}"
    ;;
  404)
    api_write POST /v1/service-sources "${service_source}"
    ;;
  *)
    fail "Higress API /v1/service-sources/${service_source_name} returned HTTP ${source_lookup_status}"
    ;;
esac

for role in "${roles[@]}"; do
  server_name="mcp-chengzhu-${role}"
  upstream_url="http://chengzhu-mcp.agentteams.io:5002/mcp/${role}"
  # AgentTeams v1.2.0's embedded Higress SDK enables an OPEN_API MCP
  # instance only when rawConfigurations contains the literal `tools:`.
  # An empty list preserves mcp-proxy pass-through semantics while avoiding
  # the SDK incorrectly emitting configDisable: true for proxy-only routes.
  raw_configuration="$(jq -rn \
    --arg role "${role}" \
    --arg upstream "${upstream_url}" \
    --arg token "${service_token}" \
    --arg timeout "${mcp_timeout_ms}" \
    '"server:\n  name: chengzhu-" + $role + "-mcp-server\n  type: mcp-proxy\n  transport: http\n  mcpServerURL: \"" + $upstream + "\"\n  timeout: " + $timeout + "\n  securitySchemes:\n  - id: UpstreamAuth\n    type: http\n    scheme: bearer\n    defaultCredential: \"" + $token + "\"\n  defaultUpstreamSecurity:\n    id: UpstreamAuth\ntools: []"')"
  mcp_body="$(jq -cn \
    --arg name "${server_name}" \
    --arg raw "${raw_configuration}" \
    --arg consumer "worker-${role}" \
    '{
      name:$name,
      description:("Chengzhu role-scoped MCP proxy for " + $consumer),
      type:"OPEN_API",
      rawConfigurations:$raw,
      mcpServerName:$name,
      domains:["aigw-local.agentteams.io"],
      services:[{name:"chengzhu-mcp-proxy.dns",port:5002,weight:100}],
      consumerAuthInfo:{type:"key-auth",enable:true,allowedConsumers:[$consumer]}
    }')"
  api_write PUT /v1/mcpServer "${mcp_body}"
  consumers_body="$(jq -cn \
    --arg name "${server_name}" \
    --arg consumer "worker-${role}" \
    '{mcpServerName:$name,consumers:[$consumer]}')"
  api_write PUT /v1/mcpServer/consumers "${consumers_body}"
done

printf 'Configured eight role-scoped Chengzhu MCP routes in Higress.\n'
