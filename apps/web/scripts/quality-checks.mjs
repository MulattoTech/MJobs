import assert from 'node:assert/strict';
import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { ESLint } from 'eslint';
import * as prettier from 'prettier';
import repositoryConfig from '../eslint.config.mjs';

const cwd = fileURLToPath(new URL('../', import.meta.url));
const eslint = new ESLint({ cwd });
const filePath = 'src/quality-probe.tsx';

test('ESLint inspects real TypeScript and JSX syntax', async () => {
  const [result] = await eslint.lintText(
    'type Props = { name: string }; export const Label = ({ name }: Props) => <b>{name}</b>;',
    { filePath },
  );
  assert.equal(result.errorCount, 0, JSON.stringify(result.messages));
  assert.equal(result.warningCount, 0, JSON.stringify(result.messages));
});

for (const [name, source, ruleId] of [
  ['dynamic execution', 'export const value: unknown = eval("untrusted");', 'no-eval'],
  [
    'unreachable statements',
    'export function bad(): number { return 1; return 2; }',
    'no-unreachable',
  ],
  ['invalid typeof comparison', 'export const bad = typeof window === "strnig";', 'valid-typeof'],
]) {
  test(`ESLint rejects ${name} inside TypeScript`, async () => {
    const [result] = await eslint.lintText(source, { filePath });
    assert.ok(result.errorCount > 0);
    assert.ok(result.messages.some((message) => message.ruleId === ruleId));
    assert.equal(
      result.fatalErrorCount,
      0,
      'The parser must inspect, not reject, TypeScript syntax',
    );
  });
}

test('Prettier detects and deterministically formats TypeScript', async () => {
  const options = {
    ...(await prettier.resolveConfig(cwd + 'prettier.config.mjs')),
    parser: 'typescript',
  };
  const original = 'export const names:string[]=["one","two"]';
  assert.equal(await prettier.check(original, options), false);
  const formatted = await prettier.format(original, options);
  assert.equal(await prettier.check(formatted, options), true);
  assert.equal(await prettier.format(formatted, options), formatted);
  assert.match(formatted, /export const names: string\[\]/);
});

test('ESLint glob traversal retains nested sources, dot paths and ignore negation', async () => {
  const root = await mkdtemp(path.join(tmpdir(), 'mjobs-eslint-globs-'));
  const selected = [
    'src/root.ts',
    'src/deep/nested/Component.tsx',
    'src/.hidden/check.ts',
    'src/review/keep.ts',
    'src/review/skip.ts',
    'vite.config.ts',
  ];
  const ignored = ['dist/ignored.ts', 'coverage/ignored.ts', 'node_modules/ignored.ts'];
  try {
    for (const name of [...selected, ...ignored]) {
      const destination = path.join(root, name);
      await mkdir(path.dirname(destination), { recursive: true });
      await writeFile(destination, 'export const value: unknown = eval("probe");\n');
    }
    const probe = new ESLint({
      cwd: root,
      overrideConfigFile: true,
      overrideConfig: repositoryConfig,
    });
    const names = (results) =>
      results
        .map((result) => path.relative(root, result.filePath).split(path.sep).join('/'))
        .sort();
    const results = await probe.lintFiles(['.']);
    assert.deepEqual(names(results), [...selected].sort());
    for (const result of results) {
      assert.equal(result.fatalErrorCount, 0, JSON.stringify(result.messages));
      assert.ok(result.messages.some((message) => message.ruleId === 'no-eval'));
    }
    assert.deepEqual(
      names(await probe.lintFiles(['src/**/*.{ts,tsx}', 'vite.config.ts'])),
      [...selected].sort(),
    );
    for (const name of ignored) {
      assert.equal(await probe.isPathIgnored(path.join(root, name)), true);
    }
    const negated = new ESLint({
      cwd: root,
      overrideConfigFile: true,
      overrideConfig: [...repositoryConfig, { ignores: ['src/review/*', '!src/review/keep.ts'] }],
    });
    assert.deepEqual(
      names(await negated.lintFiles(['.'])),
      selected.filter((name) => name !== 'src/review/skip.ts').sort(),
    );
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});
