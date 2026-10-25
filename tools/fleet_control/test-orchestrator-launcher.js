'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');
const { OrchestratorLauncher, writeAtomic, TASK_NAME } = require('./orchestrator-launcher');

let count = 0;
function ready(request, overrides = {}) {
  const process = { pid: 8002, isElevated: false, integrityRid: 8192, sessionId: 1 };
  return { version: 1, nonce: request.nonce, mode: request.mode, status: 'ok',
    completedAt: new Date().toISOString(), safeToRetry: false,
    pid: 8001, isElevated: false, integrityRid: 8192, sessionId: 1,
    daemon: { ...process, present: true, verified: true, healthy: true },
    ui: { ...process, pid: 8003, verified: true, running: true }, error: null, ...overrides };
}
async function test(name, body) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-standard-launch-test-'));
  try { await body(root); count++; console.log('PASS ' + name); }
  finally { fs.rmSync(root, { recursive: true, force: true }); }
}
function successfulAdapter(counter) {
  return async ({ root, request, taskName }) => {
    assert.equal(taskName, TASK_NAME); if (counter) counter.calls++;
    writeAtomic(path.join(root, 'orchestrator-launch-result.json'), ready(request));
    return { started: true };
  };
}
// Leave enough scheduling/I/O headroom for loaded Windows hosts. Production
// uses 25 seconds; these bounded fixture failures still complete in 500 ms.
const options = root => ({ root, timeoutMs: 500, pollMs: 2 });

async function main() {
  await test('daemon result requires actual standard-token readiness', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: successfulAdapter() });
    const result = await launcher.launch('daemon');
    assert.equal(result.ok, true); assert.equal(result.daemon.healthy, true);
    assert.equal(launcher.status().pending, false);
  });
  await test('UI mode accepts healthy existing daemon and verified UI result', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: successfulAdapter() });
    const result = await launcher.launch('ui'); assert.equal(result.ui.running, true);
  });
  await test('concurrent identical requests share one task invocation', async root => {
    const counter = { calls: 0 };
    const first = new OrchestratorLauncher({ ...options(root), runTask: successfulAdapter(counter) });
    const second = new OrchestratorLauncher({ ...options(root), runTask: successfulAdapter(counter) });
    const a = first.launch('daemon'), b = second.launch('daemon');
    assert.strictEqual(a, b); await Promise.all([a, b]); assert.equal(counter.calls, 1);
  });
  await test('different concurrent mode is busy, not a second task', async root => {
    let release; const gate = new Promise(resolve => { release = resolve; });
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), timeoutMs: 500,
      runTask: async ({ request }) => { calls++; await gate; writeAtomic(launcher.resultPath, ready(request)); return { started: true }; } });
    const first = launcher.launch('daemon');
    await assert.rejects(launcher.launch('ui'), { code: 'LAUNCH_BUSY' });
    release(); await first; assert.equal(calls, 1);
  });
  await test('nonce mismatch times out and blocks a later blind retry', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      calls++; writeAtomic(launcher.resultPath, ready(request, { nonce: crypto.randomUUID() })); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(calls, 1); assert.equal(launcher.status().requiresReconciliation, true);
  });
  await test('matching late result reconciles without task retry', async root => {
    let saved, calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => { saved = request; calls++; return { started: true }; } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    writeAtomic(launcher.resultPath, ready(saved));
    assert.equal(launcher.reconcile().pending, false); assert.equal(calls, 1);
  });
  await test('an elevated task acknowledgement cannot be success', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      writeAtomic(launcher.resultPath, ready(request, { isElevated: true, integrityRid: 12288 })); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(launcher.status().pending, true);
  });
  await test('elevated backend and mismatched session cannot be success', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      const result = ready(request); result.daemon.isElevated = true; result.daemon.sessionId = 0;
      writeAtomic(launcher.resultPath, result); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
  });
  await test('healthy-looking but unverified backend cannot be success', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      const result = ready(request); result.daemon.verified = false;
      writeAtomic(launcher.resultPath, result); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(launcher.reconcile().pending, true);
  });
  await test('running but unverified UI cannot be success', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      const result = ready(request); delete result.ui.verified;
      writeAtomic(launcher.resultPath, result); return { started: true };
    } });
    await assert.rejects(launcher.launch('ui'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(launcher.reconcile().pending, true);
  });
  await test('known pre-trigger refusal permits later explicit request', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; return { started: false, code: 'TASK_NOT_LIMITED' }; } });
    await assert.rejects(launcher.launch('daemon'), { code: 'TASK_NOT_STARTED' });
    assert.equal(launcher.status().pending, false); assert.equal(calls, 1);
  });
  await test('ambiguous task trigger may still receive a verified result', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      writeAtomic(launcher.resultPath, ready(request)); throw Error('Transport acknowledgement lost');
    } });
    assert.equal((await launcher.launch('daemon')).ok, true);
  });
  await test('never-resolving trigger is bounded and remains pending', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: () => new Promise(() => {}) });
    const started = Date.now();
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert(Date.now() - started < 3000); assert.equal(launcher.status().pending, true);
  });
  await test('accepted acknowledgement is not mistaken for readiness', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      writeAtomic(launcher.resultPath, ready(request, { status: 'accepted' })); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), error => {
      assert.equal(error.code, 'LAUNCH_UNCERTAIN');
      assert.equal(error.details.requiresReconciliation, true);
      assert.equal(error.details.state, 'uncertain');
      return true;
    });
  });
  await test('post-start error retains uncertain durable lock', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      writeAtomic(launcher.resultPath, ready(request, { status: 'error', safeToRetry: false, error: 'STARTED_BUT_READINESS_UNVERIFIED' })); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(launcher.status().pending, true);
  });
  await test('pre-start verification error is a safe explicit refusal', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      writeAtomic(launcher.resultPath, ready(request, { status: 'error', safeToRetry: true, error: 'DAEMON_PORT_OWNED_BY_OTHER_PROCESS' })); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_REJECTED' });
    assert.equal(launcher.status().pending, false);
  });
  await test('malformed durable lock fails closed after server restart', async root => {
    fs.writeFileSync(path.join(root, 'orchestrator-launch-pending.json'), 'broken');
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; return { started: true }; } });
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(calls, 0); assert.equal(launcher.status().state, 'uncertain');
  });
  await test('invalid mode does not create request or call adapter', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('daemon --resume'), { code: 'INVALID_MODE' });
    assert.equal(calls, 0); assert.equal(fs.existsSync(launcher.requestPath), false);
  });
  await test('pre-aborted signal creates no request and never triggers task', async root => {
    const controller = new AbortController(); controller.abort(); let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('ui', { signal: controller.signal }), { code: 'LAUNCH_ABORTED' });
    assert.equal(calls, 0); assert.equal(fs.existsSync(launcher.requestPath), false);
  });
  await test('beforeTrigger completes and signal propagates before task invocation', async root => {
    const controller = new AbortController(); const events = [];
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request, signal }) => {
      assert.equal(signal, controller.signal); events.push('task');
      writeAtomic(launcher.resultPath, ready(request)); return { started: true };
    } });
    await launcher.launch('daemon', { signal: controller.signal, beforeTrigger: async ({ request, signal }) => {
      assert.equal(request.mode, 'daemon'); assert.equal(signal, controller.signal);
      events.push('check'); await Promise.resolve(); events.push('checked');
    } });
    assert.deepEqual(events, ['check', 'checked', 'task']);
  });
  await test('beforeTrigger refusal writes safe rejection and clears pending lock', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('ui', { beforeTrigger: () => { throw Error('fixture refusal'); } }), { code: 'LAUNCH_REJECTED' });
    assert.equal(calls, 0); assert.equal(launcher.status().pending, false);
    assert.equal(launcher.status().result.safeToRetry, true);
    assert.equal(launcher.status().result.status, 'rejected');
  });
  await test('false safety callback is a refusal, not permission', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('ui', { beforeTrigger: () => false }), { code: 'LAUNCH_REJECTED' });
    assert.equal(calls, 0); assert.equal(launcher.status().pending, false);
  });
  await test('abort during pre-trigger check prevents trigger and records matching cancellation', async root => {
    let calls = 0; const controller = new AbortController();
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('ui', { signal: controller.signal, beforeTrigger: async () => {
      await Promise.resolve(); controller.abort();
    } }), { code: 'LAUNCH_ABORTED' });
    const cancellation = JSON.parse(fs.readFileSync(launcher.cancelPath, 'utf8'));
    assert.equal(cancellation.version, 1); assert.equal(cancellation.mode, 'ui');
    assert.equal(cancellation.nonce, launcher.status().request.nonce);
    assert.equal(calls, 0); assert.equal(launcher.status().pending, false);
    assert.equal(launcher.status().result.safeToRetry, true);
  });
  await test('never-resolving pre-trigger check is cancellable without starting task', async root => {
    let calls = 0; const controller = new AbortController();
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('daemon', { signal: controller.signal, beforeTrigger: () => {
      queueMicrotask(() => controller.abort()); return new Promise(() => {});
    } }), { code: 'LAUNCH_ABORTED' });
    assert.equal(calls, 0); assert.equal(launcher.status().pending, false);
  });
  await test('pre-trigger timeout cannot later launch and is safe to retry explicitly', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    await assert.rejects(launcher.launch('daemon', { beforeTrigger: () => new Promise(() => {}) }), { code: 'LAUNCH_REJECTED' });
    assert.equal(calls, 0); assert.equal(launcher.status().pending, false);
    assert.equal(launcher.status().result.safeToRetry, true);
  });
  await test('abort during ambiguous trigger preserves cancellation and uncertain lock', async root => {
    let calls = 0; const controller = new AbortController();
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: () => {
      calls++; queueMicrotask(() => controller.abort()); return new Promise(() => {});
    } });
    await assert.rejects(launcher.launch('daemon', { signal: controller.signal }), error => {
      assert.equal(error.code, 'LAUNCH_UNCERTAIN'); assert.equal(error.details.requiresReconciliation, true);
      assert.equal(error.details.cancelRequested, true); return true;
    });
    assert.equal(launcher.status().pending, true);
    assert.equal(JSON.parse(fs.readFileSync(launcher.cancelPath, 'utf8')).nonce, launcher.status().request.nonce);
    await assert.rejects(launcher.launch('daemon'), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(calls, 1);
  });
  await test('abort while polling readiness preserves pending request', async root => {
    const controller = new AbortController();
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      writeAtomic(launcher.resultPath, ready(request, { status: 'accepted' }));
      setTimeout(() => controller.abort(), 5); return { started: true };
    } });
    await assert.rejects(launcher.launch('daemon', { signal: controller.signal }), { code: 'LAUNCH_UNCERTAIN' });
    assert.equal(launcher.status().pending, true); assert.equal(launcher.status().requiresReconciliation, true);
  });
  await test('late success cannot silently clear a cancelled request', async root => {
    const controller = new AbortController(); let saved;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      saved = request; controller.abort(); return { started: true };
    } });
    await assert.rejects(launcher.launch('ui', { signal: controller.signal }), { code: 'LAUNCH_UNCERTAIN' });
    writeAtomic(launcher.resultPath, ready(saved));
    assert.equal(launcher.reconcile().pending, true);
    assert.equal(launcher.status().state, 'uncertain');
    writeAtomic(launcher.resultPath, ready(saved, { status: 'error', safeToRetry: true, error: 'LAUNCH_CANCELLED' }));
    assert.equal(launcher.reconcile().pending, false);
  });
  await test('old cancellation nonce cannot cancel a fresh request', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: successfulAdapter() });
    writeAtomic(launcher.cancelPath, { version: 1, nonce: crypto.randomUUID(), mode: 'daemon' });
    assert.equal((await launcher.launch('daemon')).ok, true);
    assert.equal(launcher.status().cancelRequested, false);
  });
  await test('cancellation persistence failure is explicit and never grants retry', async root => {
    const controller = new AbortController(); let saved;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async ({ request }) => {
      saved = request;
      controller.abort(); return { started: true };
    } });
    fs.mkdirSync(launcher.cancelPath);
    await assert.rejects(launcher.launch('daemon', { signal: controller.signal }), error => {
      assert.equal(error.code, 'LAUNCH_UNCERTAIN');
      assert.equal(error.details.cancellationError, 'CANCELLATION_WRITE_FAILED');
      assert.equal(error.details.requiresReconciliation, true); return true;
    });
    assert.equal(launcher.status().pending, true);
    writeAtomic(launcher.resultPath, ready(saved));
    const restarted = new OrchestratorLauncher({ ...options(root), runTask: successfulAdapter() });
    assert.equal(restarted.reconcile().pending, true);
    assert.equal(restarted.status().cancelRequested, true);
  });
  await test('different safety context cannot coalesce and bypass its callback', async root => {
    let release; const gate = new Promise(resolve => { release = resolve; });
    const launcher = new OrchestratorLauncher({ ...options(root), timeoutMs: 500,
      runTask: async ({ request }) => { await gate; writeAtomic(launcher.resultPath, ready(request)); return { started: true }; } });
    const first = launcher.launch('daemon');
    await assert.rejects(launcher.launch('daemon', { beforeTrigger: () => false }), { code: 'LAUNCH_BUSY' });
    release(); await first;
  });
  await test('explicit recheck retires completed cancellation without launching or rewriting evidence', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    const request = { version: 1, nonce: crypto.randomUUID(), mode: 'daemon', requestedAt: new Date().toISOString() };
    writeAtomic(launcher.requestPath, request);
    writeAtomic(launcher.lockPath, { ...request, cancelRequested: true });
    writeAtomic(launcher.cancelPath, { version: 1, nonce: request.nonce, mode: request.mode });
    writeAtomic(launcher.resultPath, ready(request));
    const files = [launcher.requestPath, launcher.cancelPath, launcher.resultPath];
    const snapshots = files.map(file => fs.readFileSync(file, 'utf8'));
    assert.equal(launcher.reconcile().pending, true);
    assert.equal(launcher.reconcile({ acceptCompletedCancellation: false }).pending, true);
    const state = launcher.reconcile({ acceptCompletedCancellation: true });
    assert.equal(state.pending, false); assert.equal(state.state, 'ok');
    assert.equal(state.requiresReconciliation, false); assert.equal(state.cancelRequested, true);
    assert.equal(calls, 0);
    assert.deepEqual(files.map(file => fs.readFileSync(file, 'utf8')), snapshots);
  });
  await test('explicit recheck keeps unknown, accepted, unsafe, and unverified outcomes locked', async root => {
    let calls = 0;
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => { calls++; } });
    const request = { version: 1, nonce: crypto.randomUUID(), mode: 'ui', requestedAt: new Date().toISOString() };
    writeAtomic(launcher.requestPath, request);
    writeAtomic(launcher.lockPath, { ...request, cancelRequested: true });
    assert.equal(launcher.reconcile({ acceptCompletedCancellation: true }).pending, true);
    for (const result of [
      ready(request, { status: 'accepted' }),
      ready(request, { status: 'error', safeToRetry: false }),
      ready(request, { status: 'busy', safeToRetry: true }),
      ready(request, { nonce: crypto.randomUUID() }),
      ready(request, { mode: 'daemon' }),
      ready(request, { ui: { pid: 8003, running: true, verified: false, isElevated: false, integrityRid: 8192, sessionId: 1 } }),
      ready(request, { isElevated: true, integrityRid: 12288 })
    ]) {
      writeAtomic(launcher.resultPath, result);
      const state = launcher.reconcile({ acceptCompletedCancellation: true });
      assert.equal(state.pending, true); assert.equal(state.requiresReconciliation, true);
      assert.equal(state.state, 'uncertain');
    }
    assert.equal(calls, 0);
  });
  await test('explicit recheck can retire a matched safe native pre-start refusal', async root => {
    const launcher = new OrchestratorLauncher({ ...options(root), runTask: async () => assert.fail('must not invoke task') });
    const request = { version: 1, nonce: crypto.randomUUID(), mode: 'daemon', requestedAt: new Date().toISOString() };
    writeAtomic(launcher.requestPath, request);
    writeAtomic(launcher.lockPath, { ...request, cancelRequested: true });
    writeAtomic(launcher.resultPath, ready(request, { status: 'error', safeToRetry: true, error: 'LAUNCH_CANCELLED' }));
    const state = launcher.reconcile({ acceptCompletedCancellation: true });
    assert.equal(state.pending, false); assert.equal(state.requiresReconciliation, false);
    assert.equal(state.state, 'error');
  });
  await test('recheck consent must be a literal boolean and never coerces a string', async root => {
    const launcher = new OrchestratorLauncher(options(root));
    const request = { version: 1, nonce: crypto.randomUUID(), mode: 'daemon', requestedAt: new Date().toISOString() };
    writeAtomic(launcher.requestPath, request);
    writeAtomic(launcher.lockPath, { ...request, cancelRequested: true });
    writeAtomic(launcher.resultPath, ready(request));
    assert.throws(() => launcher.reconcile({ acceptCompletedCancellation: 'true' }), TypeError);
    assert.equal(launcher.status().pending, true);
  });
  await test('recheck cannot retire a pending record bound to a different mode', async root => {
    const launcher = new OrchestratorLauncher(options(root));
    const request = { version: 1, nonce: crypto.randomUUID(), mode: 'daemon', requestedAt: new Date().toISOString() };
    writeAtomic(launcher.requestPath, request);
    writeAtomic(launcher.lockPath, { ...request, mode: 'ui', cancelRequested: true });
    writeAtomic(launcher.resultPath, ready(request));
    const state = launcher.reconcile({ acceptCompletedCancellation: true });
    assert.equal(state.pending, true); assert.equal(state.requiresReconciliation, true);
    assert.equal(state.state, 'uncertain');
  });
  console.log('PASS all ' + count + ' launcher tests. No scheduled task or AO process was invoked.');
  if (process.platform === 'win32') {
    const powershell = 'C:\\Program Files\\PowerShell\\7\\pwsh.exe';
    const output = execFileSync(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-File',
      path.join(__dirname, 'test-orchestrator-launcher.ps1')], { windowsHide: true, timeout: 15000, maxBuffer: 262144, encoding: 'utf8' });
    const native = JSON.parse(output.trim());
    assert.equal(native.ok, true); assert.equal(native.nativeStarts, 0); assert(native.checks >= 9);
    console.log('PASS ' + native.checks + ' native PowerShell fixture checks. No actual process starts.');
  } else {
    console.log('SKIP native PowerShell fixture checks: Windows required; not native-validated on this host.');
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
