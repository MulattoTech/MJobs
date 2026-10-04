# Start a fresh MJobs delivery session

Choose one profile, not three concurrent owners of the same checkout.

Windows/local checkout:

```powershell
codex -C C:\Tools\Dev\MJobs -m gpt-6-astra -c 'model_reasoning_effort="xhigh"'
```

GitHub Codespace/Linux checkout, from the repository root:

```bash
codex -C "$PWD" -m gpt-6-astra -c 'model_reasoning_effort="xhigh"'
```

Replace `xhigh` with `max` or `ultra` for the other profiles. These values
were present in the local Codex 0.160.0 catalogue on 2026-10-04; recheck
`/model` after upgrades. Select the effort in the model picker if shell quoting
differs.

Then paste the matching instruction:

- XHigh: Read `prompts/ASTRA_XHIGH.md` and its shared delivery contract, then begin authorized implementation.
- Max: Read `prompts/ASTRA_MAX.md` and its shared delivery contract, then resolve the highest-value hard prerequisite and continue.
- Ultra: Read `prompts/ASTRA_ULTRA.md` and its shared delivery contract, then use bounded native delegation on genuinely independent ready work.

In a Codespace, treat that Codespace as the single writable MJobs checkout for
the delivery session; do not simultaneously mutate the Windows checkout.

Do not resume an old session rooted in an archived worktree. Normal
sandbox/approval controls remain enabled. The larger effort setting does not
authorize destructive actions, production access or increased concurrency.
