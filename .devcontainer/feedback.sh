#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

PY="$ROOT/.venv/bin/python"
if [[ ! -x "$PY" ]]; then
  echo "Missing .venv. Run bash .devcontainer/setup.sh first." >&2
  exit 1
fi

mkdir -p outputs/live-feedback
exec "$PY" -m cli.feedback_gui   --no-open   --port 18702   --output-root "$ROOT/outputs/live-feedback"
