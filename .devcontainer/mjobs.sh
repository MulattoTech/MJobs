#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

if [[ -x .venv/bin/python ]]; then
  PY="$ROOT/.venv/bin/python"
else
  PY="$(command -v python)"
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
