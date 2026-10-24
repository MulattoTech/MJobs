"""Answer exports must stay inside direct, private application paths."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace

from fastapi.testclient import TestClient

from apps.api.main import create_app
from packages.persistence.store import Store
from services.documents import answers


APPLICATION_ID = "a" * 64
PATH_ERROR = "ANSWER_EXPORT_PATH_INVALID"


class _UnexpectedStore:
    @property
    def engine(self):
        raise AssertionError("store accessed before answer-export path validation")


class _Result:
    def __init__(self, value):
        self.value = value

    def mappings(self):
        return self

    def one_or_none(self):
        return self.value

    def scalar_one_or_none(self):
        return self.value


class _Connection:
    def __init__(self):
        self.dialect = SimpleNamespace(name="sqlite")
        self.results = iter((
            {"profile_id": "fixture"},
            "fixture",
            None,
            None,
            None,
        ))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def exec_driver_sql(self, _statement):
        return None

    def execute(self, _statement, _parameters=None):
        return _Result(next(self.results))


class _Engine:
    dialect = SimpleNamespace(name="sqlite")

    def begin(self):
        return _Connection()


class _Store:
    engine = _Engine()

    def profile(self, profile_id, connection=None):
        if profile_id != "fixture" or connection is None:
            raise AssertionError("unexpected profile lookup")
        return {"id": profile_id, "version": "v1", "answers": []}

    def _audit(self, *_args, **_kwargs):
        return None


class AnswerExportPathSecurityTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="mjobs-answer-path-")
        self.addCleanup(self.directory.cleanup)
        self.workspace = Path(self.directory.name)

    def test_application_id_validator_accepts_safe_persisted_identifiers(self):
        for application_id in (
            "a",
            "application-fixture",
            "fixture_1",
            "a" * 128,
        ):
            with self.subTest(application_id=application_id):
                self.assertEqual(
                    answers.validate_answer_export_application_id(application_id),
                    application_id,
                )

    def test_application_id_validator_rejects_non_basename_identifiers(self):
        for application_id in (
            "",
            ".",
            "..",
            "nested/application",
            "..\\outside",
            "A" * 64,
            "caf\N{LATIN SMALL LETTER E WITH ACUTE}",
            "a" * 129,
        ):
            with self.subTest(application_id=application_id):
                with self.assertRaisesRegex(ValueError, rf"^{PATH_ERROR}$"):
                    answers.validate_answer_export_application_id(application_id)

    def test_public_route_rejects_malformed_id_before_store_or_filesystem_access(self):
        store = Store(f"sqlite:///{(self.workspace / 'route.db').as_posix()}")
        store.migrate()
        self.addCleanup(store.engine.dispose)
        payload = {
            "profile_id": "fixture",
            "items": [{
                "question": "Synthetic question?",
                "text": "Synthetic answer.",
                "reviewed": True,
            }],
        }

        with TestClient(create_app(
            store, "http://127.0.0.1:1", self.workspace,
        ), raise_server_exceptions=False) as client:
            with (patch.object(
                    store, "application",
                    side_effect=AssertionError("application store accessed before path validation"),
                ) as application,
                patch.object(
                    answers, "Path", side_effect=AssertionError("filesystem path constructed"),
                ) as path):
                response = client.post(
                    "/profiles/fixture/applications/nested%5Capplication/answers/export",
                    json=payload,
                )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json(), {"detail": PATH_ERROR})
        application.assert_not_called()
        path.assert_not_called()

    def _directory_alias(self, alias: Path, target: Path):
        if os.name == "nt":
            import _winapi

            _winapi.CreateJunction(str(target), str(alias))
            self.addCleanup(alias.rmdir)
        else:
            alias.symlink_to(target, target_is_directory=True)
            self.addCleanup(alias.unlink)

    def test_malformed_application_id_fails_before_path_or_store_access(self):
        with patch.object(
            answers, "Path", side_effect=AssertionError("path constructed"),
        ) as path:
            for application_id in (
                "nested/application",
                "..\\outside",
                "../outside",
                "",
                "a" * 129,
                "A" * 64,
            ):
                with self.subTest(application_id=application_id):
                    with self.assertRaisesRegex(ValueError, rf"^{PATH_ERROR}$"):
                        answers.export_answers(
                            _UnexpectedStore(), application_id, "/private/output", [],
                        )

        path.assert_not_called()

    def test_aliased_output_root_fails_before_store_or_outside_write(self):
        outside = self.workspace / "outside-private"
        outside.mkdir()
        sentinel = outside / "synthetic-private-answer"
        sentinel.write_text("unchanged", encoding="utf-8")
        alias = self.workspace / "output-alias"
        self._directory_alias(alias, outside)

        with self.assertRaisesRegex(ValueError, rf"^{PATH_ERROR}$"):
            answers.export_answers(_UnexpectedStore(), APPLICATION_ID, alias, [])

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((outside / APPLICATION_ID).exists())

    def test_aliased_application_directory_is_rejected_without_redirected_write(self):
        redirected = self.workspace / "redirected-application"
        redirected.mkdir()
        sentinel = redirected / "synthetic-private-answer"
        sentinel.write_text("unchanged", encoding="utf-8")
        self._directory_alias(self.workspace / APPLICATION_ID, redirected)

        with self.assertRaisesRegex(ValueError, rf"^{PATH_ERROR}$"):
            answers.export_answers(_UnexpectedStore(), APPLICATION_ID, self.workspace, [])

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertFalse((redirected / "answers").exists())

    def test_aliased_answers_directory_is_rejected_without_redirected_write(self):
        application_root = self.workspace / APPLICATION_ID
        application_root.mkdir()
        redirected = application_root / "redirected-answers"
        redirected.mkdir()
        sentinel = redirected / "synthetic-private-answer"
        sentinel.write_text("unchanged", encoding="utf-8")
        self._directory_alias(application_root / "answers", redirected)

        with self.assertRaisesRegex(ValueError, rf"^{PATH_ERROR}$"):
            answers.export_answers(_Store(), APPLICATION_ID, self.workspace, [])

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(redirected.glob("*.txt")), [])

    def test_aliased_version_directory_is_rejected_without_redirected_write(self):
        application_root = self.workspace / APPLICATION_ID
        answers_root = application_root / "answers"
        answers_root.mkdir(parents=True)
        redirected = application_root / "redirected-version"
        redirected.mkdir()
        sentinel = redirected / "synthetic-private-answer"
        sentinel.write_text("unchanged", encoding="utf-8")
        self._directory_alias(answers_root / "1", redirected)

        with self.assertRaisesRegex(ValueError, rf"^{PATH_ERROR}$"):
            answers.export_answers(_Store(), APPLICATION_ID, self.workspace, [])

        self.assertEqual(sentinel.read_text(encoding="utf-8"), "unchanged")
        self.assertEqual(list(redirected.glob("*.txt")), [])


if __name__ == "__main__":
    unittest.main()
