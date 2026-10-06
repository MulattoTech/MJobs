from pathlib import Path
import tempfile
import unittest

from tools.verification.host_safety import validate_host_safety


class HostSafetyTests(unittest.TestCase):
    def repository(self, root: str, script: str) -> Path:
        path = Path(root, ".devcontainer", "setup.sh")
        path.parent.mkdir(parents=True)
        path.write_text(script, encoding="utf-8")
        return Path(root)

    def test_allows_project_scoped_dependency_and_container_setup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(
                directory,
                "python -m pip install -r requirements.lock\n"
                "npm ci --prefix apps/web\n"
                "docker compose up -d --build\n"
                "python -m playwright install-deps chromium\n",
            )

            self.assertEqual(validate_host_safety(root), [])

    def test_rejects_broad_operating_system_upgrades(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory, "winget upgrade --all\n")

            self.assertIn(
                "HOST_WIDE_PACKAGE_UPGRADE:.devcontainer/setup.sh:1",
                validate_host_safety(root),
            )

    def test_rejects_driver_firmware_and_network_reconfiguration(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(
                directory,
                "pnputil /delete-driver oem42.inf /uninstall /force\n"
                "netsh winsock reset\n"
                "Set-NetAdapter -Name Ethernet -AdminStatus Disabled\n",
            )

            findings = validate_host_safety(root)
            self.assertIn("HOST_DRIVER_OR_FIRMWARE_CHANGE:.devcontainer/setup.sh:1", findings)
            self.assertIn("HOST_NETWORK_RECONFIGURATION:.devcontainer/setup.sh:2", findings)
            self.assertIn("HOST_NETWORK_RECONFIGURATION:.devcontainer/setup.sh:3", findings)

    def test_rejects_host_reboot_and_broad_process_termination(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(
                directory,
                "Restart-Computer -Force\n"
                "taskkill /F /IM chrome.exe\n",
            )

            findings = validate_host_safety(root)
            self.assertIn("HOST_REBOOT:.devcontainer/setup.sh:1", findings)
            self.assertIn("BROAD_PROCESS_TERMINATION:.devcontainer/setup.sh:2", findings)

    def test_rejects_host_changes_in_package_scripts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.repository(directory, "echo safe\n")
            manifest = Path(root, "apps", "web", "package.json")
            manifest.parent.mkdir(parents=True)
            manifest.write_text(
                '{"scripts":{"build":"npm run compile", "clean":"shutdown /r /t 0"}}',
                encoding="utf-8",
            )

            self.assertIn(
                "HOST_REBOOT:apps/web/package.json:scripts.clean",
                validate_host_safety(root),
            )


if __name__ == "__main__":
    unittest.main()
