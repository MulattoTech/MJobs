#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

retry() {
  local attempts="$1"
  shift
  local n=1
  until "$@"; do
    if (( n >= attempts )); then
      return 1
    fi
    echo "Attempt $n failed; retrying in $((n * 2))s: $*" >&2
    sleep "$((n * 2))"
    n=$((n + 1))
  done
}

echo "==> Installing/updating Codex CLI"
retry 3 npm install -g @openai/codex@latest
codex --version

echo "==> Installing lean global coding skills for Codex"
retry 3 npx --yes skills add obra/superpowers   --global --agent codex --yes --copy   --skill brainstorming   --skill requesting-code-review   --skill systematic-debugging   --skill test-driven-development   --skill using-git-worktrees   --skill verification-before-completion   --skill writing-plans

retry 3 npx --yes skills add Jericho-Y/project-memory-manager   --global --agent codex --yes --copy --skill pmm

# AFK-DEV is a user-authored skill used by the MJobs delivery contract. The
# Codespace copy is vendored only as bootstrap material; it is not auto-discovered
# from .devcontainer. Copy it into Codex's normal user skill directory.
if [[ -d .devcontainer/codex-skills/afk-dev ]]; then
  mkdir -p "$HOME/.codex/skills"
  rm -rf "$HOME/.codex/skills/afk-dev"
  cp -a .devcontainer/codex-skills/afk-dev "$HOME/.codex/skills/afk-dev"
fi

echo
echo "==> Global skill inventory"
npx --yes skills list --global --agent codex || true
echo "==> Project MJobs skill"
test -f .agents/skills/mjobs-delivery/SKILL.md
echo "Codex developer tooling bootstrap complete."
