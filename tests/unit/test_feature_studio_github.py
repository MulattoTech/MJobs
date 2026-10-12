"""Repository-scoped deployment and webhook safety."""
import hashlib
import hmac
from pathlib import Path

import pytest

from tools.feature_studio.github_sync import verify_webhook, rollout_preview, rollout_repo


def test_webhook_signature_and_rejection():
    body=b'{"action":"opened"}'
    secret=b"local-test-secret"
    signature="sha256="+hmac.new(secret,body,hashlib.sha256).hexdigest()
    assert verify_webhook(body,signature,secret)
    assert not verify_webhook(body+b"!",signature,secret)
    assert not verify_webhook(body,signature,b"")
    assert not verify_webhook(body,"sha1=deadbeef",secret)


def test_rollout_preview_excludes_archived_repos():
    report=rollout_preview([{"repository":"MulattoTech/MJobs","archived":False},
                            {"repository":"MulattoTech/Legacy","archived":True}])
    assert report["eligible_repositories"]==["MulattoTech/MJobs"]
    assert report["executed_rollout"]==0
    assert report["approval_required"]


def test_rollout_rejects_invalid_identity_before_git():
    with pytest.raises(ValueError,match="Invalid repository"):
        rollout_repo("../wrong",source=Path("never-read"))
