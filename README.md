# skillcheck

Deterministic checks (no LLM, no network) for markdown that tells people or coding agents how to use a code library: do the claims and code samples still match the pinned upstream code?

It was written for Uniswap v4 hook documentation, where three kinds of drift keep happening: a permission cited with the wrong bit number, an import path that moved to another package, and a code sample that no longer compiles.

## Checks

| Check | What it does |
|---|---|
| `facts` | Finds claims like ``The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10)`` and compares them with the `uint160 internal constant X_FLAG = 1 << N` lines in v4-core's `Hooks.sol`. |
| `imports` | Finds every `import` in ```` ```solidity ```` blocks and checks that the path resolves under at least one remapping profile you supply. Imports whose prefix no profile maps (for example `@aave/...` when you only supplied Uniswap and OpenZeppelin profiles) are **not judged**, because there is no ground truth for them; add `--strict-imports` to flag them anyway. |
| `compile` | Compiles every *complete* Solidity block (has `pragma solidity` and a `contract`, `library` or `interface`) in isolation with `forge build`. Fragments are not judged. |

Quoted text is skipped: lines starting with `>`, lines inside ```` ```diff ```` blocks, and any line containing `skillcheck:ignore`. This is so a document can quote an error in order to report it.

## Usage

```
skillcheck --skills <dir> --hooks-sol <path/to/Hooks.sol> \
           [--remappings <profile.txt> ...] \
           [--project <foundry project dir> | --no-compile] [--strict-imports] [--exclude <dir> ...] [--json out.json]
```

- `--remappings` is optional. Without it the import check is skipped (the other checks still run). A Foundry project's own `remappings.txt` works as a profile.
- `--project` needs a `foundry.toml` and a `lib/` directory with your pinned dependencies; `examples/` has a working set. Give `--no-compile` to skip the compile check (then `forge` is not needed).
- By default the directories `node_modules .git lib out cache dist` are skipped.

### Install and quick check

Python 3.9+, standard library only (`forge` is needed only for the compile check).

```
pipx install git+https://github.com/ss1738/skillcheck@v0.1.2       # or: uvx --from git+https://github.com/ss1738/skillcheck@v0.1.2 skillcheck ...
```

Quick check of hook-permission bit claims in any docs folder, no other setup (`Hooks.sol` pinned to v4-core tag `v4.0.0`):

```
curl -sLo Hooks.sol https://raw.githubusercontent.com/Uniswap/v4-core/e50237c43811bd9b526eff40f26772152a42daba/src/libraries/Hooks.sol
uvx --from git+https://github.com/ss1738/skillcheck@v0.1.2 skillcheck --skills . --hooks-sol Hooks.sol --no-compile
```

**Exit codes:** `0` no findings. `1` findings. `2` usage or setup error (bad path, no flags found in `Hooks.sol`, `forge` missing): nothing was judged. This is covered by tests.

### GitHub Action

```yaml
- uses: foundry-rs/foundry-toolchain@<pin a commit SHA>   # only needed for the compile check
- uses: ss1738/skillcheck@<pin a commit SHA>
  with:
    skills-path: .
    hooks-sol: lib/v4-core/src/libraries/Hooks.sol
    remappings: remappings-oz-uniswap-hooks.txt remappings-src-layout.txt
    project: .
```

Inputs are passed to the script through environment variables, never interpolated into the shell command.

## Reproducible results

`scripts/reproduce.sh <empty dir>` rebuilds everything from public sources (about 4 minutes) and prints the output below. Ground truth: v4-core `d153b048` (pinned by OpenZeppelin `uniswap-hooks` v1.2.1), v4-periphery `9969eec4`, OpenZeppelin Contracts 5.5.0, solc 0.8.26, forge 1.7.1.

Target: [`Uniswap/uniswap-ai`](https://github.com/Uniswap/uniswap-ai) at `1a43e96`, plus two fix branches in [`ss1738/uniswap-ai`](https://github.com/ss1738/uniswap-ai).

| Target | facts | imports | compile | exit |
|---|---|---|---|---|
| A. `Uniswap/uniswap-ai@1a43e96` | 1 | 4 | 4 | 1 |
| B. A + `fix/uniswap-hooks-factual-corrections` (`cce7f3e`) | 0 | 1 | 2 | 1 |
| C. A + `fix/uniswap-cca-deployer-ierc20` (`9b82853`) | 1 | 4 | 3 | 1 |
| D. A + B + C | 0 | 1 | 1 | 1 |

D still reports 2 findings (the same dead `BaseHook` import and the compile failure that follows from it), both in `evals/suites/v4-security-foundations/cases/combined-vulnerability-code-review.md`. That file is an eval case whose purpose is to show vulnerable code; porting it to the current `BaseHook` changes what it tests, so it was left for its owners.

Findings for A (for `imports` the line is the one holding the import; for `compile` it is the line where the sample's code block opens):

```
[facts]   .../uniswap-hooks/skills/v4-security-foundations/SKILL.md:58   BEFORE_SWAP_RETURNS_DELTA claimed bit 10, Hooks.sol says bit 3
[imports] .../v4-security-foundations/SKILL.md:252                       unresolved import v4-periphery/src/base/hooks/BaseHook.sol
[imports] .../references/base-hook-template.md:11                        unresolved import v4-periphery/src/base/hooks/BaseHook.sol
[imports] .../references/vulnerabilities-catalog.md:134                  unresolved import @openzeppelin/contracts/security/ReentrancyGuard.sol
[imports] .../cases/combined-vulnerability-code-review.md:13             unresolved import v4-periphery/src/base/hooks/BaseHook.sol
[compile] .../uniswap-cca/skills/deployer/SKILL.md:259                   Error (7576): Undeclared identifier.
[compile] 3 more samples fail on the dead BaseHook import
flags known: 14 | complete samples compiled: 6 | findings: {'facts': 1, 'imports': 4, 'compile': 4}
```

## Limits

- `facts` checks one claim shape (a flag name followed by `(bit N)`). Other claims are not checked.
- Results depend on the library versions you pin. For example `@openzeppelin/contracts/security/ReentrancyGuard.sol` is correct under OpenZeppelin v4 and absent in v5; supply a remapping profile for each version you support.
- `compile` judges only complete blocks. In the Uniswap repo above, 6 of 69 Solidity blocks are complete.
- It reads markdown and runs `forge build` on copies of the code blocks; it never executes anything found in the documents.

## Tests

`python3 tests/test_skillcheck.py` (24 tests: red on known-bad input, green on known-good input, exit-code contract).

## License

MIT
