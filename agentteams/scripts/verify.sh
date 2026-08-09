#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agentteams_dir="$(cd "${script_dir}/.." && pwd)"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for required_command in bash ruby find grep unzip; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done

while IFS= read -r shell_script; do
  bash -n "${shell_script}"
  if [[ "${shell_script}" != "${script_dir}/verify.sh" ]] && grep -En 'curl[^|]*\|[[:space:]]*(ba)?sh' "${shell_script}" >/dev/null; then
    fail "unsafe download-to-shell pipeline in ${shell_script}"
  fi
done < <(find "${script_dir}" -maxdepth 1 -type f -name '*.sh' -print | LC_ALL=C sort)

ruby - "${agentteams_dir}" <<'RUBY'
require "json"
require "yaml"
require "pathname"

ROOT = Pathname.new(ARGV.fetch(0)).realpath
EXPECTED_VERSION = "v1.2.0"
EXPECTED_API = "agentteams.io/v1beta1"
EXPECTED_RUNTIME = "copaw"
EXPECTED_IMAGE = "higress-registry.cn-hangzhou.cr.aliyuncs.com/agentteams/agentteams-copaw-worker:v1.2.0@sha256:dcdd9103535cfac247267e0f69661820c801396d58e2c8e0c14eefd40b63b7bc"
EXPECTED_INSTALLER_URL = "https://raw.githubusercontent.com/agentscope-ai/AgentTeams/v1.2.0/install/agentteams-install.sh"
EXPECTED_INSTALLER_SHA = "701f53c53dc476d8ca7f33428e231c1706d967ac2b517ec4c1c59d742864331d"

SKILLS = {
  "research-lead" => "plan-research",
  "disclosure-researcher" => "collect-disclosures",
  "market-context-researcher" => "collect-market-context",
  "quality-analyst" => "analyze-quality",
  "growth-analyst" => "analyze-growth",
  "evidence-judge" => "judge-evidence",
  "report-writer" => "write-evidence-report",
  "compliance-reviewer" => "review-research-output"
}.freeze

MODELS = {
  "research-lead" => "qwen3-30b-a3b-instruct-2507",
  "disclosure-researcher" => "qwen3-30b-a3b-instruct-2507",
  "market-context-researcher" => "qwen3-30b-a3b-instruct-2507",
  "quality-analyst" => "qwen3.5-plus",
  "growth-analyst" => "qwen3.5-plus",
  "evidence-judge" => "qwen3.5-plus",
  "report-writer" => "qwen3-30b-a3b-instruct-2507",
  "compliance-reviewer" => "qwen3-30b-a3b-instruct-2507"
}.freeze

REQUIRED_SKILL_SECTIONS = [
  "## 触发条件",
  "## 输入",
  "## 输出",
  "## 工作流",
  "## 失败处理",
  "## 安全边界",
  "## 复用价值"
].freeze

def assert(condition, message)
  raise message unless condition
end

def load_yaml(path)
  YAML.safe_load(path.read, [], [], false)
rescue StandardError => e
  raise "invalid YAML #{path}: #{e.message}"
end

def load_json(path)
  JSON.parse(path.read)
rescue StandardError => e
  raise "invalid JSON #{path}: #{e.message}"
end

assert((ROOT / "VERSION").read.strip == EXPECTED_VERSION, "VERSION is not pinned to #{EXPECTED_VERSION}")
lock = (ROOT / "UPSTREAM.lock").read
assert(lock.include?("tag=#{EXPECTED_VERSION}"), "UPSTREAM.lock tag mismatch")
assert(lock.include?("installer_url=#{EXPECTED_INSTALLER_URL}"), "UPSTREAM.lock installer URL mismatch")
assert(lock.include?("installer_sha256=#{EXPECTED_INSTALLER_SHA}"), "UPSTREAM.lock installer checksum mismatch")
assert(lock.include?("copaw_worker_image=#{EXPECTED_IMAGE}"), "UPSTREAM.lock CoPaw/QwenPaw image mismatch")
assert(lock.include?("teamharness_plugin_version=0.1.0"), "UPSTREAM.lock TeamHarness plugin mismatch")

worker_paths = Dir[(ROOT / "manifests/workers/*.yaml").to_s].sort.map { |p| Pathname.new(p) }
assert(worker_paths.length == 8, "expected 8 Worker manifests, got #{worker_paths.length}")

workers = {}
identity_fields = ["Name:", "Role:", "Capabilities:", "Inputs:", "Outputs:", "Dependencies:", "Decision Boundary:", "Trace:"]
worker_paths.each do |path|
  document = load_yaml(path)
  assert(document["apiVersion"] == EXPECTED_API, "#{path}: apiVersion mismatch")
  assert(document["kind"] == "Worker", "#{path}: kind must be Worker")
  name = document.dig("metadata", "name")
  assert(name.is_a?(String) && !name.empty?, "#{path}: missing metadata.name")
  assert(path.basename(".yaml").to_s == name, "#{path}: filename/name mismatch")
  assert(!workers.key?(name), "duplicate Worker #{name}")
  spec = document.fetch("spec")
  assert(spec["model"] == MODELS.fetch(name), "#{name}: model profile mismatch")
  assert(spec["runtime"] == EXPECTED_RUNTIME, "#{name}: runtime must be #{EXPECTED_RUNTIME}")
  assert(spec["image"] == EXPECTED_IMAGE, "#{name}: image must be the pinned v1.2.0 CoPaw/QwenPaw image")
  expected_state = name == "research-lead" ? "Running" : "Sleeping"
  assert(spec["state"] == expected_state, "#{name}: initial lifecycle state must be #{expected_state}")
  assert(!spec.key?("package"), "#{name}: package URI must be assigned by ZIP upload, not hard-coded")
  mcp_servers = spec["mcpServers"]
  assert(mcp_servers.is_a?(Array) && mcp_servers.length == 1, "#{name}: exactly one Chengzhu MCP route is required")
  mcp = mcp_servers.first
  expected_mcp_url = "http://aigw-local.agentteams.io:8080/mcp-servers/mcp-chengzhu-#{name}/mcp"
  assert(mcp["name"] == "chengzhu", "#{name}: MCP alias must be chengzhu")
  assert(mcp["url"] == expected_mcp_url, "#{name}: MCP route is not role-scoped")
  assert(mcp["transport"] == "http", "#{name}: MCP transport must be http")
  identity = spec["identity"].to_s
  identity_fields.each { |field| assert(identity.include?(field), "#{name}: identity missing #{field}") }
  assert(spec.dig("resources", "requests", "cpu") == "200m", "#{name}: CPU request mismatch")
  assert(spec.dig("resources", "requests", "memory") == "512Mi", "#{name}: memory request mismatch")
  assert(spec.dig("resources", "limits", "cpu") == "1", "#{name}: CPU limit mismatch")
  assert(spec.dig("resources", "limits", "memory") == "2Gi", "#{name}: memory limit mismatch")
  workers[name] = document
end
assert(workers.keys.sort == SKILLS.keys.sort, "Worker manifest set does not match expected eight roles")

team = load_yaml(ROOT / "manifests/team.yaml")
assert(team["apiVersion"] == EXPECTED_API, "Team apiVersion mismatch")
assert(team["kind"] == "Team", "Team kind mismatch")
assert(team.dig("metadata", "name") == "chengzhu-research-team", "Team name mismatch")
members = team.dig("spec", "workerMembers")
assert(members.is_a?(Array) && members.length == 8, "Team must reference eight Workers")
member_names = members.map { |m| m["name"] }
assert(member_names.sort == workers.keys.sort, "Team member set differs from Worker manifests")
assert(member_names.uniq.length == 8, "Team has duplicate members")
leaders = members.select { |m| m["role"] == "team_leader" }
assert(leaders.length == 1 && leaders.first["name"] == "research-lead", "Team must have exactly one research-lead")
assert(members.reject { |m| m["role"] == "team_leader" }.all? { |m| m["role"] == "worker" }, "non-leader roles must be worker")
assert(team.dig("spec", "peerMentions") == true, "Team peerMentions must be true")

role_dirs = Dir.children(ROOT / "roles").select { |entry| (ROOT / "roles" / entry).directory? }.sort
assert(role_dirs == SKILLS.keys.sort, "role package directories do not match Worker set")

SKILLS.each do |worker_name, skill_name|
  role = ROOT / "roles" / worker_name
  manifest = load_json(role / "manifest.json")
  assert(manifest["type"] == "worker", "#{worker_name}: manifest type mismatch")
  assert(manifest["version"] == "1.0", "#{worker_name}: package schema version mismatch")
  assert(manifest.dig("worker", "suggested_name") == worker_name, "#{worker_name}: suggested_name mismatch")
  assert(manifest.dig("worker", "model") == MODELS.fetch(worker_name), "#{worker_name}: package model mismatch")
  assert(manifest.dig("worker", "runtime") == EXPECTED_RUNTIME, "#{worker_name}: package runtime mismatch")
  %w[apt_packages pip_packages npm_packages].each do |key|
    assert(manifest.dig("worker", key) == [], "#{worker_name}: #{key} must be an explicit empty list")
  end

  soul = role / "config/SOUL.md"
  agents = role / "config/AGENTS.md"
  assert(soul.file? && soul.size > 100, "#{worker_name}: missing or empty SOUL.md")
  assert(soul.read.include?("AI Agent"), "#{worker_name}: SOUL.md must disclose AI identity")
  assert(agents.file? && agents.size > 100, "#{worker_name}: missing or empty AGENTS.md")
  agents_text = agents.read
  ["manifest-registered `chengzhu` MCP", "worker-#{worker_name}", "#{worker_name}"].each do |contract|
    assert(agents_text.include?(contract), "#{worker_name}: AGENTS.md missing MCP contract #{contract}")
  end

  skill_dir = role / "skills" / skill_name
  skill_path = skill_dir / "SKILL.md"
  assert(skill_path.file?, "#{worker_name}: missing #{skill_name}/SKILL.md")
  skill_text = skill_path.read
  frontmatter_match = skill_text.match(/\A---\s*\n(.*?)\n---\s*\n/m)
  assert(frontmatter_match, "#{skill_path}: invalid frontmatter framing")
  frontmatter = YAML.safe_load(frontmatter_match[1], [], [], false)
  assert(frontmatter.is_a?(Hash), "#{skill_path}: frontmatter must be a mapping")
  assert(frontmatter.keys.sort == %w[description name], "#{skill_path}: frontmatter may contain only name and description")
  assert(frontmatter["name"] == skill_name, "#{skill_path}: skill name mismatch")
  assert(frontmatter["description"].is_a?(String) && frontmatter["description"].length >= 80, "#{skill_path}: description is not sufficiently informative")
  REQUIRED_SKILL_SECTIONS.each { |heading| assert(skill_text.include?(heading), "#{skill_path}: missing #{heading}") }
  ["manifest-registered `chengzhu` MCP", "worker-#{worker_name}", "#{worker_name}"].each do |contract|
    assert(skill_text.include?(contract), "#{skill_path}: missing MCP contract #{contract}")
  end

  openai_path = skill_dir / "agents/openai.yaml"
  openai = load_yaml(openai_path)
  interface = openai["interface"] || {}
  assert(interface["display_name"].is_a?(String), "#{openai_path}: missing display_name")
  assert(interface["short_description"].to_s.length.between?(25, 64), "#{openai_path}: short_description length must be 25-64")
  assert(interface["default_prompt"].to_s.include?("$#{skill_name}"), "#{openai_path}: default_prompt must mention $#{skill_name}")
end

create_payload = load_json(ROOT / "teamharness/create-project.json")
assert(create_payload["action"] == "create_project", "create-project action mismatch")
assert(create_payload.dig("payload", "projectId") == "${PROJECT_ID}", "create-project must use PROJECT_ID placeholder")

dag = load_json(ROOT / "teamharness/dag-plan.json")
assert(dag["action"] == "plan_dag", "DAG action must be plan_dag")
assert(dag.dig("payload", "projectId") == "${PROJECT_ID}", "DAG projectId placeholder mismatch")
tasks = dag.dig("payload", "tasks")
assert(tasks.is_a?(Array) && tasks.length == 9, "DAG must contain eight Worker tasks plus one backend system task")
task_by_id = tasks.to_h { |task| [task["taskId"], task] }
assert(task_by_id.length == 9, "DAG task IDs must be unique")

dag_contract = {
  "research-plan" => ["research-lead", []],
  "disclosure-research" => ["disclosure-researcher", ["research-plan"]],
  "market-context-research" => ["market-context-researcher", ["research-plan"]],
  "evidence-freeze" => ["chengzhu-backend", ["disclosure-research", "market-context-research"]],
  "quality-analysis" => ["quality-analyst", ["evidence-freeze"]],
  "growth-analysis" => ["growth-analyst", ["evidence-freeze"]],
  "evidence-judgement" => ["evidence-judge", ["quality-analysis", "growth-analysis"]],
  "report-draft" => ["report-writer", ["evidence-judgement"]],
  "compliance-review" => ["compliance-reviewer", ["report-draft"]]
}.freeze

expected_task_ids = dag_contract.keys.map { |key| "${PROJECT_ID}-#{key}" }
assert(task_by_id.keys.sort == expected_task_ids.sort, "DAG task ID set mismatch")
assert(tasks.map { |task| task["assignedTo"] }.sort == (SKILLS.keys + ["chengzhu-backend"]).sort, "DAG assignees must be the eight Workers plus the backend system node")
assert(!workers.key?("chengzhu-backend"), "chengzhu-backend must not be a Worker")

dag_contract.each do |task_key, contract|
  assignee, dependency_keys = contract
  task = task_by_id.fetch("${PROJECT_ID}-#{task_key}")
  assert(task["assignedTo"] == assignee, "#{task_key}: assignee mismatch")
  expected_dependencies = dependency_keys.map { |key| "${PROJECT_ID}-#{key}" }
  assert(task["dependsOn"].sort == expected_dependencies.sort, "#{task_key}: dependency mismatch")
end

tasks.each do |task|
  dependencies = task["dependsOn"]
  assert(dependencies.is_a?(Array), "#{task["taskId"]}: dependsOn must be a list")
  dependencies.each { |dep| assert(task_by_id.key?(dep), "#{task["taskId"]}: unknown dependency #{dep}") }
end

visiting = {}
visited = {}
visit = nil
visit = lambda do |task_id|
  raise "DAG cycle at #{task_id}" if visiting[task_id]
  return if visited[task_id]
  visiting[task_id] = true
  task_by_id.fetch(task_id)["dependsOn"].each { |dep| visit.call(dep) }
  visiting.delete(task_id)
  visited[task_id] = true
end
task_by_id.keys.each { |task_id| visit.call(task_id) }

direct_dag = load_json(ROOT / "teamharness/dag-plan-direct.json")
assert(direct_dag["action"] == "plan_dag", "direct DAG action must be plan_dag")
assert(direct_dag.dig("payload", "projectId") == "${PROJECT_ID}", "direct DAG projectId mismatch")
direct_tasks = direct_dag.dig("payload", "tasks")
assert(direct_tasks.is_a?(Array) && direct_tasks.length == 7, "direct DAG must contain seven executable nodes")
direct_by_id = direct_tasks.to_h { |task| [task["taskId"], task] }
direct_keys = dag_contract.keys - ["quality-analysis", "growth-analysis"]
assert(
  direct_by_id.keys.sort == direct_keys.map { |key| "${PROJECT_ID}-#{key}" }.sort,
  "direct DAG must omit only the two analyst nodes"
)
direct_judge = direct_by_id.fetch("${PROJECT_ID}-evidence-judgement")
assert(
  direct_judge["dependsOn"] == ["${PROJECT_ID}-evidence-freeze"],
  "direct Judge must depend directly on evidence-freeze"
)
direct_tasks.each do |task|
  task.fetch("dependsOn").each do |dependency|
    assert(direct_by_id.key?(dependency), "#{task["taskId"]}: unknown direct dependency #{dependency}")
  end
end

teams_policy = (ROOT / "teamharness/TEAMS.md").read
%w[create_project plan_dag ready_nodes accept_task_result].each do |contract|
  assert(teams_policy.include?(contract), "TEAMS.md missing TeamHarness contract #{contract}")
end
[
  "dag-plan-direct.json",
  "Leader-owned system bridge",
  "freeze_evidence",
  "accepted: true",
  "no `skipped` node state"
].each do |contract|
  assert(teams_policy.include?(contract), "TEAMS.md missing direct/system bridge contract #{contract}")
end

runtime_contract = (ROOT / "teamharness/runtime-contract.md").read
[
  "spec.mcpServers",
  "AGENTTEAMS_MCP_GATEWAY_TOKEN",
  "http://chengzhu-mcp.agentteams.io:5002",
  "chengzhu-backend"
].each do |contract|
  assert(runtime_contract.include?(contract), "runtime-contract.md missing #{contract}")
end
SKILLS.keys.each do |role_id|
  assert(runtime_contract.include?("/mcp/#{role_id}"), "runtime-contract.md missing role route #{role_id}")
end

backend_contract_path = ROOT.parent / "backend/app/team/contracts.py"
assert(backend_contract_path.file?, "backend Agent Team contract is missing")
backend_contract = backend_contract_path.read
SKILLS.keys.each { |role_id| assert(backend_contract.include?("'#{role_id}'"), "backend contract missing role #{role_id}") }
dag_contract.keys.each { |task_key| assert(backend_contract.include?("'#{task_key}'"), "backend contract missing task #{task_key}") }
assert(backend_contract.include?("SYSTEM_FREEZE_AGENT = 'chengzhu-backend'"), "backend freeze identity mismatch")

disclosure_skill = (ROOT / "roles/disclosure-researcher/skills/collect-disclosures/SKILL.md").read
[
  "alibabacloud-bailian-image-creator",
  "92bd723f7cc217b252feab574c1883fa0aa46b3c",
  "scripts/image_understanding.py",
  "DASHSCOPE_API_KEY",
  "visual_skill=degraded",
  "EvidenceGap",
  "fallback_reason"
].each do |contract|
  assert(disclosure_skill.include?(contract), "disclosure Skill missing Bailian/fallback contract #{contract}")
end

install_script = (ROOT / "scripts/install-agentteams-v1.2.0.sh").read
assert(install_script.include?(EXPECTED_INSTALLER_URL), "install script URL is not pinned")
assert(install_script.include?(EXPECTED_INSTALLER_SHA), "install script checksum is not pinned")
assert(install_script.include?("AGENTTEAMS_PINNED_VERSION=\"v1.2.0\""), "install script version is not pinned")
assert(install_script.include?("AGENTTEAMS_MANAGER_RUNTIME=\"openclaw\""), "Manager runtime must be OpenClaw")
assert(install_script.include?("AGENTTEAMS_DEFAULT_WORKER_RUNTIME=\"copaw\""), "default Worker runtime must be CoPaw/QwenPaw")

official_lock = (ROOT / "OFFICIAL_SKILLS.lock").read
[
  "name=alibabacloud-bailian-image-creator",
  "commit=92bd723f7cc217b252feab574c1883fa0aa46b3c",
  "skill_sha256=840b9faf3205b93d65c8a4b76a342c10b3c35e622c5a47986e08d89c7be5c6d8",
  "image_understanding_sha256=f424a10d07d978862da576af7d20efa5e43067e72dbb72e0d241fe56ea99dcb3"
].each do |contract|
  assert(official_lock.include?(contract), "official Skill lock missing #{contract}")
end

puts "Static contracts: OK (8 Workers, 1 Team, 8 Skills, 9-node debate + 7-node direct DAGs)"
RUBY

bash "${script_dir}/build-worker-packages.sh" >/dev/null
first_checksums="$(<"${agentteams_dir}/dist/SHA256SUMS")"
bash "${script_dir}/build-worker-packages.sh" >/dev/null
second_checksums="$(<"${agentteams_dir}/dist/SHA256SUMS")"
[[ "${first_checksums}" == "${second_checksums}" ]] || fail "Worker ZIP builds are not deterministic"

if command -v shasum >/dev/null 2>&1; then
  (cd "${agentteams_dir}/dist" && shasum -a 256 -c SHA256SUMS >/dev/null)
elif command -v sha256sum >/dev/null 2>&1; then
  (cd "${agentteams_dir}/dist" && sha256sum -c SHA256SUMS >/dev/null)
else
  fail "missing SHA-256 tool: install shasum or sha256sum"
fi

archive_count="$(find "${agentteams_dir}/dist" -maxdepth 1 -type f -name '*.zip' | wc -l | tr -d '[:space:]')"
[[ "${archive_count}" == "8" ]] || fail "expected 8 built ZIP packages, got ${archive_count}"

while IFS= read -r archive; do
  unzip -tq "${archive}" >/dev/null
  unzip -Z1 "${archive}" | grep -Fx 'manifest.json' >/dev/null || fail "${archive}: root manifest.json missing"
  unzip -Z1 "${archive}" | grep -E '^config/SOUL\.md$' >/dev/null || fail "${archive}: SOUL.md missing"
  unzip -Z1 "${archive}" | grep -E '^skills/[^/]+/SKILL\.md$' >/dev/null || fail "${archive}: custom SKILL.md missing"
done < <(find "${agentteams_dir}/dist" -maxdepth 1 -type f -name '*.zip' -print | LC_ALL=C sort)

printf 'Package contracts: OK (8 deterministic ZIPs; SHA256SUMS verified)\n'
printf 'All AgentTeams competition package checks passed.\n'
