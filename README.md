# MJobs

Local-first job discovery, factual document preparation, and application tracking.
The current user-testing slice includes a connected web application, a public
Greenhouse read-only source, and a controlled fictional application fixture.
Real external submission is disabled.

With Docker Desktop running Linux containers, start from PowerShell:

```powershell
cd C:\Tools\Dev\MJobs
.\Start-MJobs.ps1
```

The command preserves local data, builds the application, applies migrations,
checks the workers, and opens http://127.0.0.1:18700. See
[the user-testing guide](docs/USER_TESTING.md) for the connected journey,
diagnostics, verification commands, and current limitations.

Repository operating instructions live in [AGENTS.md](AGENTS.md). The complete
requirements remain in the GitHub backlog and scoped [area specifications](docs/spec/areas).
This milestone does not represent completion of the full specification.
