# Resume with the agents server

The installed PowerShell `codex` function routes `codex agents` through the
private Unix socket and starts its server hidden when absent. This was verified
from the live launcher on 2026-09-30; use the function through a normal
profile-loaded PowerShell session.

In one terminal:

```powershell
cd C:\Tools\Dev\MJobs
codex agents
```

In another terminal, resume this exact conversation:

```powershell
codex resume 01a0ef75-a0bd-7b90-8725-6dacd68f6716 --remote 'unix://C:\Users\Dylan\.codex\app-server-control\agents-local.sock' -C C:\Tools\Dev\MJobs
```

The agents terminal is an interactive session browser. Avoid substituting
`codex.cmd agents`, which bypasses the installed PowerShell launcher.

The next goal is saved in [NEXT_USER_TESTING_GOAL.md](../prompts/NEXT_USER_TESTING_GOAL.md).
Paste that complete `/goal` line after resuming. It advances manual application
tracking, reviewed imports, document usability and CI integration; it retains
the separate explicit-enable requirement for real external submission.

The application itself starts with `./Start-MJobs.ps1` and opens
http://127.0.0.1:18700; see [USER_TESTING.md](USER_TESTING.md).
