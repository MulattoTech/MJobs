# Ollama local-GPU lane handoff

Status: **IDLE_READY**

Runner: OpenCode 1.18.35 with the local Ollama provider.
Primary model: `ollama/gpt-oss:20b`.
Fallback model for a harder bounded task: `ollama/gemma4:26b`.

The machine has Ollama 0.35.1 running on an AMD Radeon RX 7900 XTX. Both installed
models advertise tool use and thinking. OpenCode is configured at the user level to
reach `http://127.0.0.1:11434/v1`; no cloud model or API key is required.

Before every issue selection, read both cloud-agent claim files plus live
worktrees/GitHub. Create a dedicated `ollama/*` branch/worktree before source
mutation. Write only Ollama lane files while implementing. Canonical planning and
`main` require the integration lock.

The initial assigned candidate is issue #318 / MJ-REQ-063.D01 Greenhouse ATS
detection, intentionally separated from Codex's AI lane and Antigravity's source
adapter lane.

Prefer bounded, deterministic, well-tested tasks. Escalate an ambiguous/cross-cutting
issue to Codex or Antigravity rather than guessing.
