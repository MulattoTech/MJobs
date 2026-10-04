#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Preserve any existing secret; create one only for a new Codespace clone.
if [[ -x .venv/bin/python ]]; then
  .venv/bin/python -m cli.init_env
fi

echo
echo "MJobs Codespace ready."
printf 'Git:   '
git status --short --branch
if command -v codex >/dev/null 2>&1; then
  printf 'Codex: '
  codex --version
else
  echo "Codex: not installed yet; rerun bash .devcontainer/bootstrap-codex.sh"
fi
if docker info >/dev/null 2>&1; then
  echo "Docker: ready"
else
  echo "Docker: daemon not ready yet; retry in a few seconds if starting services."
fi
echo "Use bash .devcontainer/mjobs.sh fixture to start the fictional test stack."
echo "Use bash .devcontainer/mjobs.sh personal for the isolated PERSONAL stack."
