#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agentteams_dir="$(cd "${script_dir}/.." && pwd)"
target_root="${agentteams_dir}/vendor/alibabacloud-bailian-image-creator"
readonly commit="92bd723f7cc217b252feab574c1883fa0aa46b3c"
readonly repository="https://raw.githubusercontent.com/aliyun/alibabacloud-aiops-skills"
readonly skill_path="skills/aiml/sfm/alibabacloud-bailian-image-creator"

fail() {
  printf 'ERROR: %s\n' "$*" >&2
  exit 1
}

for required_command in curl mkdir mktemp awk; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done

if command -v shasum >/dev/null 2>&1; then
  checksum() { shasum -a 256 "$1" | awk '{print $1}'; }
elif command -v sha256sum >/dev/null 2>&1; then
  checksum() { sha256sum "$1" | awk '{print $1}'; }
else
  fail "missing SHA-256 tool: install shasum or sha256sum"
fi

fetch_one() {
  local relative="$1"
  local expected="$2"
  local destination="${target_root}/${relative}"
  local parent
  parent="$(dirname "${destination}")"
  mkdir -p "${parent}"

  if [[ -f "${destination}" ]]; then
    [[ "$(checksum "${destination}")" == "${expected}" ]] \
      || fail "existing official Skill file has unexpected checksum: ${relative}"
    return
  fi

  local temporary
  temporary="$(mktemp "${parent}/.fetch.XXXXXX")"
  curl \
    --proto '=https' \
    --tlsv1.2 \
    --fail \
    --silent \
    --show-error \
    --location \
    --retry 3 \
    --output "${temporary}" \
    "${repository}/${commit}/${skill_path}/${relative}"
  [[ "$(checksum "${temporary}")" == "${expected}" ]] \
    || fail "downloaded official Skill file failed checksum: ${relative}"
  chmod 0444 "${temporary}"
  mv "${temporary}" "${destination}"
}

fetch_one "SKILL.md" "840b9faf3205b93d65c8a4b76a342c10b3c35e622c5a47986e08d89c7be5c6d8"
fetch_one "scripts/image_understanding.py" "f424a10d07d978862da576af7d20efa5e43067e72dbb72e0d241fe56ea99dcb3"
fetch_one "scripts/api_key.py" "1cf3b28d63a29d7ceec7419ee2c5d546358d733500fd66f061ac5d55c3495106"
fetch_one "scripts/requirements.txt" "c69290e63c1bbcf71488fe9e7933f26eb0fc17c0179e97cf72213b3fa0ae0469"

printf 'Official Skill fetched and verified at commit %s.\n' "${commit}"
