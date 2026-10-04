# Astra Max: difficult prerequisite and correctness session

Read prompts/ASTRA_DELIVERY_CONTRACT.md and execute it. Use the current model setting; a prompt does not change reasoning effort.

Your emphasis is the hardest high-value blocker on the next delivery path: data/provenance correctness, schema evolution, concurrency, recovery, authorization or a recurrent test/CI defect. Investigate the current repository and evidence, identify falsifiable failure hypotheses, and reproduce the real gap before redesigning anything. Use deeper reasoning to simplify the solution and reduce downstream rework, not to enlarge the scope.

Keep one implementation owner by default. A separate read-only reviewer should challenge invariants and edge cases. Add independent implementation only where ownership and dependency contracts are explicit. Deliver the working fix, affected database/browser/regression evidence, and exact-head CI; then continue with the next dependent slice rather than ending with a research report.

Use the installed relevant skills and tools through the common contract. Do not create another memory hierarchy, turn on disabled skills, or run all expensive checks after every edit. If the bottleneck is routine independent work rather than a hard reasoning problem, record that and proceed proportionately instead of manufacturing complexity.
