"""Offline regression checks for the repository Python quality contract.

This module deliberately does not install tools or make network requests. A
missing configured executable is reported as a test failure rather than being
silently treated as an available quality gate.
"""
from __future__ import annotations

import importlib.util
import shutil
import sys
import tomllib
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATHS = (
    "pyproject.toml", ".ruff.toml", "ruff.toml", "mypy.ini",
    "pyrightconfig.json", "setup.cfg", "tox.ini",
)
PUBLIC_TYPED_MODULES = (
    "apps/api/application_multipage_state.py",
    "services/operations/pipeline_workbench.py",
    "services/operations/pipeline_actions.py",
)


def _available(command: str) -> bool:
    """Check an already-installed command or importable ``python -m`` tool."""
    return shutil.which(command) is not None or importlib.util.find_spec(command) is not None


def _quality_configuration() -> tuple[str, str]:
    paths = [ROOT / path for path in CONFIG_PATHS if (ROOT / path).is_file()]
    return "\n".join(path.relative_to(ROOT).as_posix() for path in paths), "\n".join(
        path.read_text(encoding="utf-8") for path in paths
    )


class PythonQualityContractTests(unittest.TestCase):
    def test_supported_python_and_pytest_are_executable_and_typed_modules_compile(self):
        self.assertGreaterEqual(
            sys.version_info[:2], (3, 12),
            f"Python >=3.12 required; got {sys.version.split()[0]}",
        )
        self.assertTrue(
            shutil.which("pytest") or importlib.util.find_spec("pytest"),
            "pytest executable/module is unavailable; no install was attempted",
        )

        for relative_path in PUBLIC_TYPED_MODULES:
            with self.subTest(module=relative_path):
                source = (ROOT / relative_path).read_text(encoding="utf-8")
                # In-memory compilation validates syntax without imports,
                # bytecode writes, browser access, or any external services.
                compile(source, relative_path, "exec")

    def test_ruff_and_practical_typecheck_have_offline_commands_and_exclusions(self):
        config_names, config_text = _quality_configuration()
        requirements = (ROOT / ".github/test-requirements.txt").read_text(encoding="utf-8")
        test_requirements = requirements.casefold()
        workflow = (ROOT / ".github/workflows/ci.yml").read_text(
            encoding="utf-8",
        ).casefold()
        config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        findings = []

        if not _available("ruff"):
            findings.append("ruff executable/module unavailable (offline check; not installed)")
        if not config_names or not any(
            name in config_text.casefold() for name in ("ruff", "[tool.ruff]")
        ):
            findings.append("no tracked Ruff configuration found")
        if "ruff" not in test_requirements or "ruff" not in workflow:
            findings.append("CI has no configured Ruff dependency and command")

        type_tools = ("mypy", "pyright")
        configured_type_tools = [
            tool for tool in type_tools
            if _available(tool) and tool in test_requirements and tool in workflow
        ]
        if not configured_type_tools:
            findings.append(
                "no available mypy/pyright executable with a CI dependency and command"
            )
        if not any(tool in config_text.casefold() for tool in type_tools):
            findings.append("no tracked mypy/pyright configuration found")
        if not all(marker in config_text.casefold() for marker in ("generated", "private")):
            findings.append("quality configuration does not exclude generated and private paths")

        if config["tool"]["mjobs-python-quality"]["requires-python"] != ">=3.12":
            findings.append("quality configuration does not declare Python >=3.12")
        if config["tool"]["ruff"]["target-version"] != "py312":
            findings.append("Ruff target is not Python 3.12")
        if config["tool"]["mypy"]["python_version"] != "3.12":
            findings.append("mypy target is not Python 3.12")
        if config["tool"]["mypy"].get("follow_imports") != "skip":
            findings.append("mypy must skip imported modules to keep the configured check bounded")
        if tuple(config["tool"]["mypy"]["files"]) != PUBLIC_TYPED_MODULES:
            findings.append("mypy scope differs from the three bounded public modules")

        pinned = {
            line.partition("==")[0].casefold().replace("_", "-"): line.partition("==")[2]
            for line in requirements.splitlines()
            if "==" in line and not line.lstrip().startswith("#")
        }
        expected_pins = {
            "ruff": "0.17.2",
            "mypy": "2.4.0",
            "mypy-extensions": "1.1.0",
            "pathspec": "1.1.1",
            "librt": "0.16.0",
            "ast-serialize": "0.13.1",
            "typing-extensions": "4.16.0",
        }
        for package, version in expected_pins.items():
            if pinned.get(package) != version:
                findings.append(f"{package} is not pinned to {version}")

        required_commands = (
            "python -m ruff check " + " ".join(PUBLIC_TYPED_MODULES),
            "python -m mypy --config-file pyproject.toml",
        )
        for command in required_commands:
            if command.casefold() not in workflow:
                findings.append(f"CI is missing bounded quality command: {command}")
        if "python -m pytest -q tests/unit" not in workflow:
            findings.append("existing unit-test CI gate is missing")

        self.assertEqual(
            findings,
            [],
            "Python quality gates are RED/PARTIAL; configure tools and exclusions in a "
            "separately authorized scope (this test performs no install/network work): "
            + "; ".join(findings),
        )


if __name__ == "__main__":
    unittest.main()
