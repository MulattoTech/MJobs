#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
version="${MJOBS_CODEX_VERSION:-latest}"
# SemVer permits prerelease AND build metadata; numeric prerelease identifiers
# have no leading zeroes, while build identifiers may contain them.
numeric='(0|[1-9][0-9]*)'
prerelease='(0|[1-9][0-9]*|[0-9A-Za-z-]*[A-Za-z-][0-9A-Za-z-]*)'
build='[0-9A-Za-z-]+'
pattern="^${numeric}\.${numeric}\.${numeric}(-${prerelease}(\.${prerelease})*)?(\+${build}(\.${build})*)?$"
if [[ "$version" != latest && ! "$version" =~ $pattern ]]; then
  echo "MJOBS_CODEX_VERSION must be latest or an explicit semantic version." >&2
  exit 2
fi

test -f .agents/skills/mjobs-delivery/SKILL.md
retry() {
  local attempts="$1"; shift
  local n=1
  until "$@"; do
    if (( n >= attempts )); then return 1; fi
    sleep "$((n * 2))"
    n=$((n + 1))
  done
}

echo "Installing Codex CLI ($version); no agent or MJobs service is started."
retry 3 npm install --global "@openai/codex@$version"
codex --version
cat <<'MSG'
MJobs reuses its checked-in .agents/skills/mjobs-delivery skill.
No global skill, PMM, AFK, credential or user-config files are installed or replaced.
For headless sign-in: codex login --device-auth
Then run codex and read prompts/CODESPACES_ITERATION.md with your explicit task.
MSG
