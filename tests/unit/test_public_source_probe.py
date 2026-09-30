"""A controlled live read cannot mutate the wrong runtime."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from cli.public_source_probe import run


class PublicSourceProbeTests(unittest.TestCase):
    def test_required_personal_mode_aborts_before_profile_or_source_write(self):
        for settings in (
            {"operation_mode": "FIXTURE", "external_submission_enabled": False},
            {"operation_mode": "PERSONAL", "external_submission_enabled": True},
        ):
            with self.subTest(settings=settings), tempfile.TemporaryDirectory() as directory:
                requests = []

                def respond(request):
                    requests.append((request.method, request.url.path))
                    if request.url.path == "/settings":
                        return httpx.Response(200, json=settings)
                    return httpx.Response(500)

                client = httpx.Client(base_url="http://127.0.0.1:18701", transport=httpx.MockTransport(respond))
                evidence = Path(directory) / "live-read.json"
                with patch("cli.public_source_probe.httpx.Client", return_value=client):
                    failure = None
                    try:
                        run("http://127.0.0.1:18701", "greenhouse", evidence, required_mode="PERSONAL")
                    except Exception as error:
                        failure = error
                self.assertIsInstance(failure, RuntimeError)
                self.assertIn("personal operation", str(failure))
                self.assertEqual(requests, [("GET", "/settings")])
                self.assertFalse(evidence.exists())


if __name__ == "__main__":
    unittest.main()
