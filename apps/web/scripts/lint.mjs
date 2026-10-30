import {readdirSync, readFileSync} from 'node:fs';
import {extname, join} from 'node:path';
import {fileURLToPath} from 'node:url';

const webRoot = fileURLToPath(new URL('../', import.meta.url));
const roots = [join(webRoot, 'src'), join(webRoot, 'vite.config.ts')];
const extensions = new Set(['.ts', '.tsx']);
const rules = [
  ['TypeScript suppression', /@ts-(?:ignore|nocheck)\b/],
  ['dynamic eval', /\beval\s*\(/],
  ['dynamic Function constructor', /\bnew\s+Function\s*\(/],
  ['legacy var declaration', /\bvar\s+[A-Za-z_$]/],
  ['merge conflict marker', /^(?:<{7}|={7}|>{7})/],
];

function files(path) {
  if (extname(path)) return extensions.has(extname(path)) ? [path] : [];
  return readdirSync(path, {withFileTypes: true}).flatMap(entry =>
    files(join(path, entry.name)));
}

const targets = roots.flatMap(files).sort();
if (!targets.length) throw new Error('Frontend lint found no TypeScript source files');
const findings = [];
for (const path of targets) {
  const relative = path.slice(webRoot.length).replaceAll('\\', '/');
  for (const [index, line] of readFileSync(path, 'utf8').split(/\r?\n/).entries()) {
    for (const [name, pattern] of rules) {
      if (pattern.test(line)) findings.push(`${relative}:${index + 1}: ${name}`);
    }
    if (/[ \t]+$/.test(line)) findings.push(`${relative}:${index + 1}: trailing whitespace`);
  }
}
if (findings.length) {
  console.error(findings.join('\n'));
  process.exitCode = 1;
} else {
  console.log(`Frontend lint: PASS (${targets.length} files)`);
}
