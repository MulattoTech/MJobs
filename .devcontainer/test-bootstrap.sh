#!/usr/bin/env bash
# Offline contract tests: no real npm install, auth access, agent or runtime start.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir -p "$TMP/repo/.devcontainer" "$TMP/repo/.agents/skills/mjobs-delivery" "$TMP/bin" "$TMP/home/.codex/skills/afk-dev"
cp "$ROOT/.devcontainer/bootstrap-codex.sh" "$TMP/repo/.devcontainer/"
printf 'Synthetic project skill\n' > "$TMP/repo/.agents/skills/mjobs-delivery/SKILL.md"
printf 'Synthetic existing skill\n' > "$TMP/home/.codex/skills/afk-dev/SKILL.md"
printf 'Synthetic credentials: never real\n' > "$TMP/home/.codex/auth.json"
printf 'Synthetic config\n' > "$TMP/home/.codex/config.toml"
export TEST_NPM_LOG="$TMP/npm.log" TEST_NPX_LOG="$TMP/npx.log" TEST_CODEX_LOG="$TMP/codex.log"
export HOME="$TMP/home"
cat > "$TMP/bin/npm" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_NPM_LOG"
if [[ "${TEST_NPM_FAIL:-0}" == 1 ]]; then exit 17; fi
STUB
cat > "$TMP/bin/codex" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_CODEX_LOG"
[[ "$*" == --version ]] || exit 19
printf 'codex-cli synthetic-bootstrap-test\n'
STUB
cat > "$TMP/bin/npx" <<'STUB'
#!/usr/bin/env bash
printf '%s\n' "$*" >> "$TEST_NPX_LOG"
exit 23
STUB
printf '#!/usr/bin/env bash\nexit 0\n' > "$TMP/bin/sleep"
chmod +x "$TMP/bin/"*
export PATH="$TMP/bin:$PATH"
script="$TMP/repo/.devcontainer/bootstrap-codex.sh"
before="$(find "$HOME" -type f -exec sha256sum {} + | sort)"
unset MJOBS_CODEX_VERSION TEST_NPM_FAIL
bash "$script"
test "$(cat "$TEST_NPM_LOG")" = 'install --global @openai/codex@latest'
test "$(cat "$TEST_CODEX_LOG")" = '--version'
: > "$TEST_NPM_LOG"
MJOBS_CODEX_VERSION=0.160.0 bash "$script"
test "$(cat "$TEST_NPM_LOG")" = 'install --global @openai/codex@0.160.0'
for version in '1.2.3-beta.1+build.7' '0.0.0-0+00' '1.2.3+sha.1' '1.2.3-01abc'; do
  : > "$TEST_NPM_LOG"
  MJOBS_CODEX_VERSION="$version" bash "$script"
  test "$(cat "$TEST_NPM_LOG")" = "install --global @openai/codex@$version"
done
for version in '1.2.3-01' '1.02.3' '1.2.3-rc..1' '1.2.3+' '1.2.3 ' 'https://invalid.example/package'; do
  : > "$TEST_NPM_LOG"
  if MJOBS_CODEX_VERSION="$version" bash "$script"; then
    echo "Invalid semantic version accepted: $version" >&2; exit 1
  else
    test "$?" = 2
  fi
  test ! -s "$TEST_NPM_LOG"
done
: > "$TEST_NPM_LOG"
if MJOBS_CODEX_VERSION='--invalid-package' bash "$script"; then
  echo 'Invalid version unexpectedly accepted' >&2; exit 1
else
  test "$?" = 2
fi
test ! -s "$TEST_NPM_LOG"
: > "$TEST_CODEX_LOG"
if TEST_NPM_FAIL=1 bash "$script"; then
  echo 'Persistent installation failure unexpectedly accepted' >&2; exit 1
else
  test "$?" = 1
fi
test "$(wc -l < "$TEST_NPM_LOG" | tr -d ' ')" = 3
test ! -s "$TEST_CODEX_LOG"
test ! -e "$TEST_NPX_LOG"
test "$before" = "$(find "$HOME" -type f -exec sha256sum {} + | sort)"
echo 'Codespaces bootstrap contract PASS: default/pinned versions, invalid input, three-attempt bound, no extra skills/agents, preserved synthetic credentials/config.'
