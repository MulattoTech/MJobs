"""Execute the Windows user launcher without starting containers or touching user data."""
import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
class StartMJobsTests(unittest.TestCase):
    def launch(self, existing=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / "Start-MJobs.ps1", root / "Start-MJobs.ps1")
            if existing is not None:
                (root / ".env").write_bytes(existing)
            # Replace only host/network boundaries. The real launcher generates,
            # publishes and preserves configuration, builds arguments and checks readiness.
            (root / "probe.ps1").write_text(r'''
$ErrorActionPreference = 'Stop'
$global:composeCalled = $false
function docker {
    if ($args[0] -eq 'info') { $global:LASTEXITCODE = 0; 'linux'; return }
    if ($args[0] -ne 'compose') { throw 'Unexpected Docker command' }
    $global:composeCalled = $true
    $values = @{}
    foreach ($line in [IO.File]::ReadAllLines((Join-Path $PSScriptRoot '.env'))) {
        if ($line.Contains('=')) { $parts = $line.Split('=', 2); $values[$parts[0]] = $parts[1] }
    }
    if (-not $values['POSTGRES_PASSWORD'] -or -not $values['MJOBS_CREDENTIAL_ENCRYPTION_KEY']) {
        $global:LASTEXITCODE = 1; return
    }
    $global:LASTEXITCODE = 0
}
function Invoke-RestMethod { @{ status = 'ready' } }
try {
    & (Join-Path $PSScriptRoot 'Start-MJobs.ps1') -NoOpen -NoBuild -Project mjobs-launch-test -Port 18810
    $status = 'ready'
} catch { $status = $_.Exception.Message }
@{ status = $status; compose_called = $global:composeCalled } | ConvertTo-Json -Compress
''', encoding="utf-8")
            environment = os.environ.copy()
            environment.pop("MJOBS_CREDENTIAL_ENCRYPTION_KEY", None)
            result = subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(root / "probe.ps1")],
                capture_output=True, text=True, timeout=30, env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            state = json.loads(result.stdout.splitlines()[-1])
            return state, (root / ".env").read_bytes(), result.stdout, list(root.glob(".env.init-*.tmp"))

    def test_fresh_launch_creates_complete_distinct_secrets_and_reaches_readiness(self):
        state, content, output, temporary = self.launch()
        self.assertEqual(state["status"], "ready")
        values = dict(line.split("=", 1) for line in content.decode().splitlines())
        self.assertEqual(len(base64.urlsafe_b64decode(values["MJOBS_CREDENTIAL_ENCRYPTION_KEY"])), 32)
        self.assertNotEqual(values["POSTGRES_PASSWORD"], values["MJOBS_CREDENTIAL_ENCRYPTION_KEY"])
        for secret in values.values():
            self.assertNotIn(secret, output)
        self.assertEqual(temporary, [])

    def test_existing_complete_configuration_remains_byte_identical(self):
        content = (b"# preserved private settings\r\nPOSTGRES_PASSWORD=synthetic-private-password\r\n"
                   b"MJOBS_CREDENTIAL_ENCRYPTION_KEY=" + base64.urlsafe_b64encode(b"k" * 32) + b"\r\n")
        state, after, output, _ = self.launch(content)
        self.assertEqual(state["status"], "ready")
        self.assertEqual(after, content)
        self.assertNotIn("synthetic-private-password", output)

    def test_legacy_missing_key_stops_before_compose_without_rotating_configuration(self):
        content = b"POSTGRES_PASSWORD=synthetic-private-password\n"
        state, after, output, _ = self.launch(content)
        self.assertFalse(state["compose_called"])
        self.assertIn("MJOBS_CREDENTIAL_ENCRYPTION_KEY", state["status"])
        self.assertIn("restore", state["status"].lower())
        self.assertEqual(after, content)
        self.assertNotIn("synthetic-private-password", output)

    def test_invalid_key_stops_before_compose_without_disclosing_it(self):
        for key in ("invalid-private-key", base64.urlsafe_b64encode(b"k" * 16).decode(),
                    base64.urlsafe_b64encode(b"k" * 32).decode() + " "):
            with self.subTest(key_length=len(key)):
                # Trailing file whitespace is dotenv syntax; whitespace inside
                # a quoted key is part of the value and must not be accepted.
                content = f'POSTGRES_PASSWORD=synthetic-password\nMJOBS_CREDENTIAL_ENCRYPTION_KEY="{key}"\n'.encode()
                state, after, output, _ = self.launch(content)
                self.assertFalse(state["compose_called"])
                self.assertIn("invalid", state["status"])
                self.assertEqual(after, content)
                self.assertNotIn(key, output)

    def test_valid_dotenv_quotes_and_comments_are_preserved(self):
        key = base64.urlsafe_b64encode(b"k" * 32).decode()
        for value in (f'"{key}" # private key', f"'{key}' # private key", f"{key} # private key"):
            with self.subTest(value_syntax=value[-14:]):
                content = f"POSTGRES_PASSWORD=synthetic-password\nMJOBS_CREDENTIAL_ENCRYPTION_KEY={value}\n".encode()
                state, after, output, _ = self.launch(content)
                self.assertEqual(state["status"], "ready")
                self.assertEqual(after, content)
                self.assertNotIn(key, output)


if __name__ == "__main__":
    unittest.main()
