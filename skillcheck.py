#!/usr/bin/env python3
"""skillcheck: verify claims and code samples in agent-skill / docs markdown against pinned upstream code.

Deterministic, no LLM, no network. Checks:
  facts    "`HOOK_FLAG` permission (bit N)" claims vs the constants in Hooks.sol
  imports  every import path in ```solidity blocks resolves under at least one remapping profile
           (imports whose prefix no profile maps are not judged unless --strict-imports)
  compile  every complete Solidity block (pragma + contract) compiles in isolation with forge

Exit codes:  0 = no findings   1 = findings   2 = usage or setup error (nothing was judged)
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile

__version__ = "0.1.4"

FENCE = re.compile(r"```(?:solidity|sol)\n(.*?)```", re.S)
IMPORT = re.compile(r'^\s*import\s+(?:[^"\';]*?\s+from\s+)?["\']([^"\']+)["\']\s*;', re.M)
CLAIM = re.compile(r"`([A-Z][A-Z0-9_]+?)(?:_FLAG)?`\s+(?:permission\s+)?\(bit (\d+)\)")
CONST = re.compile(r"uint160\s+internal\s+constant\s+([A-Z0-9_]+?)_FLAG\s*=\s*1\s*<<\s*(\d+)\s*;")
DEFAULT_EXCLUDE = ("node_modules", ".git", "lib", "out", "cache", "dist")
MARKDOWN_SUFFIXES = (".md", ".mdx")
# A sample the author elided cannot be judged by compiling it: the compiler
# reports the missing code, not a defect in the docs. These match only lines
# that are nothing but an ellipsis, or a comment standing in for omitted code.
ELISION = (
    re.compile(r"^\s*\.\.\.\s*;?\s*$"),                 # bare `...` statement
    re.compile(r"^\s*//\s*\.\.\.\s*$"),                 # `// ...`
    re.compile(r"^\s*/\*\s*\.\.\.\s*\*/\s*$"),        # `/* ... */`
    re.compile(r"^\s*//.*\b(?:import statements|rest of the|omitted for brevity"
               r"|constructor, state)\b", re.I),
)
# forge refuses before compiling when no installed solc matches the pragma.
# That is a property of the fixture, not of the sample.
PRAGMA_MISMATCH = re.compile(r"invalid solc version|No solc version exists that matches")
COMPILE_TIMEOUT_S = 300


class SetupError(Exception):
    """Something prevented checking (bad path, no ground truth, forge missing). Exit code 2."""


def read(path):
    with open(path, errors="ignore") as fh:
        return fh.read()


def md_files(root, exclude=DEFAULT_EXCLUDE):
    # .mdx too: Docusaurus/Nextra docs sites (Uniswap/docs among them) are
    # almost entirely .mdx, and skipping it made whole doc trees silently
    # unjudged -- a clean run that had checked nothing.
    for d, dirs, fs in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in exclude)
        for f in sorted(fs):
            if f.endswith(MARKDOWN_SUFFIXES):
                yield os.path.join(d, f)


def load_flags(hooks_sol):
    if not os.path.isfile(hooks_sol):
        raise SetupError(f"--hooks-sol not found: {hooks_sol}")
    flags = {n: int(b) for n, b in CONST.findall(read(hooks_sol))}
    if not flags:
        raise SetupError(f"no 'uint160 internal constant X_FLAG = 1 << N' lines found in {hooks_sol}")
    return flags


def load_remappings(path):
    if not os.path.isfile(path):
        raise SetupError(f"remappings file not found: {path}")
    rm = {}
    for line in read(path).split("\n"):
        line = line.strip().strip(",").strip('"')
        if "=" in line and not line.startswith(("#", "[")):
            k, v = line.split("=", 1)
            rm[k.strip().strip('"')] = v.strip().strip('"')
    return rm


def check_facts(skills, flags, exclude=DEFAULT_EXCLUDE):
    out = []
    for f in md_files(skills, exclude):
        fence = None
        for i, line in enumerate(read(f).split("\n"), 1):
            st = line.lstrip()
            if st.startswith("```"):
                fence = None if fence is not None else st[3:].strip().lower()
                continue
            if fence == "diff":
                continue  # a diff quotes the old text on purpose
            if st.startswith(">") or "skillcheck:ignore" in line:
                continue  # quoted text or explicitly suppressed
            for name, bit in CLAIM.findall(line):
                if name in flags and int(bit) != flags[name]:
                    out.append({"check": "facts", "file": f, "line": i,
                                "detail": f"{name} claimed bit {bit}, Hooks.sol says bit {flags[name]}"})
    return out


def blocks(skills, exclude=DEFAULT_EXCLUDE):
    # yields (file, line of the opening fence, code). Code starts on the line after the fence.
    for f in md_files(skills, exclude):
        t = read(f)
        for m in FENCE.finditer(t):
            yield f, t[:m.start()].count("\n") + 1, m.group(1)


def resolve(path, remaps, base):
    if path.startswith("."):
        return True  # relative import inside a snippet: cannot judge
    best = None
    for k in remaps:
        if path.startswith(k) and (best is None or len(k) > len(best)):
            best = k
    if best is None:
        return False
    return os.path.exists(os.path.join(base, remaps[best] + path[len(best):]))


def covered(path, profiles):
    """True if some profile has a remapping prefix for this import (i.e. we have ground truth for it)."""
    return any(path.startswith(k) for r, _ in profiles for k in r)


def check_imports(skills, profiles, base=None, exclude=DEFAULT_EXCLUDE, strict=False):
    # profiles: list of (remaps, base). The old single-profile call (remaps dict, base) is accepted too.
    # Imports whose prefix no profile maps are NOT judged (no ground truth), unless strict=True.
    if isinstance(profiles, dict):
        profiles = [(profiles, base)]
    out = []
    for f, fence_line, code in blocks(skills, exclude):
        for m in IMPORT.finditer(code):
            imp = m.group(1)
            if not strict and not imp.startswith(".") and not covered(imp, profiles):
                continue
            if not any(resolve(imp, r, b) for r, b in profiles):
                line = fence_line + 1 + code[:m.start(1)].count("\n")  # the line holding the import path
                out.append({"check": "imports", "file": f, "line": line, "detail": f"unresolved import {imp}"})
    return out


UNRESOLVED = re.compile(r'Unable to resolve imports:\s*((?:\s*"[^"]+"\s+in\s+"[^"]+"\s*)+)')
QUOTED_IMPORT = re.compile(r'"([^"]+)"\s+in\s+"')


def compile_detail(text, tmpdir):
    """One actionable line. Names the unresolved imports when that is the cause,
    and never leaks the local fixture path into a finding."""
    m = UNRESOLVED.search(text)
    if m:
        paths = QUOTED_IMPORT.findall(m.group(1))
        if paths:
            shown = ", ".join(paths[:3]) + (f" (+{len(paths) - 3} more)" if len(paths) > 3 else "")
            return f"unresolved import in sample: {shown}"
    m = re.search(r"(Error \(\d+\): [^\n]*)", text)
    if m:
        return re.sub(re.escape(tmpdir), "<sample>", m.group(1))[:200]
    return "compile failed"


def elided(code):
    """Line number of the first elision marker, else None."""
    for i, line in enumerate(code.splitlines(), 1):
        if any(p.match(line) for p in ELISION):
            return i
    return None


def unjudgeable_imports(code, profiles):
    """Imports we have no ground truth for, so a failed compile proves nothing:
    paths relative to the reader's own project, and prefixes no supplied
    remapping profile maps. Mirrors the rule check_imports already applies."""
    bad = []
    for m in IMPORT.finditer(code):
        imp = m.group(1)
        if imp.startswith("."):
            bad.append(imp)
        elif profiles and not covered(imp, profiles):
            bad.append(imp)
    return bad


def check_compile(skills, project, exclude=DEFAULT_EXCLUDE, profiles=()):
    if shutil.which("forge") is None:
        raise SetupError("forge not found on PATH (needed for the compile check; use --no-compile to skip)")
    for need in ("foundry.toml", "lib"):
        if not os.path.exists(os.path.join(project, need)):
            raise SetupError(f"--project must contain {need}: {project}")
    out, n, skipped = [], 0, {"fragment": [], "pragma": [], "deps": []}
    for f, line, code in blocks(skills, exclude):
        if not ("pragma solidity" in code and re.search(r"\b(contract|library|interface)\s+\w+", code)):
            continue
        at = elided(code)
        if at is not None:
            skipped["fragment"].append({"file": f, "line": line, "detail": f"elided at sample line {at}"})
            continue
        outside = unjudgeable_imports(code, profiles)
        if outside:
            shown = ", ".join(outside[:3]) + (f" (+{len(outside) - 3} more)" if len(outside) > 3 else "")
            skipped["deps"].append({"file": f, "line": line,
                                    "detail": f"imports outside the fixture: {shown}"})
            continue
        n += 1
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "src"))
            with open(os.path.join(td, "src", "Sample.sol"), "w") as fh:
                fh.write(code)
            shutil.copy(os.path.join(project, "foundry.toml"), td)
            os.symlink(os.path.abspath(os.path.join(project, "lib")), os.path.join(td, "lib"))
            try:
                p = subprocess.run(["forge", "build", "--force"], cwd=td, capture_output=True, text=True,
                                   timeout=COMPILE_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                out.append({"check": "compile", "file": f, "line": line, "detail": f"compile timed out after {COMPILE_TIMEOUT_S}s"})
                continue
            text = p.stdout + p.stderr
            if PRAGMA_MISMATCH.search(text):
                n -= 1
                skipped["pragma"].append({"file": f, "line": line,
                                          "detail": "sample's solc pragma is not satisfiable by --project"})
                continue
            if "Compiler run successful" not in text:
                out.append({"check": "compile", "file": f, "line": line,
                            "detail": compile_detail(text, td)})
    return out, n, skipped


def build_parser():
    ap = argparse.ArgumentParser(prog="skillcheck", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills", required=True, help="directory to scan for markdown")
    ap.add_argument("--hooks-sol", required=True, help="path to v4-core src/libraries/Hooks.sol (ground truth)")
    # action="extend" so a repeated flag accumulates. With plain nargs="*" a
    # second --remappings silently REPLACED the first, so only the last profile
    # was consulted and imports the earlier profile covered were reported
    # unresolved -- a false positive that looks exactly like real drift.
    ap.add_argument("--remappings", nargs="*", action="extend", default=[],
                    help="one or more remapping files (profiles); an import passes if it resolves under any. "
                         "Without any, the import check is skipped (a Foundry project's own remappings.txt works)")
    ap.add_argument("--project", help="Foundry project dir (foundry.toml + lib/) used for the compile check")
    ap.add_argument("--no-compile", action="store_true", help="skip the compile check (no forge needed)")
    ap.add_argument("--strict-imports", action="store_true",
                    help="also flag imports whose prefix no remapping profile covers (default: not judged)")
    ap.add_argument("--exclude", nargs="*", default=list(DEFAULT_EXCLUDE), help="directory names to skip")
    ap.add_argument("--report-skipped", action="store_true",
                    help="list samples that were not judged (elided code, or a solc pragma --project cannot satisfy)")
    ap.add_argument("--json", help="write findings to this file")
    ap.add_argument("--version", action="version", version=f"skillcheck {__version__}")
    return ap


def run(a):
    if not os.path.isdir(a.skills):
        raise SetupError(f"--skills is not a directory: {a.skills}")
    if not a.no_compile and not a.project:
        raise SetupError("--project is required unless --no-compile is given")
    flags = load_flags(a.hooks_sol)
    profiles = [(load_remappings(p), os.path.dirname(os.path.abspath(p))) for p in a.remappings]
    findings = check_facts(a.skills, flags, a.exclude)
    if profiles:
        findings += check_imports(a.skills, profiles, exclude=a.exclude, strict=a.strict_imports)
    else:
        print("note: no --remappings given; import check skipped", file=sys.stderr)
    compiled, skipped = None, {"fragment": [], "pragma": [], "deps": []}
    if not a.no_compile:
        cf, compiled, skipped = check_compile(a.skills, a.project, a.exclude, profiles)
        findings += cf
    for x in findings:
        print(f"[{x['check']}] {os.path.relpath(x['file'], a.skills)}:{x['line']}  {x['detail']}")
    if a.report_skipped:
        for kind in ("fragment", "pragma", "deps"):
            for x in skipped[kind]:
                print(f"[skipped:{kind}] {os.path.relpath(x['file'], a.skills)}:{x['line']}  {x['detail']}")
    summary = {c: sum(1 for x in findings if x["check"] == c) for c in ("facts", "imports", "compile")}
    nskip = {k: len(v) for k, v in skipped.items()}
    print(f"\nflags known: {len(flags)} | complete samples compiled: {compiled} "
          f"| not judged: {nskip} | findings: {summary}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"version": __version__, "summary": summary,
                       "not_judged": skipped, "findings": findings}, fh, indent=1)
    return 1 if findings else 0


def main(argv=None):
    try:
        a = build_parser().parse_args(argv)  # argparse exits 2 on usage errors
        return run(a)
    except SetupError as e:
        print(f"skillcheck: error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
