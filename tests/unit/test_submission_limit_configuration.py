"""MJ-REQ-275: bounded deployment settings reach actual admission consumers."""

import json
import os
from pathlib import Path
import shutil
import subprocess
from unittest.mock import Mock, patch

import pytest

from apps.api.main import create_app
from services.applications.configuration import configured_submission_policy
from tests.unit.test_configuration_surface_sustain_276 import _public_settings
from workers import main as worker_main


KEYS = ("MJOBS_SUBMISSION_HOURLY_LIMIT", "MJOBS_SUBMISSION_DAILY_LIMIT",
        "MJOBS_SUBMISSION_COMPANY_LIMIT")


def test_defaults_preserve_existing_caps_and_external_submission_off():
    policy = configured_submission_policy({})
    assert (policy.hourly_limit, policy.daily_limit, policy.company_limit) == (3, 10, 1)
    assert policy.external_submissions_enabled is False


@pytest.mark.parametrize("name", KEYS)
@pytest.mark.parametrize("value", ("", "-1", "1.0", "1e2", "+1", " 1", "1 ",
                                    "true", "10001", "999999", "\u0661", None, True))
def test_invalid_values_fail_with_fixed_safe_diagnostic(name, value):
    with pytest.raises(ValueError, match=f"^{name}_INVALID$"):
        configured_submission_policy({name: value})


def test_boundary_caps_do_not_change_other_policy_gates():
    policy = configured_submission_policy(dict(zip(KEYS, ("0", "10000", "2"))))
    assert (policy.hourly_limit, policy.daily_limit, policy.company_limit) == (0, 10000, 2)
    assert (policy.minimum_match, policy.minimum_confidence) == (80, 80)
    assert policy.external_submissions_enabled is False


def test_invalid_configuration_fails_before_worker_or_api_startup_effects():
    with patch.dict(os.environ, {KEYS[0]: "private-invalid-canary"}), \
            patch("workers.main.Store") as store_factory, \
            patch("workers.main.Redis.from_url") as redis, \
            patch("workers.main.threading.Thread") as thread:
        with pytest.raises(ValueError, match=f"^{KEYS[0]}_INVALID$"):
            worker_main.main()
        store_factory.assert_not_called()
        redis.assert_not_called()
        thread.assert_not_called()
        store = Mock()
        with pytest.raises(ValueError, match=f"^{KEYS[0]}_INVALID$"):
            create_app(store, "http://127.0.0.1:1", Path("unused"))
        store.bind_workspace_mode.assert_not_called()


@pytest.mark.parametrize("configured", (None, ("0", "12", "2"), ("", "12", "2")))
def test_compose_forwards_same_caps_to_api_and_all_workers(tmp_path, configured):
    docker = shutil.which("docker")
    if docker is None:
        pytest.skip("Docker Compose CLI unavailable; parser and admission tests still run")
    root = Path(__file__).resolve().parents[2]
    environment = {key: value for key, value in os.environ.items() if key not in KEYS}
    environment.update(POSTGRES_PASSWORD="configuration-test-only",
                       MJOBS_CREDENTIAL_ENCRYPTION_KEY="configuration-test-only")
    expected = dict(zip(KEYS, configured or ("3", "10", "1")))
    if configured is not None:
        environment.update(expected)
    env_file = tmp_path / "compose.env"
    env_file.write_text("", encoding="utf-8")
    result = subprocess.run(
        [docker, "compose", "--env-file", str(env_file), "-f", str(root / "compose.yml"),
         "config", "--format", "json"],
        env=environment, capture_output=True, text=True, timeout=30, check=True)
    services = json.loads(result.stdout)["services"]
    for name in ("api", "worker", "source-worker", "company-discovery-worker", "ai-worker", "document-worker"):
        assert {key: services[name]["environment"][key] for key in KEYS} == expected
    with patch.dict(os.environ, expected, clear=True):
        if expected[KEYS[0]] == "":
            with pytest.raises(ValueError, match=f"^{KEYS[0]}_INVALID$"):
                _public_settings(tmp_path)
            return
        settings = _public_settings(tmp_path)
    assert settings["submission_limits"] == dict(zip(
        ("hourly", "daily", "company"), map(int, expected.values())))
    assert settings["external_submission_enabled"] is False
