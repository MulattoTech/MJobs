'use strict';
// Run real PS5.1 and PS7 offline fixtures on Windows. Never launch Codex or a TUI.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { spawnSync } = require('node:child_process');

function windowsShells() {
  const windows = process.env.SystemRoot || 'C:\\Windows';
  const ps5Candidates = [
    path.join(windows, 'Sysnative', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
    path.join(windows, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe'),
  ];
  const ps5 = ps5Candidates.find(file => fs.existsSync(file));
  const programs = process.env.ProgramW6432 || process.env.ProgramFiles || 'C:\\Program Files';
  const ps7 = path.join(programs, 'PowerShell', '7', 'pwsh.exe');
  assert(ps5, 'Windows PowerShell 5.1 executable is required; native validation is not optional on Windows.');
  assert(fs.existsSync(ps7), 'PowerShell 7 executable is required; native validation is not optional on Windows.');
  return [{ name: 'Windows PowerShell 5.1', executable: ps5, major: 5 },
    { name: 'PowerShell 7', executable: ps7, major: 7 }];
}

function runFixture(shell, file, suite) {
  const child = spawnSync(shell.executable,
    ['-NoLogo', '-NoProfile', '-NonInteractive', '-File', path.join(__dirname, file)],
    { cwd: __dirname, encoding: 'utf8', windowsHide: true, timeout: 45000, maxBuffer: 1024 * 1024 });
  if (child.error) throw new Error(`${shell.name} / ${file}: ${child.error.message}`);
  assert.equal(child.signal, null, `${shell.name} / ${file} was interrupted: ${child.signal}`);
  assert.equal(child.status, 0,
    `${shell.name} / ${file} failed:\n${String(child.stderr || child.stdout).slice(0, 6000)}`);
  const result = JSON.parse(child.stdout.replace(/^\uFEFF/, '').trim());
  assert.equal(result.SchemaVersion, 1);
  assert.equal(result.Suite, suite);
  assert.equal(result.Status, 'PASS');
  const version = String(result.PowerShellVersion).split('.').map(Number);
  assert.equal(version[0], shell.major, 'Fixture ran under the wrong PowerShell edition.');
  if (shell.major === 5) assert.equal(version[1], 1, 'PS5 fixture requires PowerShell 5.1.');
  assert.equal(result.ModelRequests, 0);
  assert.equal(result.NativeProcessesStarted, 0);
  assert.equal(result.RealProfilesChanged, 0);
  assert(Number.isInteger(result.Passed) && result.Passed > 0);
  if (suite === 'codex-shell-plan') {
    assert.equal(result.Passed, 78);
    assert.equal(result.RemoteFilesChanged, 0);
  } else {
    assert.equal(result.FirstChanges, 2);
    assert.equal(result.SecondChanges, 0);
    assert.equal(result.ExactBackups, 2);
    assert.equal(result.FixtureRemoved, true);
  }
  return { shell: shell.name, ...result };
}

function main() {
  if (process.platform !== 'win32') {
    console.log(JSON.stringify({ status: 'SKIP', suite: 'codex-shell-native',
      reason: 'Windows PowerShell 5.1 and Windows PowerShell 7 fixtures require Windows.',
      platform: process.platform, nativeFixtureRuns: 0, modelRequests: 0,
      realProfilesChanged: 0 }));
    return;
  }
  const results = [];
  for (const shell of windowsShells()) {
    results.push(runFixture(shell, 'Test-CodexShell.ps1', 'codex-shell-plan'));
    results.push(runFixture(shell, 'Test-CodexShell-Installer.ps1', 'codex-shell-installer'));
  }
  console.log(JSON.stringify({ status: 'PASS', suite: 'codex-shell-native',
    nativeFixtureRuns: results.length,
    assertions: results.reduce((sum, result) => sum + result.Passed, 0),
    modelRequests: 0, codexProcessesStarted: 0, realProfilesChanged: 0, results }));
}

try { main(); }
catch (error) { console.error(`FAIL codex-shell-native: ${error.message}`); process.exitCode = 1; }
