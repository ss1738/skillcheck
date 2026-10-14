#!/usr/bin/env bash
# Reproduce skillcheck results against Uniswap/uniswap-ai from public sources only.
# Needs: git, python3, forge (Foundry), network. Usage: scripts/reproduce.sh <empty work dir>
set -euo pipefail
WORK="${1:?usage: reproduce.sh <empty work dir>}"
HERE="$(cd "$(dirname "$0")/.." && pwd)"
SHA=1a43e968a39537d5a73aeca20dd1ed9d96f502fa           # Uniswap/uniswap-ai at the audited commit
UP=https://github.com/Uniswap/uniswap-ai.git
FORK=https://github.com/ss1738/uniswap-ai.git
mkdir -p "$WORK" && cd "$WORK"
[ -z "$(ls -A .)" ] || { echo "work dir must be empty" >&2; exit 2; }

echo "== 1. Foundry project with pinned libraries (ground truth + compile environment)"
mkdir proj && ( cd proj && git init -q && git config user.email a@b.invalid && git config user.name reproduce \
  && forge init --force . >/dev/null 2>&1 \
  && forge install OpenZeppelin/uniswap-hooks@v1.2.1 Uniswap/v4-periphery@9969eec4 >/dev/null 2>&1 \
  && rm -f src/Counter.sol test/Counter.t.sol script/Counter.s.sol \
  && cp "$HERE/examples/foundry.toml" "$HERE"/examples/remappings-*.txt . )
echo "uniswap-hooks $(git -C proj/lib/uniswap-hooks describe --tags --always) | v4-periphery $(git -C proj/lib/v4-periphery rev-parse --short HEAD) | nested v4-core $(git -C proj/lib/uniswap-hooks/lib/v4-core rev-parse --short HEAD)"

echo "== 2. Targets"
git init -q target && cd target
git config user.email a@b.invalid && git config user.name reproduce
git remote add up "$UP" && git remote add fork "$FORK"
git fetch -q --depth 1 up "$SHA"
git fetch -q --depth 2 fork fix/uniswap-hooks-factual-corrections:hooks
git fetch -q --depth 2 fork fix/uniswap-cca-deployer-ierc20:cca
git branch -q base "$SHA"
git checkout -q -b merged base && git cherry-pick hooks cca >/dev/null
cd ..

HOOKS=proj/lib/uniswap-hooks/lib/v4-core/src/libraries/Hooks.sol
check() {  # label, branch
  ( cd target && git checkout -q "$2" )
  echo; echo "---- $1"
  set +e
  python3 "$HERE/skillcheck.py" --skills target --hooks-sol "$HOOKS" \
    --remappings proj/remappings-oz-uniswap-hooks.txt proj/remappings-src-layout.txt --project proj
  echo "exit code: $?"
  set -e
}
check "A. Uniswap/uniswap-ai @ 1a43e96"                                  base
check "B. + branch fix/uniswap-hooks-factual-corrections (cce7f3e)"      hooks
check "C. + branch fix/uniswap-cca-deployer-ierc20 (9b82853)"            cca
check "D. B and C applied together"                                      merged
