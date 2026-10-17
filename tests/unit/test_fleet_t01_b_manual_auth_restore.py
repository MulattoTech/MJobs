"""Restoring user auth metadata never promotes a session to authenticated."""
import unittest
from unittest.mock import patch

from services.applications import manual_auth
from services.applications import manual_auth_recovery


class ManualAuthCompletionError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ManualAuthCompletionRestoreUnitTests(unittest.TestCase):
    def test_restore_returns_current_bound_declaration_without_session_authority(self):
        restore = getattr(manual_auth_recovery, "restore_user_auth_completion", None)
        self.assertTrue(callable(restore), "durable user-auth restoration wrapper is missing")

        declaration = {
            "status": "USER_DECLARED",
            "profile_id": "fixture",
            "application_id": "synthetic-app",
            "review_operation_id": "review-1",
            "authenticated_session_available": False,
        }
        with patch.object(manual_auth, "ManualAuthCompletionError",
                          ManualAuthCompletionError, create=True), patch.object(
            manual_auth, "current_user_auth_completion", return_value=declaration,
            create=True,
        ) as current:
            restored = restore(
                object(), "fixture", "synthetic-app", "review-1",
            )

        self.assertEqual(restored, declaration)
        self.assertIs(restored["authenticated_session_available"], False)
        current.assert_called_once()

    def test_restore_rejects_mismatched_or_session_available_projection(self):
        restore = getattr(manual_auth_recovery, "restore_user_auth_completion", None)
        self.assertTrue(callable(restore), "durable user-auth restoration wrapper is missing")
        for declaration in (
            {"status": "USER_DECLARED", "profile_id": "other",
             "application_id": "synthetic-app", "review_operation_id": "review-1",
             "authenticated_session_available": False},
            {"status": "USER_DECLARED", "profile_id": "fixture",
             "application_id": "synthetic-app", "review_operation_id": "review-1",
             "authenticated_session_available": True},
        ):
            with self.subTest(declaration=declaration), patch.object(
                manual_auth, "ManualAuthCompletionError",
                ManualAuthCompletionError, create=True,
            ), patch.object(
                manual_auth, "current_user_auth_completion", return_value=declaration,
                create=True,
            ):
                with self.assertRaises(ManualAuthCompletionError) as raised:
                    restore(object(), "fixture", "synthetic-app", "review-1")
                self.assertEqual(raised.exception.code, "AUTH_COMPLETION_STALE")


if __name__ == "__main__":
    unittest.main()
