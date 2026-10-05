#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Bootstrapping MJobs development environment at $ROOT"

if [[ ! -d .venv ]]; then
  python -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate

python -m pip install --upgrade pip wheel
python -m pip install -r requirements.lock
npm ci --prefix apps/web

# Install Linux browser dependencies as root, but keep browser binaries owned by vscode.
if command -v sudo >/dev/null 2>&1; then
  sudo env DEBIAN_FRONTEND=noninteractive "$(command -v python)" -m playwright install-deps chromium
else
  python -m playwright install-deps chromium
fi
python -m playwright install chromium

# This is intentionally idempotent and never overwrites an existing private secret.
python -m cli.init_env

# Codex CLI is developer tooling; the project-scoped skill already lives in this repo.
# Keep CI focused on the app; prepare them automatically only in a real Codespace.
if [[ "${CODESPACES:-false}" == "true" ]]; then
  bash .devcontainer/bootstrap-codex.sh
else
  echo "==> Not a live Codespace; skipping Codex CLI bootstrap."
fi

python -m tools.verification.agent_skills
python -m tools.verification.catalogue
python -m tools.verification.progress validate
git diff --check

echo
echo "MJobs environment bootstrap complete."
echo "Fixture:  bash .devcontainer/mjobs.sh fixture"
echo "Personal: bash .devcontainer/mjobs.sh personal"
echo "Verify:   bash .devcontainer/verify.sh full"
echo "Codex:    codex  # Read prompts/CODESPACES_ITERATION.md and supply one explicit task"
