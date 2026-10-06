import json
from pathlib import Path
import tempfile
import unittest

from tools.verification.gpu_independence import validate_gpu_independence


class GpuIndependenceTests(unittest.TestCase):
    def make_repository(self, root):
        files = {
            "requirements.lock": "fastapi==0.1.0\n",
            "apps/web/package.json": json.dumps({"dependencies": {"react": "1.0.0"}}),
            "apps/web/package-lock.json": json.dumps({"packages": {"": {}, "node_modules/react": {"version": "1.0.0"}}}),
            "compose.yml": "services:\n  api:\n    image: app:local\n",
            "infra/docker/Dockerfile": "FROM python:3.12-slim\n",
            "apps/web/src/App.tsx": "export function App() { return null; }\n",
            "services/applications/mock_adapter.py": "headless=True, args=[\"--disable-gpu\"]\n",
        }
        for relative, content in files.items():
            path = Path(root, relative)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        return Path(root)

    def test_accepts_cpu_only_dependencies_compose_and_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            self.assertEqual(validate_gpu_independence(root), [])

    def test_rejects_required_gpu_runtime_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            (root / "requirements.lock").write_text("fastapi==0.1.0\ntorch==2.0.0\n", encoding="utf-8")
            self.assertIn("GPU_DEPENDENCY:requirements.lock:torch", validate_gpu_independence(root))

    def test_rejects_web_gpu_runtime_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            (root / "apps/web/package.json").write_text(
                json.dumps({"dependencies": {"onnxruntime-gpu": "1.0.0"}}), encoding="utf-8")
            (root / "apps/web/package-lock.json").write_text(
                json.dumps({"packages": {"": {}, "node_modules/onnxruntime-gpu": {"version": "1.0.0"}}}),
                encoding="utf-8",
            )
            self.assertIn(
                "GPU_DEPENDENCY:apps/web/package-lock.json:onnxruntime-gpu",
                validate_gpu_independence(root),
            )

    def test_rejects_gpu_device_requests_in_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            (root / "compose.yml").write_text(
                "services:\n  api:\n    image: app:local\n    deploy:\n      resources:\n        reservations:\n          devices:\n            - capabilities: [gpu]\n",
                encoding="utf-8",
            )
            self.assertIn("GPU_RUNTIME_CONFIG:compose.yml", validate_gpu_independence(root))

    def test_rejects_cuda_runtime_in_docker_image(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            (root / "infra/docker/Dockerfile").write_text(
                "FROM nvidia/cuda:12.0-runtime\n", encoding="utf-8")
            self.assertIn(
                "GPU_RUNTIME_CONFIG:infra/docker/Dockerfile",
                validate_gpu_independence(root),
            )

    def test_rejects_frontend_gpu_api_usage(self):
        with tempfile.TemporaryDirectory() as directory:
            root = self.make_repository(directory)
            (root / "apps/web/src/App.tsx").write_text(
                "const device = navigator.gpu;\n", encoding="utf-8")
            self.assertIn("GPU_API_USAGE:apps/web/src/App.tsx", validate_gpu_independence(root))


if __name__ == "__main__":
    unittest.main()
