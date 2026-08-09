#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
agentteams_dir="$(cd "${script_dir}/.." && pwd)"
roles_dir="${agentteams_dir}/roles"
dist_dir="${agentteams_dir}/dist"

readonly package_timestamp="202607300000"
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

for required_command in find sort zip unzip mktemp cp touch mv; do
  command -v "${required_command}" >/dev/null 2>&1 || fail "missing required command: ${required_command}"
done

if ! command -v shasum >/dev/null 2>&1 && ! command -v sha256sum >/dev/null 2>&1; then
  fail "missing SHA-256 tool: install shasum or sha256sum"
fi

build_tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/chengzhu-agentteams-build.XXXXXX")"
cleanup() {
  if [[ -n "${build_tmp_dir:-}" && -d "${build_tmp_dir}" ]]; then
    rm -rf -- "${build_tmp_dir}"
  fi
}
trap cleanup EXIT HUP INT TERM

mkdir -p "${dist_dir}"

for worker_name in "${worker_names[@]}"; do
  role_dir="${roles_dir}/${worker_name}"
  package_dir="${build_tmp_dir}/${worker_name}/package"
  file_list="${build_tmp_dir}/${worker_name}/files.txt"
  archive_tmp="${build_tmp_dir}/${worker_name}.zip"
  archive_final="${dist_dir}/${worker_name}.zip"

  [[ -f "${role_dir}/manifest.json" ]] || fail "missing ${role_dir}/manifest.json"
  [[ -f "${role_dir}/config/SOUL.md" ]] || fail "missing ${role_dir}/config/SOUL.md"
  [[ -f "${role_dir}/config/AGENTS.md" ]] || fail "missing ${role_dir}/config/AGENTS.md"

  mkdir -p "${package_dir}"
  cp -R "${role_dir}/." "${package_dir}/"

  (
    cd "${package_dir}"
    find . -type f -print | LC_ALL=C sort | sed 's#^\./##' > "${file_list}"
  )

  while IFS= read -r relative_file; do
    touch -t "${package_timestamp}" "${package_dir}/${relative_file}"
  done < "${file_list}"

  (
    cd "${package_dir}"
    zip -X -q "${archive_tmp}" -@ < "${file_list}"
  )

  unzip -tq "${archive_tmp}" >/dev/null
  unzip -Z1 "${archive_tmp}" | grep -Fx 'manifest.json' >/dev/null || fail "${worker_name}.zip has no root manifest.json"
  mv -f -- "${archive_tmp}" "${archive_final}"
  printf 'Built %s\n' "${archive_final}"
done

checksums_tmp="${build_tmp_dir}/SHA256SUMS"
(
  cd "${dist_dir}"
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "${worker_names[@]/%/.zip}" > "${checksums_tmp}"
  else
    sha256sum "${worker_names[@]/%/.zip}" > "${checksums_tmp}"
  fi
)
mv -f -- "${checksums_tmp}" "${dist_dir}/SHA256SUMS"
printf 'Wrote %s\n' "${dist_dir}/SHA256SUMS"
