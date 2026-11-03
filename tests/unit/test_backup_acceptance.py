"""Fresh-project proof cannot mutate arbitrary Compose projects."""
from unittest.mock import patch

import pytest

from cli import backup_acceptance


@pytest.mark.parametrize("source,target", [
    ("mjobs-personal", "mjobs-fixture-0123456789ab"),
    ("mjobs-fixture-0123456789ab", "mjobs-personal"),
    ("mjobs-fixture-0123456789ab", "mjobs-fixture-0123456789ab"),
])
def test_unsafe_or_same_project_is_rejected_before_docker(source, target, tmp_path):
    with patch.object(backup_acceptance.recovery, "preflight_recovery_targets") as preflight:
        with pytest.raises(ValueError):
            backup_acceptance.run(tmp_path / "missing.json", tmp_path / "evidence.json",
                                  source, target, 18700, tmp_path / "backup")
        preflight.assert_not_called()


def test_occupied_target_is_rejected_before_stopping_source(tmp_path):
    import json
    baseline = {"profile_id": "fictional-profile", "application_id": "fictional-app",
                "operation_id": "fictional-operation", "request_id": "fictional-request",
                "external_submission_enabled": False,
                "confirmation": {"application_id": "fictional-app", "receipt": "MOCK-fictional",
                                 "submission_count": 1}}
    baseline_path = tmp_path / "baseline.json"
    baseline_path.write_text(json.dumps(baseline), encoding="utf-8")
    with patch.object(backup_acceptance.recovery, "preflight_recovery_targets"), \
         patch.object(backup_acceptance.local_backup, "_config", return_value={}), \
         patch.object(backup_acceptance.local_backup, "_compose", return_value=b"occupied-target-container"), \
         patch.object(backup_acceptance.local_backup, "_volumes", return_value=[]), \
         patch.object(backup_acceptance.recovery, "compose") as mutate:
        with pytest.raises(backup_acceptance.local_backup.BackupError, match="already exists"):
            backup_acceptance.run(baseline_path, tmp_path / "evidence.json",
                                  "mjobs-fixture-0123456789ab", "mjobs-fixture-abcdef012345", 18700,
                                  tmp_path / "backup")
        mutate.assert_not_called()


def test_legacy_v2_job_inventory_is_rejected_before_backup_runtime_access(tmp_path):
    import json

    # A saturated v2 inventory is internally consistent but cannot prove completeness.
    rows = [{"job_id": f"legacy-job-{index:03d}", "canonical_sha256": "1" * 64,
             "source_copy_count": 1, "source_copies_sha256": "2" * 64}
            for index in range(200)]
    inventory = {"schema_version": 2, "profile_id": "fictional-profile",
                 "job_count": 200, "source_copy_count": 200, "jobs": rows,
                 "sha256": backup_acceptance.recovery._inventory_digest(rows)}
    baseline = {"profile_id": "fictional-profile", "application_id": "fictional-app",
                "operation_id": "fictional-operation", "request_id": "fictional-request",
                "external_submission_enabled": False, "job_inventory": inventory,
                "confirmation": {"application_id": "fictional-app", "receipt": "MOCK-fictional",
                                 "submission_count": 1}}
    baseline_path = tmp_path / "baseline-v2.json"
    baseline_text = json.dumps(baseline)
    baseline_path.write_text(baseline_text, encoding="utf-8")
    evidence_path = tmp_path / "evidence.json"
    archive = tmp_path / "backup"

    with (
        patch.object(backup_acceptance.recovery, "resolve_compose_files",
                     side_effect=AssertionError("Legacy inventory reached runtime")) as compose_files,
        patch.object(backup_acceptance.recovery, "preflight_recovery_targets") as preflight,
        patch.object(backup_acceptance.local_backup, "_config") as config,
        patch.object(backup_acceptance.local_backup, "_compose") as target,
        patch.object(backup_acceptance.local_backup, "_volumes") as volumes,
        patch.object(backup_acceptance.httpx, "Client") as client,
        patch.object(backup_acceptance.recovery, "compose") as mutate,
        patch.object(backup_acceptance, "run_public_cli") as archive_commands,
    ):
        with pytest.raises(
            ValueError, match="^Legacy public job inventory requires a fresh complete baseline$"
        ):
            backup_acceptance.run(
                baseline_path, evidence_path,
                "mjobs-fixture-0123456789ab", "mjobs-fixture-abcdef012345", 18700, archive,
            )
        for boundary in (compose_files, preflight, config, target, volumes,
                         client, mutate, archive_commands):
            boundary.assert_not_called()

    assert baseline_path.read_text(encoding="utf-8") == baseline_text
    assert not evidence_path.exists()
    assert not archive.exists()
