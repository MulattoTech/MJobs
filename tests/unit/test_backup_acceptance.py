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
