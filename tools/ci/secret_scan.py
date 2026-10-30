"""Run a checksum-pinned Gitleaks release and retain only redacted local results."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import platform
import subprocess
import tarfile
import tempfile
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]


def main() -> int:
    config = json.loads((ROOT / '.github/secret-scanner.json').read_text('utf-8'))
    if platform.system() != 'Linux' or platform.machine() not in {'x86_64', 'AMD64'}:
        raise SystemExit('This pinned runner binary requires Linux x64; use the verified local platform binary for other hosts.')
    url = f'https://github.com/gitleaks/gitleaks/releases/download/v{config["version"]}/{config["archive"]}'
    with tempfile.TemporaryDirectory(prefix='mjobs-secret-scan-') as directory:
        root = Path(directory)
        archive = root / 'scanner.tar.gz'
        with urlopen(url, timeout=60) as response:
            data = response.read(50 * 1024 * 1024 + 1)
        if len(data) > 50 * 1024 * 1024 or hashlib.sha256(data).hexdigest() != config['sha256']:
            raise RuntimeError('Downloaded scanner does not match the reviewed SHA-256')
        archive.write_bytes(data)
        with tarfile.open(archive, 'r:gz') as package:
            member = package.getmember('gitleaks')
            if not member.isfile() or member.size > 100 * 1024 * 1024:
                raise RuntimeError('Unexpected scanner archive member')
            stream = package.extractfile(member)
            if stream is None:
                raise RuntimeError('Scanner binary missing')
            executable = root / 'gitleaks'
            executable.write_bytes(stream.read())
            executable.chmod(0o700)
        output = ROOT / 'outputs/ci'
        output.mkdir(parents=True, exist_ok=True)
        result = subprocess.run([str(executable), 'git', str(ROOT), '--log-opts=--all',
                                 '--redact', '--no-banner', '--report-format', 'json',
                                 '--report-path', str(output / 'secret-scan-redacted.json'),
                                 '--gitleaks-ignore-path', str(ROOT / '.gitleaksignore')],
                                cwd=ROOT, timeout=600,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        print('Scanner result:', 'PASS' if result.returncode == 0 else 'FAIL - investigate redacted findings')
        return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
