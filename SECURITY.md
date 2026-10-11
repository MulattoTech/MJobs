# Security policy

MJobs is a developer preview handling potentially sensitive career information. Public code, successful CI and a completed CodeQL run do not constitute production-security certification. Existing open findings are tracked rather than silently dismissed.

Use GitHub **Security → Advisories → Report a vulnerability** for private reports. Private vulnerability reporting is enabled. Include affected commit, impact and a minimal synthetic reproduction; do not put exploit payloads, real resumes, credentials, cookies or private application records in public issues.

CI runs on disposable GitHub-hosted machines with synthetic data and least-privilege permissions. Public PR jobs do not receive personal secrets or a write token. Never connect an untrusted public workflow to the maintainer's workstation.

Source control is not a secret store. If a credential is exposed, revoke/rotate it through its provider before considering history cleanup. Preserve necessary private recovery evidence. Removing a public tag, release or commit cannot recall copies already downloaded.

[Safe installation](docs/INSTALLATION.md) and [automation controls](docs/maintainers/AUTOMATION.md) describe the supported boundaries. External job submission remains off.
