#!/usr/bin/env python3
"""Tests for skillcheck: red on known-bad input, green on known-good input, and a fixed exit-code contract."""
import os, subprocess, sys, tempfile, unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import skillcheck as sc  # noqa: E402

CLI = [sys.executable, os.path.join(ROOT, "skillcheck.py")]
HOOKS = """
    uint160 internal constant BEFORE_SWAP_FLAG = 1 << 7;
    uint160 internal constant BEFORE_SWAP_RETURNS_DELTA_FLAG = 1 << 3;
    uint160 internal constant AFTER_ADD_LIQUIDITY_FLAG = 1 << 10;
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.write(text)


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.mkdtemp()
        self.skills = os.path.join(self.td, "skills")
        os.makedirs(self.skills)
        self.hooks = os.path.join(self.td, "Hooks.sol")
        write(self.hooks, HOOKS)
        write(os.path.join(self.td, "lib", "pkg", "src", "A.sol"), "// x")
        self.rm = os.path.join(self.td, "remappings.txt")
        write(self.rm, "pkg/=lib/pkg/\n")

    def md(self, text, name="s.md"):
        write(os.path.join(self.skills, name), text)

    def facts(self):
        return sc.check_facts(self.skills, sc.load_flags(self.hooks))

    def imports(self):
        return sc.check_imports(self.skills, sc.load_remappings(self.rm), self.td)

    def cli(self, *args, env=None):
        return subprocess.run(CLI + list(args), capture_output=True, text=True, env=env)


class Facts(Base):
    def test_wrong_bit_is_flagged(self):
        self.md("The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) is dangerous.")
        f = self.facts()
        self.assertEqual(len(f), 1)
        self.assertIn("bit 3", f[0]["detail"])

    def test_right_bit_is_clean(self):
        self.md("The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 3) is dangerous.")
        self.assertEqual(self.facts(), [])

    def test_flag_suffix_form(self):
        self.md("`AFTER_ADD_LIQUIDITY_FLAG` (bit 10) and `BEFORE_SWAP` permission (bit 6)")
        self.assertEqual(len(self.facts()), 1)  # only BEFORE_SWAP is wrong (real: 7)

    def test_unknown_name_ignored(self):
        self.md("`SOMETHING_ELSE` permission (bit 99)")
        self.assertEqual(self.facts(), [])

    def test_blockquote_and_ignore_marker_not_flagged(self):
        self.md("> The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) is wrong, we report it\n"
                "The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) <!-- skillcheck:ignore -->")
        self.assertEqual(self.facts(), [])

    def test_diff_block_not_flagged_but_prose_after_it_is(self):
        self.md("```diff\n-The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) is bad\n+(bit 3)\n```\n"
                "The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) is still here")
        f = self.facts()
        self.assertEqual(len(f), 1)
        self.assertEqual(f[0]["line"], 5)


class Imports(Base):
    def test_unresolved_import_flagged(self):
        self.md('```solidity\nimport {X} from "pkg/src/Gone.sol";\n```')
        self.assertEqual(len(self.imports()), 1)

    def test_resolved_import_clean(self):
        self.md('```solidity\nimport {X} from "pkg/src/A.sol";\nimport "./local.sol";\n```')
        self.assertEqual(self.imports(), [])

    def test_unmapped_prefix_flagged(self):
        self.md('```sol\nimport "unknown/B.sol";\n```')
        self.assertEqual(len(self.imports()), 1)

    def test_second_profile_resolves_import(self):
        write(os.path.join(self.td, "lib2", "interfaces", "I.sol"), "// x")
        self.md('```solidity\nimport "core/interfaces/I.sol";\n```')
        one = [(sc.load_remappings(self.rm), self.td)]
        self.assertEqual(len(sc.check_imports(self.skills, one)), 1)
        self.assertEqual(sc.check_imports(self.skills, one + [({"core/": "lib2/"}, self.td)]), [])

    def test_excluded_directory_is_skipped(self):
        write(os.path.join(self.skills, "lib", "vendored.md"), '```solidity\nimport "unknown/B.sol";\n```')
        self.assertEqual(self.imports(), [])  # 'lib' is excluded by default
        self.assertEqual(len(sc.check_imports(self.skills, sc.load_remappings(self.rm), self.td, exclude=())), 1)


class ExitCodes(Base):
    def test_clean_exits_0(self):
        self.md("The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 3) is dangerous.")
        p = self.cli("--skills", self.skills, "--hooks-sol", self.hooks, "--remappings", self.rm, "--no-compile")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_findings_exit_1(self):
        self.md("The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) is dangerous.")
        p = self.cli("--skills", self.skills, "--hooks-sol", self.hooks, "--remappings", self.rm, "--no-compile")
        self.assertEqual(p.returncode, 1)

    def test_bad_paths_exit_2(self):
        p = self.cli("--skills", "/nonexistent", "--hooks-sol", self.hooks, "--remappings", self.rm, "--no-compile")
        self.assertEqual(p.returncode, 2)
        self.assertIn("skillcheck: error", p.stderr)

    def test_no_ground_truth_exit_2(self):
        write(self.hooks, "// no flags here")
        p = self.cli("--skills", self.skills, "--hooks-sol", self.hooks, "--remappings", self.rm, "--no-compile")
        self.assertEqual(p.returncode, 2)

    def test_missing_args_exit_2(self):
        self.assertEqual(self.cli().returncode, 2)

    def test_compile_without_project_exit_2(self):
        p = self.cli("--skills", self.skills, "--hooks-sol", self.hooks, "--remappings", self.rm)
        self.assertEqual(p.returncode, 2)

    def test_forge_missing_exit_2(self):
        project = os.path.join(self.td, "proj")
        write(os.path.join(project, "foundry.toml"), "[profile.default]\n")
        os.makedirs(os.path.join(project, "lib"))
        env = {"PATH": "/nonexistent"}  # no forge anywhere (the interpreter is invoked by absolute path)
        p = subprocess.run([sys.executable, os.path.join(ROOT, "skillcheck.py"), "--skills", self.skills,
                            "--hooks-sol", self.hooks, "--remappings", self.rm, "--project", project],
                           capture_output=True, text=True, env=env)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("forge not found", p.stderr)

    def test_remappings_optional_facts_still_checked(self):
        self.md("The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 10) is dangerous.")
        p = self.cli("--skills", self.skills, "--hooks-sol", self.hooks, "--no-compile")
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("import check skipped", p.stderr)

    def test_remappings_optional_clean_exits_0(self):
        self.md("The `BEFORE_SWAP_RETURNS_DELTA` permission (bit 3) is dangerous.")
        p = self.cli("--skills", self.skills, "--hooks-sol", self.hooks, "--no-compile")
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_version_flag(self):
        p = self.cli("--version")
        self.assertEqual(p.returncode, 0)
        self.assertIn(sc.__version__, p.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
