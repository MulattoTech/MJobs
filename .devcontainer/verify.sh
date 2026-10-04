#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mode="${1:-quick}"
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run bash .devcontainer/setup.sh first." >&2
  exit 1
fi

# shellcheck disable=SC1091
source .venv/bin/activate

docker info >/dev/null
docker compose config --quiet
npm test --prefix apps/web -- --run
npm run build --prefix apps/web
python -m tools.verification.agent_skills
python -m tools.verification.catalogue
python -m tools.verification.progress validate
git diff --check

if [[ "$mode" == "full" ]]; then
  python -m unittest discover -s tests -p 'test_*.py' -q
elif [[ "$mode" != "quick" ]]; then
  echo "Usage: $0 {quick|full}" >&2
  exit 2
fi

echo "Codespace verification PASS ($mode)."
