#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
version="${MJOBS_CODEX_VERSION:-latest}"
if [[ ! "$version" =~ ^(latest|[0-9]+\.[0-9]+\.[0-9]+([-+][0-9A-Za-z.-]+)?)$ ]]; then
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
