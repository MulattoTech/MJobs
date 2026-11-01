import assert from 'node:assert/strict';
import { fileURLToPath } from 'node:url';
import test from 'node:test';
import { ESLint } from 'eslint';
import * as prettier from 'prettier';

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
