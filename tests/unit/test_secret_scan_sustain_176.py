from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from tools.ci import secret_scan


SYNTHETIC_CANARY = "SYNTHETIC_SECRET_CANARY_176"


class _Response:
    def __init__(self, payload: bytes):
        self.payload = payload
        self.read_limits: list[int] = []

    def __enter__(self):
        return self

    def __exit__(self, _type, _value, _traceback):
        return False

    def read(self, limit: int) -> bytes:
        self.read_limits.append(limit)
        return self.payload[:limit]


def _archive(*, member_type: bytes = tarfile.REGTYPE) -> bytes:
    payload = b"synthetic scanner executable"
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as package:
        member = tarfile.TarInfo("gitleaks")
        member.type = member_type
        member.mode = 0o700
        if member_type == tarfile.REGTYPE:
            member.size = len(payload)
            package.addfile(member, io.BytesIO(payload))
        else:
            member.linkname = "elsewhere"
            package.addfile(member)
    return stream.getvalue()


class SecretScanSustain176Tests(unittest.TestCase):
    def _root(self, directory: str, archive: bytes) -> Path:
        root = Path(directory)
        (root / ".github").mkdir()
        (root / ".github/secret-scanner.json").write_text(json.dumps({
            "version": "0.0.176",
            "archive": "synthetic-scanner.tar.gz",
            "sha256": hashlib.sha256(archive).hexdigest(),
        }), encoding="utf-8")
        (root / ".gitleaksignore").write_text("", encoding="utf-8")
        return root

    def test_main_confines_child_output_and_retains_only_redacted_report(self):
        archive = _archive()
        response = _Response(archive)
        observed: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, archive)

            def fake_run(command, **kwargs):
                observed["command"] = command
                observed["kwargs"] = kwargs
                if kwargs.get("stdout") is None:
                    print(SYNTHETIC_CANARY)
                if kwargs.get("stderr") is None:
                    print(SYNTHETIC_CANARY, file=__import__("sys").stderr)
                report = Path(command[command.index("--report-path") + 1])
                report.write_text('[{"Secret":"REDACTED"}]', encoding="utf-8")
                return subprocess.CompletedProcess(command, 0)

            stdout = io.StringIO()
            stderr = io.StringIO()
            with patch.object(secret_scan, "ROOT", root), patch.object(
                    secret_scan.platform, "system", return_value="Linux"), patch.object(
                    secret_scan.platform, "machine", return_value="x86_64"), patch.object(
                    secret_scan, "urlopen", return_value=response) as download, patch.object(
                    secret_scan.subprocess, "run", side_effect=fake_run), redirect_stdout(stdout), redirect_stderr(stderr):
                self.assertEqual(secret_scan.main(), 0)

            command = observed["command"]
            kwargs = observed["kwargs"]
            self.assertEqual(download.call_args.kwargs, {"timeout": 60})
            self.assertEqual(response.read_limits, [50 * 1024 * 1024 + 1])
            self.assertIn("--redact", command)
            report_path = Path(command[command.index("--report-path") + 1])
            self.assertEqual(report_path, root / "outputs/ci/secret-scan-redacted.json")
            self.assertEqual(kwargs["timeout"], 600)
            self.assertIs(kwargs["stdout"], subprocess.DEVNULL)
            self.assertIs(kwargs["stderr"], subprocess.DEVNULL)
            self.assertEqual(stdout.getvalue(), "Scanner result: PASS\n")
            self.assertEqual(stderr.getvalue(), "")
            self.assertNotIn(SYNTHETIC_CANARY, stdout.getvalue() + stderr.getvalue())

    def test_checksum_mismatch_stops_before_archive_or_execution(self):
        archive = _archive()
        response = _Response(archive + b"tampered")
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, archive)
            with patch.object(secret_scan, "ROOT", root), patch.object(
                    secret_scan.platform, "system", return_value="Linux"), patch.object(
                    secret_scan.platform, "machine", return_value="x86_64"), patch.object(
                    secret_scan, "urlopen", return_value=response), patch.object(
                    secret_scan.tarfile, "open") as open_archive, patch.object(
                    secret_scan.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "reviewed SHA-256"):
                    secret_scan.main()
            open_archive.assert_not_called()
            run.assert_not_called()

    def test_unsafe_archive_member_type_stops_before_execution(self):
        archive = _archive(member_type=tarfile.SYMTYPE)
        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, archive)
            with patch.object(secret_scan, "ROOT", root), patch.object(
                    secret_scan.platform, "system", return_value="Linux"), patch.object(
                    secret_scan.platform, "machine", return_value="x86_64"), patch.object(
                    secret_scan, "urlopen", return_value=_Response(archive)), patch.object(
                    secret_scan.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "Unexpected scanner archive member"):
                    secret_scan.main()
            run.assert_not_called()

    def test_oversized_archive_member_stops_before_execution(self):
        archive = _archive()
        member = tarfile.TarInfo("gitleaks")
        member.type = tarfile.REGTYPE
        member.size = 100 * 1024 * 1024 + 1

        class ParsedArchive:
            def __enter__(self):
                return self

            def __exit__(self, _type, _value, _traceback):
                return False

            def getmember(self, name):
                if name != "gitleaks":
                    raise KeyError(name)
                return member

        with tempfile.TemporaryDirectory() as directory:
            root = self._root(directory, archive)
            with patch.object(secret_scan, "ROOT", root), patch.object(
                    secret_scan.platform, "system", return_value="Linux"), patch.object(
                    secret_scan.platform, "machine", return_value="x86_64"), patch.object(
                    secret_scan, "urlopen", return_value=_Response(archive)), patch.object(
                    secret_scan.tarfile, "open", return_value=ParsedArchive()), patch.object(
                    secret_scan.subprocess, "run") as run:
                with self.assertRaisesRegex(RuntimeError, "Unexpected scanner archive member"):
                    secret_scan.main()
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
