"""MJ-REQ-280 static admission contracts; never execute npm or contact a service.

Passing these checks proves configuration/discovery only, not lint, formatting,
TypeScript compilation or component behavior. Missing tooling is intentionally
RED, not skipped or xfailed. Production config is outside this task's ownership.
"""

import json
from pathlib import Path
import re
import shlex
import unittest


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "apps" / "web"
STRICT_FLAGS = (
    "noImplicitAny", "noImplicitThis", "strictNullChecks",
    "strictFunctionTypes", "strictBindCallApply", "strictPropertyInitialization",
    "strictBuiltinIteratorReturn", "useUnknownInCatchVariables", "alwaysStrict",
)


def strict_options(options):
    return options.get("strict") is True and not any(
        options.get(flag) is False for flag in STRICT_FLAGS
    )


def local_check_command(command, kind):
    """Conservative allowlist for finite, non-mutating package commands.

    This inspects text only: even an admitted command is never executed here.
    New command shapes require deliberate review rather than a shell fallback.
    """
    if not isinstance(command, str) or re.search(r"[;|`$<>\r\n]", command):
        return False
    if kind == "build":
        return command.strip() == "tsc --noEmit && vite build"
    if "&" in command:
        return False
    try:
        words = shlex.split(command)
    except ValueError:
        return False
    if kind == "test":
        return words == ["vitest", "run"]
    if kind == "lint":
        return words in (["eslint", "."], ["eslint", "src"],
                         ["eslint", ".", "--max-warnings", "0"],
                         ["eslint", "src", "--max-warnings", "0"])
    if kind == "format":
        return words in (["prettier", "--check", "."],
                         ["prettier", "--check", "src"])
    return False


class TypeScriptQualityContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.package = json.loads((WEB / "package.json").read_text(encoding="utf-8"))
        cls.tsconfig = json.loads((WEB / "tsconfig.json").read_text(encoding="utf-8"))
        cls.scripts = cls.package.get("scripts", {})
        cls.dependencies = {
            **cls.package.get("dependencies", {}),
            **cls.package.get("devDependencies", {}),
        }

    def test_strict_typescript_covers_components_without_relaxed_strict_flags(self):
        self.assertTrue(strict_options(self.tsconfig.get("compilerOptions", {})))
        self.assertIn("src", self.tsconfig.get("include", []))
        self.assertNotIn("extends", self.tsconfig, "Resolve inherited config before admission")
        self.assertFalse(self.tsconfig.get("exclude"), "Audit excluded source before admission")
        self.assertIn("typescript", self.dependencies)

    def test_lint_script_is_local_nonfixing_and_finite(self):
        self.assertTrue(local_check_command(self.scripts.get("lint"), "lint"),
                        "Missing/rejected lint script: require local non-fixing ESLint")

    def test_formatter_check_script_is_local_nonwriting_and_finite(self):
        command = self.scripts.get("format:check", self.scripts.get("format-check"))
        self.assertTrue(local_check_command(command, "format"),
                        "Missing/rejected format-check script: require Prettier --check")

    def test_component_test_script_is_one_shot(self):
        self.assertTrue(local_check_command(self.scripts.get("test"), "test"))
        self.assertIn("vitest", self.dependencies)

    def test_build_script_typechecks_before_bundling(self):
        self.assertTrue(local_check_command(self.scripts.get("build"), "build"))
        self.assertIn("vite", self.dependencies)

    def test_quality_scripts_do_not_have_implicit_lifecycle_hooks(self):
        for script in ("lint", "format:check", "format-check", "test", "build"):
            for prefix in ("pre", "post"):
                with self.subTest(script=script, prefix=prefix):
                    self.assertNotIn(prefix + script, self.scripts)

    def test_eslint_dependency_is_declared(self):
        self.assertIn("eslint", self.dependencies, "ESLint must be a declared local dependency")

    def test_eslint_config_exists(self):
        names = ("eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
                 "eslint.config.ts", ".eslintrc", ".eslintrc.json", ".eslintrc.cjs")
        self.assertTrue(any((directory / name).is_file()
                            for directory in (WEB, ROOT) for name in names)
                        or bool(self.package.get("eslintConfig")),
                        "No declared ESLint configuration; existence alone is not runtime proof")

    def test_formatter_dependency_is_declared(self):
        self.assertIn("prettier", self.dependencies, "Prettier must be a declared local dependency")

    def test_formatter_config_exists(self):
        names = (".prettierrc", ".prettierrc.json", ".prettierrc.yml", ".prettierrc.yaml",
                 ".prettierrc.js", ".prettierrc.cjs", "prettier.config.js",
                 "prettier.config.cjs", "prettier.config.mjs")
        self.assertTrue(any((directory / name).is_file()
                            for directory in (WEB, ROOT) for name in names)
                        or bool(self.package.get("prettier")),
                        "No declared formatter configuration")

    def test_component_tests_are_discoverable_by_default_vitest_patterns(self):
        config = (WEB / "vite.config.ts").read_text(encoding="utf-8")
        self.assertNotRegex(config, r"\btest\s*:",
                            "Explicit test configuration requires discovery-contract review")
        candidates = sorted((WEB / "src").rglob("*.test.tsx"))
        components = []
        for path in candidates:
            content = path.read_text(encoding="utf-8")
            if ("from 'vitest'" in content or 'from "vitest"' in content) and (
                "react-dom" in content or "@testing-library/react" in content
            ):
                components.append(path)
        self.assertTrue(components, "No discoverable React component test found")


class QualityOracleNegativeControls(unittest.TestCase):
    def test_strict_oracle_rejects_disabled_and_overridden_modes(self):
        self.assertTrue(strict_options({"strict": True}))
        for options in ({}, {"strict": False}, {"strict": "true"},
                        {"strict": True, "strictNullChecks": False}):
            with self.subTest(options=options):
                self.assertFalse(strict_options(options))

    def test_script_oracle_accepts_local_one_shot_commands(self):
        for kind, command in (("lint", "eslint ."), ("format", "prettier --check ."),
                              ("test", "vitest run"),
                              ("build", "tsc --noEmit && vite build")):
            with self.subTest(kind=kind):
                self.assertTrue(local_check_command(command, kind))

    def test_script_oracle_rejects_network_watch_mutation_and_shell_fallbacks(self):
        for command in (None, "", "npx eslint .", "npm install", "vitest",
                        "vitest --watch", "vitest run || true", "eslint . --fix",
                        "prettier --write .", "curl https://example.invalid",
                        "eslint .; echo pass", "eslint . && npm install",
                        "vite build", "tsc --noEmit || vite build"):
            for kind in ("lint", "format", "test", "build"):
                with self.subTest(command=command, kind=kind):
                    self.assertFalse(local_check_command(command, kind))
