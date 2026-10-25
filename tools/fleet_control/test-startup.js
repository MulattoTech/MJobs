'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { StartupController, POLICY } = require('./startup-control');

function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
function fixture(t, overrides = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-startup-test-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const events = [];
  const live = {
    quota: { fresh: true, remainingPercent: 100, ordinaryUsageAllowed: true, armed: false, tripped: true, thresholdPercent: 7 },
    emergency: { latched: true, revision: 'stop-1' }, dispatchPaused: false,
  };
  const deps = {
    safety: async () => structuredClone(live),
    bootstrap: async () => { events.push('bootstrap'); return { ok: true, targetCount: 3, primaryIds: ['mjobs-primary'] }; },
    holdDispatch: async () => { events.push('hold'); live.dispatchPaused = true; },
    unlock: async ctx => { assert.equal(ctx.expectedEmergencyRevision, 'stop-1'); events.push('unlock'); live.emergency.latched = false; },
    watchdogDisarmed: async () => { events.push('watchdog-ack'); return { ready: true }; },
    arm: async ctx => { assert.deepEqual(ctx.policy, POLICY); events.push('arm-7'); live.quota.armed = true; live.quota.tripped = false; return { armed: true, token: 'guard-revision-1' }; },
    guardAck: async ctx => { assert.equal(ctx.guardReceipt.token, 'guard-revision-1'); events.push('guard-ack'); return { ready: true }; },
    restore: {
      start: async ctx => { assert.equal(ctx.autoDispatchAfter, false); events.push('restore-start'); },
      status: async () => { events.push('restore-status'); return { active: false, phase: 'complete', total: 3, restoredCount: 3 }; },
      pause: async () => { events.push('restore-pause'); },
    },
    readiness: async () => { events.push('current-readiness'); return { ready: true }; },
    openUi: async () => { events.push('ui'); },
  };
  const config = { root, deps, pollMs: 1, timeouts: { readMs: 200, actionMs: 200, watchdogMs: 200, guardAckMs: 200, restoreMs: 200, readinessMs: 200, totalMs: 2000 }, ...overrides };
  return { root, deps, config, events, live, make: () => new StartupController(config) };
}
const request = (id = 'request-0001', extra = {}) => ({ confirm: 'STARTUP', requestId: id, ...extra });

test('ordered startup composes existing restore, acknowledges guard, and finishes paused', async t => {
  const f = fixture(t), controller = f.make();
  const accepted = controller.start(request('sequence-01', { openUi: true }));
  assert.equal(accepted.statusCode, 202); assert.equal(accepted.run.active, true);
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'ready_paused'); assert.equal(result.active, false);
  assert.equal(result.guardAcknowledged, true); assert.equal(f.live.dispatchPaused, true);
  assert.deepEqual(f.events, ['bootstrap', 'hold', 'unlock', 'watchdog-ack', 'arm-7', 'guard-ack', 'restore-start', 'restore-status', 'current-readiness', 'ui']);
  assert.equal(result.options.autoDispatchAfter, false);
  const saved = JSON.parse(fs.readFileSync(path.join(f.root, 'startup-state.json'), 'utf8'));
  assert.equal(saved.phase, 'ready_paused'); assert.equal(saved.runId, accepted.run.runId);
});

test('exact confirmation and stable request identity are required', t => {
  const f = fixture(t), controller = f.make();
  assert.throws(() => controller.start({ confirm: 'start', requestId: 'request-0001' }), /STARTUP/);
  assert.throws(() => controller.start({ confirm: 'STARTUP' }), /request ID/);
  assert.deepEqual(f.events, []);
});

test('duplicate requests and concurrent clicks never start a second restore', async t => {
  const f = fixture(t), gate = deferred(), reached = deferred();
  f.deps.watchdogDisarmed = async () => { reached.resolve(); return gate.promise; };
  const controller = f.make(); const a = controller.start(request('dedupe-0001'));
  await reached.promise;
  const b = controller.start(request('dedupe-0001')), c = controller.start(request('dedupe-0002'));
  assert.equal(b.run.runId, a.run.runId); assert.equal(c.run.runId, a.run.runId);
  assert.equal(b.idempotent, true); assert.equal(c.idempotent, true);
  gate.resolve({ ready: true }); await controller.waitForIdle();
  const retry = controller.start(request('dedupe-0001'));
  assert.equal(retry.idempotent, true); assert.equal(retry.run.phase, 'ready_paused');
  assert.equal(f.events.filter(x => x === 'restore-start').length, 1);
});

test('bad quota or bootstrap validation cannot unlock or launch', async t => {
  for (const condition of ['stale', 'null', 'zero', 'brake', 'denied', 'manifest']) {
    await t.test(condition, async st => {
      const f = fixture(st);
      if (condition === 'stale') f.live.quota.fresh = false;
      if (condition === 'null') f.live.quota.remainingPercent = null;
      if (condition === 'zero') f.live.quota.remainingPercent = 0;
      if (condition === 'brake') f.live.quota.remainingPercent = 15;
      if (condition === 'denied') f.live.quota.ordinaryUsageAllowed = false;
      if (condition === 'manifest') f.deps.bootstrap = async () => ({ ok: false, reason: 'Multiple roots', targetCount: 3 });
      const controller = f.make(); controller.start(request('invalid-' + condition));
      const result = await controller.waitForIdle();
      assert.equal(result.phase, 'failed'); assert.equal(result.reconciliationRequired, false);
      assert.equal(f.live.emergency.latched, true); assert.deepEqual(f.events, []);
    });
  }
});

test('new emergency before unlock cannot be cleared by the old startup request', async t => {
  const f = fixture(t);
  f.deps.bootstrap = async () => { f.live.emergency.revision = 'stop-2'; return { ok: true, targetCount: 3 }; };
  const controller = f.make(); controller.start(request('revision-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.error.code, 'EMERGENCY_CHANGED');
  assert.equal(f.events.includes('unlock'), false); assert.equal(f.live.emergency.latched, true);
});

test('a reconcilable bootstrap blocker durably preserves exact target identities without taking startup actions', async t => {
  const f = fixture(t), manifest = { targetCount: 3, primaryIds: ['mjobs-primary'], targetIds: ['mjobs-primary', 'mjobs-worker-1', 'mjobs-worker-2'], manifestRevision: 'preserved-manifest-1' };
  f.deps.bootstrap = async () => ({ ok: false, ...manifest, reconciliationRequired: true, reason: 'A prior manual restore is unverified' });
  const controller = f.make(); controller.start(request('blocked-native-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'uncertain'); assert.equal(result.reconciliationRequired, true); assert.equal(result.error.code, 'BOOTSTRAP_BLOCKED');
  assert.deepEqual(result.bootstrap, { ...manifest, alreadyOnline: false });
  const saved = JSON.parse(fs.readFileSync(path.join(f.root, 'startup-state.json'), 'utf8'));
  assert.deepEqual(saved.bootstrap, result.bootstrap); assert.equal(saved.reconciliationRequired, true);
  assert.deepEqual(f.events, []); assert.equal(f.live.emergency.latched, true);
  assert.throws(() => controller.start(request('blocked-native-02')), /Reconcile/);
});

test('emergency after guard acknowledgment prevents restoration and never re-unlocks', async t => {
  const f = fixture(t);
  f.deps.guardAck = async () => { f.live.emergency = { latched: true, revision: 'stop-2' }; return { ready: true }; };
  const controller = f.make(); controller.start(request('emergency-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.error.code, 'EMERGENCY_ACTIVE');
  assert.equal(f.events.includes('restore-start'), false);
  assert.equal(f.events.filter(x => x === 'unlock').length, 1);
  assert.equal(f.live.emergency.latched, true); assert.equal(f.live.dispatchPaused, true);
});

test('quota becoming stale between stages prevents arming and launch', async t => {
  const f = fixture(t);
  f.deps.watchdogDisarmed = async () => { f.live.quota.fresh = false; return { ready: true }; };
  const controller = f.make(); controller.start(request('stale-later-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.error.code, 'QUOTA_STALE');
  assert.equal(f.events.includes('arm-7'), false); assert.equal(f.events.includes('restore-start'), false);
});

test('watchdog and guard acknowledgment waits are bounded and do not launch on timeout', async t => {
  for (const target of ['watchdog', 'guard']) await t.test(target, async st => {
    const f = fixture(st);
    f.config.timeouts.watchdogMs = 12; f.config.timeouts.guardAckMs = 12;
    f.deps[target === 'watchdog' ? 'watchdogDisarmed' : 'guardAck'] = async () => ({ ready: false });
    const controller = f.make(); controller.start(request('timeout-' + target));
    const result = await controller.waitForIdle();
    assert.equal(result.phase, 'failed'); assert.equal(result.error.code, 'STAGE_TIMEOUT');
    assert.equal(f.events.includes('restore-start'), false);
  });
});

test('a timed-out mutation stays uncertain even if it later resolves', async t => {
  const f = fixture(t), gate = deferred();
  f.config.timeouts.actionMs = 12;
  f.deps.arm = async () => gate.promise;
  const controller = f.make(); const first = controller.start(request('uncertain-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'uncertain'); assert.equal(result.reconciliationRequired, true);
  assert.equal(result.actionPending.name, 'arm 7% guard');
  assert.throws(() => controller.start(request('uncertain-02')), /Reconcile/);
  assert.equal(controller.start(request('uncertain-01')).run.runId, first.run.runId);
  gate.resolve({ armed: true, token: 'late' }); await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.events.includes('restore-start'), false); assert.equal(controller.status().phase, 'uncertain');
});

test('restart marks an active durable run uncertain without resuming any operation', async t => {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.root, 'startup-state.json'), JSON.stringify({ schemaVersion: 1, active: true, runId: 'old-run', requestId: 'old-request', ownerId: 'gone', phase: 'restoring', recentRequests: [{ requestId: 'old-request', runId: 'old-run' }], restoreStartAttempted: true }));
  const controller = f.make();
  assert.equal(controller.status().phase, 'uncertain');
  assert.equal(controller.status().error.code, 'LOST_OWNER');
  assert.throws(() => controller.start(request('restart-0001')), /Reconcile/);
  assert.equal(controller.start(request('old-request')).idempotent, true);
  assert.deepEqual(f.events, []);
});

test('corrupt durable state is preserved and blocks startup', t => {
  const f = fixture(t), file = path.join(f.root, 'startup-state.json');
  fs.writeFileSync(file, '{broken');
  const controller = f.make();
  assert.equal(controller.status().error.code, 'STATE_UNREADABLE');
  assert.throws(() => controller.start(request('corrupt-0001')), /Reconcile/);
  assert.equal(fs.readFileSync(file, 'utf8'), '{broken'); assert.deepEqual(f.events, []);
});

test('cancel before restoration stops the sequence and is idempotent', async t => {
  const f = fixture(t), entered = deferred();
  f.deps.watchdogDisarmed = async () => { entered.resolve(); return new Promise(() => {}); };
  const controller = f.make(); const accepted = controller.start(request('cancel-0001'));
  await entered.promise;
  const cancelled = controller.cancel({ runId: accepted.run.runId });
  assert.equal(cancelled.statusCode, 202);
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'cancelled'); assert.equal(f.events.includes('arm-7'), false);
  assert.equal(f.events.includes('restore-start'), false); assert.equal(f.live.dispatchPaused, true);
  assert.equal(controller.cancel({ runId: accepted.run.runId }).idempotent, true);
});

test('cancel while restore.start is unverified pauses that restore and blocks retries', async t => {
  const f = fixture(t), entered = deferred();
  f.deps.restore.start = async () => { f.events.push('restore-start'); entered.resolve(); return new Promise(() => {}); };
  const controller = f.make(); const accepted = controller.start(request('cancel-restore-01'));
  await entered.promise; controller.cancel({ runId: accepted.run.runId });
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'uncertain'); assert.equal(result.reconciliationRequired, true);
  assert.equal(f.events.filter(x => x === 'restore-start').length, 1);
  assert.equal(f.events.includes('restore-pause'), true);
  assert.throws(() => controller.start(request('cancel-restore-02')), /Reconcile/);
});

test('historical restore completion cannot bypass current controller-readiness validation', async t => {
  const f = fixture(t);
  f.deps.readiness = async () => ({ failed: true, reason: 'Primary controller stopped' });
  const controller = f.make(); controller.start(request('readiness-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'uncertain'); assert.match(result.error.message, /Primary controller stopped/);
  assert.equal(f.events.includes('restore-pause'), true); assert.equal(f.events.includes('ui'), false);
});

test('already-online fleet is verified but not restored twice; optional UI is separate', async t => {
  const f = fixture(t);
  f.deps.bootstrap = async () => ({ ok: true, targetCount: 3, alreadyOnline: true });
  f.live.emergency.latched = false;
  const controller = f.make(); controller.start(request('already-online-01', { openUi: true }));
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'ready_paused'); assert.equal(f.events.includes('unlock'), false);
  assert.equal(f.events.includes('restore-start'), false); assert.equal(f.events.includes('restore-status'), false);
  assert.equal(f.events.filter(x => x === 'ui').length, 1); assert.equal(f.events.includes('current-readiness'), true);
});

test('owner loss during an awaited probe prevents the old controller from proceeding', async t => {
  const f = fixture(t), entered = deferred(), gate = deferred();
  f.deps.watchdogDisarmed = async () => { entered.resolve(); return gate.promise; };
  const original = f.make(); original.start(request('owner-loss-01')); await entered.promise;
  const restarted = f.make(); assert.equal(restarted.status().phase, 'uncertain');
  gate.resolve({ ready: true }); await original.waitForIdle();
  assert.equal(original.status().phase, 'uncertain');
  assert.equal(f.events.includes('arm-7'), false); assert.equal(f.events.includes('restore-start'), false);
  assert.throws(() => restarted.start(request('owner-loss-02')), /Reconcile/);
});

test('wrong guard threshold or changed polling settings prevent restore', async t => {
  for (const change of ['threshold', 'polling', 'disabled']) await t.test(change, async st => {
    const f = fixture(st);
    const originalArm = f.deps.arm;
    f.deps.arm = async ctx => {
      const receipt = await originalArm(ctx);
      if (change === 'threshold') f.live.quota.thresholdPercent = 0;
      if (change === 'polling') f.live.quota.pollSeconds = 60;
      if (change === 'disabled') f.live.quota.monitorEnabled = false;
      return receipt;
    };
    const controller = f.make(); controller.start(request('guard-config-' + change));
    const result = await controller.waitForIdle();
    assert.equal(result.phase, 'failed'); assert.match(result.error.code, /^GUARD_/);
    assert.equal(f.events.includes('restore-start'), false);
  });
});

test('unconfirmed dispatch pause blocks all bootstrap mutations after hold', async t => {
  const f = fixture(t); f.deps.holdDispatch = async () => { f.events.push('hold-without-ack'); };
  const controller = f.make(); controller.start(request('dispatch-ack-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.error.code, 'DISPATCH_NOT_HELD'); assert.equal(f.events.includes('unlock'), false);
  assert.equal(f.events.includes('restore-start'), false);
});

function uncertainFixture(t) {
  const f = fixture(t);
  fs.writeFileSync(path.join(f.root, 'startup-state.json'), JSON.stringify({
    schemaVersion: 1, revision: 'prior-revision-1', runId: 'prior-run-01', requestId: 'prior-request-01', ownerId: 'old-owner',
    active: false, phase: 'uncertain', reconciliationRequired: true, recentRequests: [{ runId: 'prior-run-01', requestId: 'prior-request-01' }],
    bootstrap: { targetCount: 3, primaryIds: ['mjobs-primary'] }, actionPending: { name: 'restore start' },
    restoreStartAttempted: true, guardReceipt: { token: 'guard-revision-1' }, error: { code: 'LOST_OWNER', message: 'Prior owner exited' },
  }));
  return f;
}
function stoppedProof(ctx) {
  return { runId: ctx.runId, stateRevision: ctx.expectedStateRevision, observedAt: new Date().toISOString(), outcome: 'idle',
    noOperationInFlight: true, dispatchPaused: true, restoreActive: false, launchActive: false,
    submissionAuditComplete: true, submittedResume: false, runningExpectedAgents: 0 };
}
function readyProof(ctx) {
  return { ...stoppedProof(ctx), outcome: 'ready_paused', emergencyLatched: false,
    expectedTargetsMatch: true, allExpectedControllersHealthy: true, allExpectedRuntimesHealthy: true,
    expectedTargetCount: 3, verifiedTargetCount: 3,
    guard: { fresh: true, armed: true, tripped: false, remainingPercent: 100, ordinaryUsageAllowed: true,
      thresholdPercent: 7, pollSeconds: 5, window: 'any', revision: 'current-guard', acknowledgedRevision: 'current-guard' } };
}
const recheck = { confirm: 'RECHECK STARTUP', runId: 'prior-run-01' };

test('reconcile requires exact confirmation and target run', async t => {
  const f = uncertainFixture(t); f.deps.reconcile = async ctx => stoppedProof(ctx);
  const controller = f.make();
  await assert.rejects(controller.reconcile({ ...recheck, confirm: 'yes' }), /RECHECK STARTUP/);
  await assert.rejects(controller.reconcile({ ...recheck, runId: 'different' }), /current Startup run/);
  assert.equal(controller.status().phase, 'uncertain'); assert.deepEqual(f.events, []);
});

test('healthy reconciliation adopts verified current runtimes without launching anything', async t => {
  const f = uncertainFixture(t); f.deps.reconcile = async ctx => readyProof(ctx);
  const controller = f.make(), result = await controller.reconcile(recheck);
  assert.equal(result.reconciled, true); assert.equal(result.run.phase, 'ready_paused');
  assert.equal(result.run.ownerId, controller.ownerId); assert.equal(result.run.reconciliationRequired, false);
  assert.equal(result.run.guardReceipt.token, 'current-guard'); assert.equal(result.run.guardAcknowledged, true);
  assert.equal(result.run.reconciliation.previousError.code, 'LOST_OWNER');
  assert.equal(result.run.reconciliation.previousAction.name, 'restore start');
  assert.equal(result.run.restore.restoredCount, 3); assert.deepEqual(f.events, []);
});

test('positively never-submitted stopped work can return to idle for a new explicit request', async t => {
  const f = uncertainFixture(t); f.deps.reconcile = async ctx => stoppedProof(ctx);
  const controller = f.make(), result = await controller.reconcile(recheck);
  assert.equal(result.run.phase, 'idle'); assert.equal(result.run.reconciliationRequired, false);
  assert.equal(result.run.actionPending, null); assert.deepEqual(f.events, []);
  assert.equal(controller.start(request('prior-request-01')).idempotent, true);
  controller.start(request('new-after-recheck-01')); await controller.waitForIdle();
  assert.equal(controller.status().phase, 'ready_paused'); assert.equal(f.events.filter(x => x === 'restore-start').length, 1);
});

test('incomplete, stale or mismatched ready/stopped proof never clears uncertainty', async t => {
  const cases = {
    'unknown submitted resume': proof => { proof.submittedResume = null; },
    'submitted resume': proof => { proof.submittedResume = true; },
    'incomplete audit': proof => { proof.submissionAuditComplete = false; },
    'running agent': proof => { proof.runningExpectedAgents = 1; },
    'in-flight action': proof => { proof.noOperationInFlight = false; },
    'active restore': proof => { proof.restoreActive = true; },
    'dispatch unpaused': proof => { proof.dispatchPaused = false; },
    'stale proof': proof => { proof.observedAt = new Date(Date.now() - 60000).toISOString(); },
    'wrong revision': proof => { proof.stateRevision = 'other'; },
    'wrong run': proof => { proof.runId = 'other'; },
    'unknown outcome': proof => { proof.outcome = 'unknown'; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = uncertainFixture(st); f.deps.reconcile = async ctx => { const proof = stoppedProof(ctx); alter(proof); return proof; };
    const controller = f.make(), result = await controller.reconcile(recheck);
    assert.equal(result.reconciled, false); assert.equal(result.pending, true); assert.equal(result.run.phase, 'uncertain');
    assert.equal(result.run.revision, 'prior-revision-1'); assert.deepEqual(f.events, []);
  });
  for (const [name, alter] of Object.entries({
    'guard not acknowledged': proof => { proof.guard.acknowledgedRevision = 'old'; },
    'stale guard': proof => { proof.guard.fresh = false; },
    'string percentage': proof => { proof.guard.remainingPercent = '100'; },
    'low quota': proof => { proof.guard.remainingPercent = 7; },
    'emergency engaged': proof => { proof.emergencyLatched = true; },
    'controller stopped': proof => { proof.allExpectedControllersHealthy = false; },
    'missing runtime': proof => { proof.verifiedTargetCount = 2; },
    'wrong manifest count': proof => { proof.expectedTargetCount = 2; proof.verifiedTargetCount = 2; },
    'identities unverified': proof => { proof.expectedTargetsMatch = false; },
  })) await t.test(name, async st => {
    const f = uncertainFixture(st); f.deps.reconcile = async ctx => { const proof = readyProof(ctx); alter(proof); return proof; };
    const controller = f.make(), result = await controller.reconcile(recheck);
    assert.equal(result.reconciled, false); assert.equal(result.run.phase, 'uncertain'); assert.deepEqual(f.events, []);
  });
});

test('a still-pending timed-out mutation prevents even invoking the reconciliation inspector', async t => {
  const f = fixture(t), gate = deferred(); f.config.timeouts.actionMs = 10;
  f.deps.arm = async () => gate.promise;
  let inspected = 0;
  f.deps.reconcile = async ctx => { inspected++; return stoppedProof(ctx); };
  const controller = f.make(); const started = controller.start(request('late-action-01'));
  await controller.waitForIdle(); assert.equal(controller.status().phase, 'uncertain');
  const waiting = await controller.reconcile({ confirm: 'RECHECK STARTUP', runId: started.run.runId });
  assert.equal(waiting.pending, true); assert.equal(inspected, 0);
  gate.resolve({ armed: true, token: 'late' }); await new Promise(resolve => setImmediate(resolve));
  const result = await controller.reconcile({ confirm: 'RECHECK STARTUP', runId: started.run.runId });
  assert.equal(inspected, 1); assert.equal(result.run.phase, 'idle'); assert.equal(f.events.includes('restore-start'), false);
});

test('changed durable revision during recheck prevents stale proof from committing', async t => {
  const f = uncertainFixture(t), gate = deferred(), entered = deferred();
  f.deps.reconcile = async ctx => { entered.resolve(); await gate.promise; return stoppedProof(ctx); };
  const controller = f.make(); const checking = controller.reconcile(recheck); await entered.promise;
  const file = path.join(f.root, 'startup-state.json'); const newer = JSON.parse(fs.readFileSync(file, 'utf8'));
  newer.revision = 'new-emergency-revision'; newer.error = { code: 'NEWER_EVENT' }; fs.writeFileSync(file, JSON.stringify(newer));
  gate.resolve(); await assert.rejects(checking, /state changed/);
  assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).revision, 'new-emergency-revision'); assert.deepEqual(f.events, []);
});

test('stale in-memory run cannot reconcile over a different durable run', async t => {
  const f = uncertainFixture(t); let inspected = 0;
  f.deps.reconcile = async ctx => { inspected++; return stoppedProof(ctx); };
  const controller = f.make(); const file = path.join(f.root, 'startup-state.json');
  const newer = JSON.parse(fs.readFileSync(file, 'utf8')); newer.runId = 'new-run'; newer.revision = 'new-revision'; newer.active = true; newer.phase = 'validating'; fs.writeFileSync(file, JSON.stringify(newer));
  await assert.rejects(controller.reconcile(recheck), /different Startup run/);
  assert.equal(inspected, 0); assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).runId, 'new-run');
});

test('reconciliation timeout leaves state unchanged and late proof cannot commit', async t => {
  const f = uncertainFixture(t), gate = deferred(); f.config.timeouts.readMs = 10;
  f.deps.reconcile = async ctx => { await gate.promise; return stoppedProof(ctx); };
  const controller = f.make(); await assert.rejects(controller.reconcile(recheck), /timed out/);
  assert.equal(controller.status().phase, 'uncertain'); gate.resolve(); await new Promise(resolve => setImmediate(resolve));
  assert.equal(controller.status().phase, 'uncertain'); assert.equal(controller.status().revision, 'prior-revision-1');
});

test('concurrent read-only rechecks coalesce and never duplicate a state transition', async t => {
  const f = uncertainFixture(t), gate = deferred(), entered = deferred(); let calls = 0;
  f.deps.reconcile = async ctx => { calls++; entered.resolve(); await gate.promise; return stoppedProof(ctx); };
  const controller = f.make(); const first = controller.reconcile(recheck); await entered.promise;
  const second = controller.reconcile(recheck); gate.resolve(); const results = await Promise.all([first, second]);
  assert.equal(calls, 1); assert.equal(results[0].run.revision, results[1].run.revision); assert.deepEqual(f.events, []);
});

test('unfinished cleanup on restart is treated as uncertain even if phase says failed', t => {
  const f = uncertainFixture(t), file = path.join(f.root, 'startup-state.json');
  const prior = JSON.parse(fs.readFileSync(file, 'utf8')); prior.phase = 'failed'; prior.reconciliationRequired = false; prior.cleanup = { active: true }; fs.writeFileSync(file, JSON.stringify(prior));
  const controller = f.make(); assert.equal(controller.status().phase, 'uncertain'); assert.equal(controller.status().error.code, 'LOST_OWNER');
  assert.deepEqual(f.events, []);
});

test('a completed mutation that outruns its timer still fails closed on elapsed deadline', async t => {
  let clock = Date.now();
  const f = fixture(t, { now: () => clock });
  const originalArm = f.deps.arm; let receivedSignal;
  f.deps.arm = async ctx => {
    receivedSignal = ctx.signal;
    const receipt = await originalArm(ctx);
    clock += f.config.timeouts.actionMs + 1;
    return receipt;
  };
  const controller = f.make(); controller.start(request('elapsed-mutation-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'uncertain'); assert.equal(result.error.code, 'STAGE_TIMEOUT');
  assert.equal(receivedSignal.aborted, true); assert.equal(f.events.includes('restore-start'), false);
});

test('read-only adapter completion after its deadline cannot authorize mutations', async t => {
  let clock = Date.now();
  const f = fixture(t, { now: () => clock });
  f.deps.bootstrap = async () => { clock += f.config.timeouts.readMs + 1; return { ok: true, targetCount: 3 }; };
  const controller = f.make(); controller.start(request('elapsed-probe-01'));
  const result = await controller.waitForIdle();
  assert.equal(result.phase, 'failed'); assert.equal(result.error.code, 'STAGE_TIMEOUT');
  assert.deepEqual(f.events, []); assert.equal(f.live.emergency.latched, true);
});

test('an in-memory unresolved mutation cannot be bypassed by externally clearing durable uncertainty', async t => {
  const f = fixture(t), gate = deferred(); f.config.timeouts.actionMs = 10;
  let calls = 0; f.deps.arm = async () => { calls++; return gate.promise; };
  const controller = f.make(); controller.start(request('outstanding-first-01')); await controller.waitForIdle();
  const file = path.join(f.root, 'startup-state.json'); const modified = JSON.parse(fs.readFileSync(file, 'utf8'));
  modified.phase = 'idle'; modified.reconciliationRequired = false; modified.revision = 'external-clear'; fs.writeFileSync(file, JSON.stringify(modified));
  assert.throws(() => controller.start(request('outstanding-second-02')), /still in flight/);
  assert.equal(calls, 1); assert.equal(f.events.includes('restore-start'), false);
  gate.resolve({ armed: true, token: 'late' }); await new Promise(resolve => setImmediate(resolve));
});
