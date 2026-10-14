#!/usr/bin/env python3
"""skillcheck: verify claims and code samples in agent-skill / docs markdown against pinned upstream code.

Deterministic, no LLM, no network. Checks:
  facts    "`HOOK_FLAG` permission (bit N)" claims vs the constants in Hooks.sol
  imports  every import path in ```solidity blocks resolves under at least one remapping profile
  compile  every complete Solidity block (pragma + contract) compiles in isolation with forge

Exit codes:  0 = no findings   1 = findings   2 = usage or setup error (nothing was judged)
"""
import argparse, json, os, re, shutil, subprocess, sys, tempfile

__version__ = "0.1.0"

FENCE = re.compile(r"```(?:solidity|sol)\n(.*?)```", re.S)
IMPORT = re.compile(r'^\s*import\s+(?:[^"\';]*?\s+from\s+)?["\']([^"\']+)["\']\s*;', re.M)
CLAIM = re.compile(r"`([A-Z][A-Z0-9_]+?)(?:_FLAG)?`\s+(?:permission\s+)?\(bit (\d+)\)")
CONST = re.compile(r"uint160\s+internal\s+constant\s+([A-Z0-9_]+?)_FLAG\s*=\s*1\s*<<\s*(\d+)\s*;")
DEFAULT_EXCLUDE = ("node_modules", ".git", "lib", "out", "cache", "dist")
COMPILE_TIMEOUT_S = 300


class SetupError(Exception):
    """Something prevented checking (bad path, no ground truth, forge missing). Exit code 2."""


def read(path):
    with open(path, errors="ignore") as fh:
        return fh.read()


def md_files(root, exclude=DEFAULT_EXCLUDE):
    for d, dirs, fs in os.walk(root):
        dirs[:] = sorted(x for x in dirs if x not in exclude)
        for f in sorted(fs):
            if f.endswith(".md"):
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


def check_imports(skills, profiles, base=None, exclude=DEFAULT_EXCLUDE):
    # profiles: list of (remaps, base). The old single-profile call (remaps dict, base) is accepted too.
    if isinstance(profiles, dict):
        profiles = [(profiles, base)]
    out = []
    for f, line, code in blocks(skills, exclude):
        for imp in IMPORT.findall(code):
            if not any(resolve(imp, r, b) for r, b in profiles):
                out.append({"check": "imports", "file": f, "line": line, "detail": f"unresolved import {imp}"})
    return out


def check_compile(skills, project, exclude=DEFAULT_EXCLUDE):
    if shutil.which("forge") is None:
        raise SetupError("forge not found on PATH (needed for the compile check; use --no-compile to skip)")
    for need in ("foundry.toml", "lib"):
        if not os.path.exists(os.path.join(project, need)):
            raise SetupError(f"--project must contain {need}: {project}")
    out, n = [], 0
    for f, line, code in blocks(skills, exclude):
        if not ("pragma solidity" in code and re.search(r"\b(contract|library|interface)\s+\w+", code)):
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
            if "Compiler run successful" not in text:
                msgs = re.findall(r"(Error \(\d+\): [^\n]*|Unable to resolve imports:[^\n]*)", text)
                out.append({"check": "compile", "file": f, "line": line,
                            "detail": (msgs[0] if msgs else "compile failed")[:200]})
    return out, n


def build_parser():
    ap = argparse.ArgumentParser(prog="skillcheck", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--skills", required=True, help="directory to scan for markdown")
    ap.add_argument("--hooks-sol", required=True, help="path to v4-core src/libraries/Hooks.sol (ground truth)")
    ap.add_argument("--remappings", nargs="*", default=[],
                    help="one or more remapping files (profiles); an import passes if it resolves under any. "
                         "Without any, the import check is skipped (a Foundry project's own remappings.txt works)")
    ap.add_argument("--project", help="Foundry project dir (foundry.toml + lib/) used for the compile check")
    ap.add_argument("--no-compile", action="store_true", help="skip the compile check (no forge needed)")
    ap.add_argument("--exclude", nargs="*", default=list(DEFAULT_EXCLUDE), help="directory names to skip")
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
        findings += check_imports(a.skills, profiles, exclude=a.exclude)
    else:
        print("note: no --remappings given; import check skipped", file=sys.stderr)
    compiled = None
    if not a.no_compile:
        cf, compiled = check_compile(a.skills, a.project, a.exclude)
        findings += cf
    for x in findings:
        print(f"[{x['check']}] {os.path.relpath(x['file'], a.skills)}:{x['line']}  {x['detail']}")
    summary = {c: sum(1 for x in findings if x["check"] == c) for c in ("facts", "imports", "compile")}
    print(f"\nflags known: {len(flags)} | complete samples compiled: {compiled} | findings: {summary}")
    if a.json:
        with open(a.json, "w") as fh:
            json.dump({"version": __version__, "summary": summary, "findings": findings}, fh, indent=1)
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
