# Frontend quality checks

Scope: MJ-REQ-280 / issue #281. Run from the repository root with the Node 24
runtime used by CI. Dependencies are local to `apps/web` and locked by npm.

```sh
npm ci --prefix apps/web --no-audit --no-fund
npm run lint --prefix apps/web
npm run format:check --prefix apps/web
npm run quality:test --prefix apps/web
npm run typecheck --prefix apps/web
npm test --prefix apps/web
npm run build --prefix apps/web
```

`lint` runs ESLint on every TypeScript/TSX source, the Vite configuration and
JavaScript tooling, then retains the existing source-policy checks. The ESLint
rules reject unsafe dynamic execution, unreachable code, duplicate branches,
invalid `typeof` comparisons and other concrete mistakes. Its Babel ESTree parser
understands TypeScript/JSX syntax; it does not perform type analysis. Strict
TypeScript 7 type checking remains the separate required `typecheck` gate. The
currently published typescript-eslint peer range does not support TypeScript 7;
this setup neither downgrades TypeScript nor overrides dependency compatibility.

Prettier is pinned and configured for consistent formatting. Format the files
being edited using the local executable, for example:

```sh
npm exec --prefix apps/web -- prettier --config apps/web/prettier.config.mjs --write apps/web/src/example.tsx
```

Replace the example path with an actual file. `npm run format --prefix apps/web`
formats the whole frontend when an explicitly scoped formatting change is wanted.
The required `format:check` gate initially covers the tooling and formatter/linter
configuration; legacy UI files are not mass-reformatted by this implementation.
TypeScript and ESLint still check the entire frontend source set.

`quality:test` checks that the actual configured ESLint engine parses valid TSX
and rejects dynamic evaluation, unreachable statements and an invalid `typeof`
comparison. It also verifies Prettier's detection and stable formatting of
TypeScript. Component behavior continues to be covered by the existing Vitest
suite, with built-UI Playwright acceptance in the connected workflow.

Sources: [package scripts](../apps/web/package.json),
[ESLint configuration](../apps/web/eslint.config.mjs),
[strict TypeScript configuration](../apps/web/tsconfig.json),
[required CI](../.github/workflows/ci.yml).
