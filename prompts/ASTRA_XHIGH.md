> **Current budget policy:** GitHub Actions, hosted tests, bot/cloud-agent reviews
> and extra AI reviewer sessions are disabled by the user. Older hosted-review/CI
> gates and expired deadlines below are historical where they conflict with
> AGENTS.md's active local-only policy. Use explicit local verification; missing
> hosted checks are NOT_RUN, never PASS. Do not restore billing or automation.

# Astra XHigh: primary implementation session

Read prompts/ASTRA_DELIVERY_CONTRACT.md and execute it. Use the current model setting; a prompt does not change reasoning effort.

Your emphasis is economical delivery of the next coherent, dependency-ready feature or regression fix. Default to one write-heavy implementation lane. Delegate bounded repository exploration, test diagnosis or read-only review when it reduces the critical path; do not delegate a task whose coordination cost exceeds its implementation. Keep architecture changes local unless the acceptance contract demonstrates a need.

Inspect the lifecycle/profile prerequisites before choosing the next slice. After each exact-head-green merge, update the baseline and continue with the next useful gap. Prefer finishing accepted behavior over accumulating many partial branches. Leave every unclosed acceptance criterion explicit.

Use the installed relevant skills and tools through the common contract, not a ritual sequence of every skill. Preserve the disabled standalone subagent skill setting. End with merged PRs, satisfied criteria, test/CI receipts, unresolved work and next action. Never run this alongside another writable session in the same checkout.
