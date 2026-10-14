#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Only the stdlib configuration initializer runs on the host; application
# dependencies and their Python version remain owned by the Compose images.
PY=""
for candidate in "$ROOT/.venv/bin/python" "$(command -v python3 || true)" "$(command -v python || true)"; do
  if [[ -n "$candidate" && -x "$candidate" ]] &&
      "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 10))' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "Python 3.10 or newer is required to initialize local configuration. Install python3 or create a supported .venv." >&2
  exit 1
fi

"$PY" -m cli.init_env

wait_for_docker() {
  local i
  for i in {1..30}; do
    if docker info >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  echo "Docker daemon did not become ready." >&2
  return 1
}

wait_for_docker

mode="${1:-fixture}"
case "$mode" in
  fixture)
    export MJOBS_PORT=18700
    export MJOBS_OPERATION_MODE=FIXTURE
    export MJOBS_COMPOSE_MOCK=1
    export COMPOSE_PROFILES=fixture
    docker compose -p mjobs --profile fixture up -d --build --wait --wait-timeout 180
    echo "MJobs FIXTURE stack is ready on forwarded port 18700."
    ;;
  personal)
    export MJOBS_PORT=18701
    export MJOBS_OPERATION_MODE=PERSONAL
    export MJOBS_COMPOSE_MOCK=0
    export COMPOSE_PROFILES=
    docker compose -p mjobs-personal up -d --build --wait --wait-timeout 180
    echo "MJobs PERSONAL stack is ready on forwarded port 18701."
    echo "Real external submission remains OFF."
    ;;
  status)
    echo "== FIXTURE =="
    docker compose -p mjobs --profile fixture ps
    echo
    echo "== PERSONAL =="
    docker compose -p mjobs-personal ps
    ;;
  down-fixture)
    docker compose -p mjobs --profile fixture down
    ;;
  down-personal)
    docker compose -p mjobs-personal down
    ;;
  down-all)
    docker compose -p mjobs --profile fixture down
    docker compose -p mjobs-personal down
    ;;
  *)
    echo "Usage: $0 {fixture|personal|status|down-fixture|down-personal|down-all}" >&2
    exit 2
    ;;
esac
