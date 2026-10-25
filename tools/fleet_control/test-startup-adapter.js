'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { createHash } = require('node:crypto');
const { StartupAdapter } = require('./startup-adapter');
const { StartupController } = require('./startup-control');
const { configRevision } = require('./quota-guard');
const { targetsFromSnapshot } = require('./restore-fleet');

const clone = value => structuredClone(value);
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
function fixture(t) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-startup-adapter-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const state = {
    now: Date.now(), effects: [], reads: [], operationBusy: false,
    saved: { sessions: [
      { id: 'mjobs-primary', projectId: 'mjobs', kind: 'orchestrator', harness: 'codex', activity: 'idle' },
      { id: 'mjobs-codex-1', projectId: 'mjobs', kind: 'worker', harness: 'codex', activity: 'idle' },
      { id: 'mjobs-agy-1', projectId: 'mjobs', kind: 'worker', harness: 'agy', activity: 'idle' },
    ] },
    sessions: [], counts: new Map(),
    resources: { ramGiB: 30, diskGiB: 150, cpuPercent: 10 },
    quota: { fresh: true, selectedRemainingPercent: 100, ordinaryUsageAllowed: true },
    config: { enabled: true, pollSeconds: 5, thresholdPercent: 7, window: 'any', autoKillArmed: false, tripped: false, updatedAt: '2026-10-25T00:00:00.000Z' },
    acknowledgedRevision: null,
    catalog: { providers: [{ id: 'codex', available: true }, { id: 'agy', available: true }] },
    launch: { pending: false, requiresReconciliation: false },
    emergency: { phase: 'holding_zero', remainingTotal: 0 },
  };
  const latchPath = path.join(root, 'emergency-latch.json');
  const writeLatch = (enabled, extra = {}) => fs.writeFileSync(latchPath, JSON.stringify({ enabled, ...extra }));
  writeLatch(true, { trigger: 'initial' });
  const revision = () => createHash('sha256').update(fs.readFileSync(latchPath)).digest('hex');
  const quota = {
    config: () => clone(state.config),
    status: () => ({ ...clone(state.quota), config: clone(state.config),
      guardAcknowledged: state.quota.fresh === true && state.acknowledgedRevision === configRevision(state.config),
      guardAck: state.acknowledgedRevision ? { revision: state.acknowledgedRevision } : null }),
    dispatchBlockReason: () => {
      const s = quota.status();
      if (!s.fresh) return 'Quota telemetry is stale';
      if (!s.config.enabled || !s.config.autoKillArmed || s.config.tripped) return 'Quota guard is unavailable';
      if (s.ordinaryUsageAllowed === false || s.selectedRemainingPercent <= 15) return 'Quota allowance is blocked';
      if (!s.guardAcknowledged) return 'Quota guard is awaiting acknowledgment';
      return null;
    },
    update: update => { state.effects.push({ op: 'quota.update', update: clone(update) }); Object.assign(state.config, update, { updatedAt: new Date(++state.now).toISOString() }); return quota.status(); },
    arm: () => { state.effects.push({ op: 'quota.arm' }); Object.assign(state.config, { autoKillArmed: true, tripped: false, updatedAt: new Date(++state.now).toISOString() }); return quota.status(); },
  };
  const master = {
    sustain: { config: { enabled: false } }, turbo: { config: { enabled: false } }, drainState: { enabled: false },
    locked: () => JSON.parse(fs.readFileSync(latchPath, 'utf8')).enabled,
    status: () => ({ emergency: { ...clone(state.emergency), latched: master.locked(), checkedAt: state.emergency.checkedAt ?? new Date(state.now).toISOString() } }),
    disableAutomaticDispatch: () => { state.effects.push({ op: 'hold' }); master.sustain.config.enabled = false; master.turbo.config.enabled = false; },
    rearm: ({ expectedRevision }) => {
      if (revision() !== expectedRevision) throw Error('A newer emergency stop superseded this release request');
      state.effects.push({ op: 'unlock', expectedRevision }); writeLatch(false); return master.status();
    },
    restore: {
      config: { minFreeRamGiB: 7, minFreeDiskGiB: 20, maxCpuPercent: 95 },
      state: { active: false, phase: 'idle', total: 3, restoredCount: 0 }, running: false,
      getSnapshot: () => clone(state.saved),
      targets: saved => targetsFromSnapshot(saved),
      resourceReader: async () => { state.reads.push('resources'); return clone(state.resources); },
      runtimeInspector: async projects => { state.reads.push({ op: 'runtimes', projects }); return new Map(state.counts); },
      watchdogDisarmed: async explicit => { state.reads.push({ op: 'watchdog', explicit }); return true; },
      status: () => clone(master.restore.state),
      start: options => { state.effects.push({ op: 'restore.start', options: clone(options) }); master.restore.state.active = true; return clone(master.restore.state); },
      pause: () => { state.effects.push({ op: 'restore.pause' }); master.restore.state.active = false; return clone(master.restore.state); },
    },
  };
  const launcher = {
    status: () => clone(state.launch),
    reconcile: options => { state.reads.push({ op: 'launcher.reconcile', options: clone(options) }); return clone(state.launch); },
    launch: async mode => { state.effects.push({ op: 'launch', mode }); return { requested: true }; },
  };
  const providers = { get: async () => { state.reads.push('providers'); return clone(state.catalog); } };
  const options = {
    root, master, quota, launcher, providers, now: () => state.now,
    verifyCodex: async () => { state.reads.push('verify-codex'); },
    getSessions: async () => { state.reads.push('sessions'); return clone(state.sessions); },
    runtimeHealth: (session, count) => !session.isTerminated && session.controllerRunning === true && session.activity?.state !== 'exited' && Number.isInteger(count) && count > 0,
    operationBusy: () => state.operationBusy,
  };
  const context = (extra = {}) => ({ runId: 'startup-run-001', requestId: 'startup-request-001', signal: new AbortController().signal, expectedEmergencyRevision: revision(), guardReceipt: { token: configRevision(state.config) }, autoDispatchAfter: false, ...extra });
  const protect = () => { writeLatch(false); state.config.autoKillArmed = true; state.config.tripped = false; state.acknowledgedRevision = configRevision(state.config); };
  const healthy = () => {
    state.sessions = state.saved.sessions.map(s => ({ ...s, activity: { state: 'idle' }, controllerRunning: true }));
    state.counts = new Map(state.sessions.map(s => [s.id, 2]));
  };
  return { root, state, master, quota, launcher, providers, options, context, protect, healthy, writeLatch, revision, make: () => new StartupAdapter(options) };
}

test('bootstrap is read-only and validates the preserved mixed-provider manifest while AO is offline', async t => {
  const f = fixture(t), adapter = f.make();
  const result = await adapter.bootstrap(f.context());
  assert.equal(result.ok, true); assert.equal(result.targetCount, 3); assert.deepEqual(result.primaryIds, ['mjobs-primary']);
  assert.equal(result.alreadyOnline, false); assert.deepEqual(f.state.effects, []);
  assert.equal(f.master.locked(), true); assert.equal(f.state.config.autoKillArmed, false);
  assert.equal(f.state.reads.filter(x => x === 'verify-codex').length, 1);
});

test('quota must be explicitly fresh, finite, in range, above the brake, and usable', async t => {
  const cases = {
    stale: q => { q.fresh = false; },
    'truthy nonboolean freshness': q => { q.fresh = 'true'; },
    null: q => { q.selectedRemainingPercent = null; },
    'string percentage': q => { q.selectedRemainingPercent = '100'; },
    NaN: q => { q.selectedRemainingPercent = NaN; },
    Infinity: q => { q.selectedRemainingPercent = Infinity; },
    negative: q => { q.selectedRemainingPercent = -1; },
    zero: q => { q.selectedRemainingPercent = 0; },
    '15% brake': q => { q.selectedRemainingPercent = 15; },
    'over 100%': q => { q.selectedRemainingPercent = 101; },
    'ordinary usage denied': q => { q.ordinaryUsageAllowed = false; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); f.writeLatch(false); alter(f.state.quota); const adapter = f.make();
    const result = await adapter.bootstrap(f.context());
    assert.equal(result.ok, false); assert.match(result.reason, /Fresh Codex allowance/);
    assert.throws(() => adapter.safeForMutation(f.context()), /Fresh Codex allowance/);
    assert.deepEqual(f.state.effects, []);
  });
});

test('safe bootstrap enforces each project has exactly one preserved primary', async t => {
  const cases = {
    empty: s => { s.sessions = []; },
    'invalid session id': s => { s.sessions[1].id = '../escape'; },
    'invalid project id': s => { s.sessions[1].projectId = '../../other'; },
    'duplicate id': s => { s.sessions[1].id = s.sessions[0].id; },
    'missing primary': s => { s.sessions[0].kind = 'worker'; },
    'duplicate primaries': s => { s.sessions[1].kind = 'orchestrator'; },
    'orphan second project': s => { s.sessions[2].projectId = 'other-project'; },
    'no supported providers': s => { s.sessions.forEach(item => { item.harness = 'unsupported'; }); },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); alter(f.state.saved); const adapter = f.make();
    assert.throws(() => adapter.manifest());
    assert.equal((await adapter.bootstrap(f.context())).ok, false); assert.deepEqual(f.state.effects, []);
  });
  await t.test('one explicit primary per project is valid', async st => {
    const f = fixture(st); f.state.saved.sessions.push({ id: 'other-primary', projectId: 'other-project', kind: 'orchestrator', harness: 'agy', activity: 'idle' });
    const result = await f.make().bootstrap(f.context());
    assert.equal(result.ok, true); assert.equal(result.targetCount, 4); assert.deepEqual(result.primaryIds, ['mjobs-primary', 'other-primary']);
  });
});

test('bootstrap refuses stale emergency zero verification, busy control operations and unresolved launches', async t => {
  const cases = {
    'watchdog stale': f => { f.state.emergency.checkedAt = new Date(f.state.now - 35000).toISOString(); },
    'watchdog future': f => { f.state.emergency.checkedAt = new Date(f.state.now + 1000).toISOString(); },
    'watchdog unknown age': f => { f.state.emergency.checkedAt = 'invalid'; },
    'remaining process': f => { f.state.emergency.remainingTotal = 1; },
    'zero not verified': f => { f.state.emergency.remainingTotal = null; },
    'shutdown partial': f => { f.state.emergency.phase = 'partial_stop'; },
    'control busy': f => { f.state.operationBusy = true; },
    'drain active': f => { f.master.drainState.enabled = true; },
    'restore active': f => { f.master.restore.state.active = true; },
    'restore running': f => { f.master.restore.running = true; },
    'launch pending': f => { f.state.launch.pending = true; },
    'launch uncertain': f => { f.state.launch.requiresReconciliation = true; },
    'provider unavailable': f => { f.state.catalog.providers[1].available = false; },
    'Codex missing': f => { f.options.verifyCodex = async () => { throw Error('Codex binary unavailable'); }; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); alter(f); const result = await f.make().bootstrap(f.context());
    assert.equal(result.ok, false); assert.ok(result.reason); assert.deepEqual(f.state.effects, []);
  });
});

test('resource failures cannot permit release, arming, or restoration', async t => {
  const cases = {
    'RAM low': r => { r.ramGiB = 6.99; }, 'RAM unknown': r => { r.ramGiB = null; },
    'disk low': r => { r.diskGiB = 19.99; }, 'disk unknown': r => { r.diskGiB = NaN; },
    'CPU at cutoff': r => { r.cpuPercent = 95; }, 'CPU unknown': r => { r.cpuPercent = undefined; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); alter(f.state.resources); const result = await f.make().bootstrap(f.context());
    assert.equal(result.ok, false); assert.match(result.reason, /RAM|disk|CPU/); assert.deepEqual(f.state.effects, []);
  });
});

test('latch release uses an exact on-disk CAS token and refuses a newer stop', t => {
  const f = fixture(t), adapter = f.make(), ctx = f.context();
  f.writeLatch(true, { trigger: 'newer-stop' });
  assert.throws(() => adapter.unlock(ctx), /newer emergency/); assert.equal(f.master.locked(), true); assert.deepEqual(f.state.effects, []);
  const newer = f.context(); const result = adapter.unlock(newer);
  assert.equal(result.released, true); assert.deepEqual(f.state.effects, [{ op: 'unlock', expectedRevision: newer.expectedEmergencyRevision }]);
});

test('master rearm rechecks CAS if a newer latch appears just before submission', t => {
  const f = fixture(t), adapter = f.make(), ctx = f.context(), original = f.master.rearm;
  f.master.rearm = options => { f.writeLatch(true, { trigger: 'last-moment-stop' }); return original(options); };
  assert.throws(() => adapter.unlock(ctx), /newer emergency/); assert.equal(f.master.locked(), true); assert.deepEqual(f.state.effects, []);
});

test('all exposed operations reject an already-aborted caller before side effects', async t => {
  const methods = ['safety', 'bootstrap', 'holdDispatch', 'unlock', 'watchdogDisarmed', 'arm', 'guardAck', 'readiness', 'openUi', 'restoreStart', 'restoreStatus', 'restorePause', 'reconcile'];
  for (const method of methods) await t.test(method, async st => {
    const f = fixture(st); f.protect(); const adapter = f.make(), abort = new AbortController(); abort.abort();
    await assert.rejects(Promise.resolve().then(() => adapter[method](f.context({ signal: abort.signal }))), /cancelled/);
    assert.deepEqual(f.state.effects, []);
  });
});

test('cancellation while bootstrap awaits provider discovery prevents subsequent work', async t => {
  const f = fixture(t), gate = deferred(), entered = deferred(), abort = new AbortController();
  f.providers.get = async () => { entered.resolve(); return gate.promise; };
  const work = f.make().bootstrap(f.context({ signal: abort.signal })); await entered.promise;
  abort.abort(); gate.resolve(f.state.catalog);
  await assert.rejects(work, /cancelled/); assert.deepEqual(f.state.effects, []); assert.equal(f.state.reads.includes('resources'), false);
});

test('a cancelled caller joining an in-flight readiness read cannot receive success', async t => {
  const f = fixture(t); f.protect(); f.healthy(); const gate = deferred(), entered = deferred(), abort = new AbortController();
  f.options.getSessions = async () => { entered.resolve(); await gate.promise; return clone(f.state.sessions); };
  const adapter = f.make(), first = adapter.readiness(f.context()); await entered.promise;
  const joined = adapter.readiness(f.context({ signal: abort.signal })); abort.abort(); gate.resolve();
  assert.equal((await first).ready, true); await assert.rejects(joined, /cancelled/); assert.deepEqual(f.state.effects, []);
});

test('arming writes the exact policy, returns its revision, and awaits a new matching acknowledgment', t => {
  const f = fixture(t); f.writeLatch(false); Object.assign(f.state.config, { thresholdPercent: 10, pollSeconds: 60, window: 'weekly' });
  const adapter = f.make(); const receipt = adapter.arm(f.context());
  assert.equal(receipt.armed, true); assert.equal(receipt.token, configRevision(f.state.config));
  assert.deepEqual(f.state.effects.map(x => x.op), ['quota.update', 'quota.arm']);
  assert.deepEqual(f.state.effects[0].update, { enabled: true, pollSeconds: 5, thresholdPercent: 7, window: 'any' });
  const ctx = f.context({ guardReceipt: receipt });
  assert.equal(adapter.guardAck(ctx), false);
  f.state.acknowledgedRevision = receipt.token; assert.equal(adapter.guardAck(ctx), true);
  assert.equal(adapter.guardAck(f.context({ guardReceipt: { token: 'old' } })), false);
  f.state.quota.fresh = false; assert.equal(adapter.guardAck(ctx), false);
});

test('emergency activated after policy update prevents the subsequent arm operation', t => {
  const f = fixture(t); f.writeLatch(false); const update = f.quota.update;
  f.quota.update = values => { const result = update(values); f.writeLatch(true, { trigger: 'new-stop' }); return result; };
  assert.throws(() => f.make().arm(f.context()), /Emergency stop/);
  assert.deepEqual(f.state.effects.map(x => x.op), ['quota.update']); assert.equal(f.master.locked(), true);
});

test('restore refuses changed guard configuration and acknowledgment revisions', async t => {
  const cases = {
    'old receipt': (f, ctx) => { ctx.guardReceipt.token = 'old'; },
    'stale acknowledgment': f => { f.state.acknowledgedRevision = 'old'; },
    'same settings new revision': f => { f.state.config.updatedAt = new Date(++f.state.now).toISOString(); f.state.acknowledgedRevision = configRevision(f.state.config); },
    threshold: f => { f.state.config.thresholdPercent = 8; f.state.acknowledgedRevision = configRevision(f.state.config); },
    polling: f => { f.state.config.pollSeconds = 60; f.state.acknowledgedRevision = configRevision(f.state.config); },
    window: f => { f.state.config.window = 'weekly'; f.state.acknowledgedRevision = configRevision(f.state.config); },
    disabled: f => { f.state.config.enabled = false; },
    unarmed: f => { f.state.config.autoKillArmed = false; },
    tripped: f => { f.state.config.tripped = true; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); f.protect(); const ctx = f.context(); alter(f, ctx);
    await assert.rejects(f.make().restoreStart(ctx)); assert.deepEqual(f.state.effects, []);
  });
});

test('restore composes exactly one existing operation, preserves the manifest and never enables dispatch', async t => {
  const f = fixture(t); f.protect(); const adapter = f.make(), ctx = f.context();
  await adapter.restoreStart(ctx);
  const starts = f.state.effects.filter(x => x.op === 'restore.start');
  assert.equal(starts.length, 1); assert.deepEqual(starts[0].options, { autoDispatchAfter: false, manifest: f.state.saved, fromEmergency: true });
  await assert.rejects(adapter.restoreStart(ctx), /already running/);
  assert.equal(f.state.effects.filter(x => x.op === 'restore.start').length, 1);
  assert.equal(f.master.sustain.config.enabled, false); assert.equal(f.master.turbo.config.enabled, false);
});

test('a restore becoming active after preview cannot be submitted twice', async t => {
  const f = fixture(t), adapter = f.make(); assert.equal((await adapter.bootstrap(f.context())).ok, true);
  f.protect(); f.master.restore.running = true;
  await assert.rejects(adapter.restoreStart(f.context()), /already running/); assert.deepEqual(f.state.effects, []);
});

test('historical restore completion and process counts do not replace current controller health', async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.state.sessions[0].controllerRunning = false;
  Object.assign(f.master.restore.state, { phase: 'complete', restoredCount: 3, total: 3, active: false });
  const status = await f.make().readiness(f.context());
  assert.equal(status.ready, false); assert.equal(status.runtimesReady, false); assert.deepEqual(status.missing, ['mjobs-primary']);
  assert.equal(status.verified, 2); assert.deepEqual(f.state.effects, []);
});

test('readiness requires both live sessions and runtime verification, and detects competing primaries', async t => {
  for (const scenario of ['missing session', 'missing process evidence', 'another primary', 'daemon error']) await t.test(scenario, async st => {
    const f = fixture(st); f.protect(); f.healthy();
    if (scenario === 'missing session') f.state.sessions.pop();
    if (scenario === 'missing process evidence') f.state.counts.delete('mjobs-codex-1');
    if (scenario === 'another primary') f.state.sessions.push({ id: 'mjobs-other-primary', projectId: 'mjobs', kind: 'orchestrator', activity: { state: 'idle' } });
    if (scenario === 'daemon error') f.options.getSessions = async () => { throw Error('Daemon offline'); };
    const status = await f.make().readiness(f.context()); assert.equal(status.ready, false); assert.equal(status.runtimesReady, false);
    if (scenario === 'another primary') assert.deepEqual(status.conflicts, ['mjobs-other-primary']);
    if (scenario === 'daemon error') assert.equal(status.daemonOnline, false);
    assert.deepEqual(f.state.effects, []);
  });
});

test('live readiness is re-evaluated after cache expiry and is immediately decorated with current safety', async t => {
  const f = fixture(t); f.protect(); f.healthy(); const adapter = f.make();
  assert.equal((await adapter.readiness(f.context())).ready, true);
  f.writeLatch(true); assert.equal((await adapter.readiness(f.context())).ready, false);
  f.writeLatch(false); f.state.quota.fresh = false; assert.equal((await adapter.readiness(f.context())).ready, false);
  f.state.quota.fresh = true; f.state.sessions[0].controllerRunning = false; f.state.now += 3001;
  assert.equal((await adapter.readiness(f.context())).ready, false); assert.deepEqual(f.state.effects, []);
});

test('healthy already-online fleet is recognized without submitting a restore or launch', async t => {
  const f = fixture(t); f.protect(); f.healthy();
  const result = await f.make().bootstrap(f.context()); assert.equal(result.ok, true); assert.equal(result.alreadyOnline, true);
  assert.deepEqual(f.state.effects, []);
});

test('Open UI delegates one request only after guard, quota and cancellation checks', async t => {
  const f = fixture(t); f.protect(); const adapter = f.make();
  await adapter.openUi(f.context()); assert.deepEqual(f.state.effects, [{ op: 'launch', mode: 'ui' }]);
  f.writeLatch(true); await assert.rejects(adapter.openUi(f.context()), /Emergency stop/);
  assert.equal(f.state.effects.length, 1);
});

test('deps exposes bound adapter methods and only the existing restoration interface', async t => {
  const f = fixture(t), adapter = f.make(), deps = adapter.deps();
  assert.deepEqual(Object.keys(deps.restore).sort(), ['pause', 'start', 'status']);
  const safety = deps.safety; assert.equal(safety(f.context()).emergency.latched, true);
  const watchdog = deps.watchdogDisarmed; assert.equal(await watchdog(f.context()), true);
  assert.deepEqual(f.state.reads, [{ op: 'watchdog', explicit: true }]); assert.deepEqual(f.state.effects, []);
});

test('manifest identity changed after bootstrap cannot submit a restore', async t => {
  const f = fixture(t); f.protect(); const adapter = f.make(), bootstrap = await adapter.bootstrap(f.context());
  assert.equal(bootstrap.ok, true); assert.ok(bootstrap.manifestRevision);
  f.state.saved.sessions[1].id = 'mjobs-replaced-worker';
  await assert.rejects(adapter.restoreStart(f.context({ bootstrap })), /fleet changed/);
  assert.deepEqual(f.state.effects, []);
});

test('UI launcher receives the same abort signal and a last-moment safety callback', async t => {
  for (const scenario of ['cancelled', 'emergency', 'quota stale']) await t.test(scenario, async st => {
    const f = fixture(st); f.protect(); const abort = new AbortController(), gate = deferred(), entered = deferred();
    f.launcher.launch = async (mode, options) => {
      assert.equal(mode, 'ui'); assert.equal(options.signal, abort.signal); assert.equal(typeof options.beforeTrigger, 'function');
      entered.resolve(); await gate.promise;
      options.beforeTrigger(); f.state.effects.push({ op: 'native-ui-trigger' });
    };
    const work = f.make().openUi(f.context({ signal: abort.signal })); await entered.promise;
    if (scenario === 'cancelled') abort.abort();
    if (scenario === 'emergency') f.writeLatch(true, { trigger: 'during-launch-preflight' });
    if (scenario === 'quota stale') f.state.quota.fresh = false;
    gate.resolve(); await assert.rejects(work); assert.deepEqual(f.state.effects, []);
  });
});

function inspectionContext(f, adapter, previous = {}) {
  const manifest = adapter.manifest();
  return f.context({
    expectedStateRevision: 'startup-revision-001', inspectionStartedAt: new Date(f.state.now).toISOString(),
    previousRun: {
      schemaVersion: 1, phase: 'uncertain', restoreStartAttempted: false, restoreStarted: false, actionPending: null,
      bootstrap: { manifestRevision: manifest.revision, targetCount: manifest.targets.length, targetIds: manifest.targets.map(s => s.id) },
      error: { code: 'LOST_OWNER' }, ...previous,
    },
  });
}

test('read-only reconciliation returns fresh ready proof for the exact currently healthy protected fleet', async t => {
  const f = fixture(t); f.protect(); f.healthy(); const adapter = f.make(), ctx = inspectionContext(f, adapter, { restoreStartAttempted: true, restoreStarted: true });
  const proof = await adapter.reconcile(ctx);
  assert.equal(proof.outcome, 'ready_paused'); assert.equal(proof.runId, ctx.runId); assert.equal(proof.stateRevision, ctx.expectedStateRevision);
  assert.equal(proof.noOperationInFlight, true); assert.equal(proof.dispatchPaused, true);
  assert.equal(proof.expectedTargetsMatch, true); assert.equal(proof.expectedTargetCount, 3); assert.equal(proof.verifiedTargetCount, 3);
  assert.equal(proof.guard.revision, configRevision(f.state.config)); assert.equal(proof.guard.acknowledgedRevision, proof.guard.revision);
  assert.deepEqual(f.state.effects, []);
});

test('read-only reconciliation can prove idle only with explicit no-submission state and all zero process counts', async t => {
  const f = fixture(t); f.protect(); f.state.counts = new Map(f.state.saved.sessions.map(s => [s.id, 0]));
  const adapter = f.make(), proof = await adapter.reconcile(inspectionContext(f, adapter));
  assert.equal(proof.outcome, 'idle'); assert.equal(proof.submissionAuditComplete, true); assert.equal(proof.submittedResume, false);
  assert.equal(proof.runningExpectedAgents, 0); assert.deepEqual(f.state.effects, []);
});

test('unknown audit flags, invalid counts or possible submitted work cannot be reconciled to idle', async t => {
  const cases = {
    'restore attempt unknown': (f, ctx) => { delete ctx.previousRun.restoreStartAttempted; },
    'restore started unknown': (f, ctx) => { delete ctx.previousRun.restoreStarted; },
    'pending action unknown': (f, ctx) => { delete ctx.previousRun.actionPending; },
    'attempted resume': (f, ctx) => { ctx.previousRun.restoreStartAttempted = true; },
    'started resume': (f, ctx) => { ctx.previousRun.restoreStarted = true; },
    'pending action': (f, ctx) => { ctx.previousRun.actionPending = { name: 'restore' }; },
    'unreadable state': (f, ctx) => { ctx.previousRun.error = { code: 'STATE_UNREADABLE' }; },
    'missing process count': f => { f.state.counts.delete('mjobs-codex-1'); },
    'negative process count': f => { f.state.counts.set('mjobs-codex-1', -1); },
    'running process': f => { f.state.counts.set('mjobs-codex-1', 1); },
    'daemon unavailable': f => { f.options.getSessions = async () => { throw Error('No daemon'); }; },
    'restore active': f => { f.master.restore.state.active = true; },
    'restore running': f => { f.master.restore.running = true; },
    'launcher pending': f => { f.state.launch.pending = true; },
    'launcher owner uncertain': f => { f.state.launch.requiresReconciliation = true; },
    'dispatch enabled': f => { f.master.sustain.config.enabled = true; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); f.protect(); f.state.counts = new Map(f.state.saved.sessions.map(s => [s.id, 0]));
    const ctx = inspectionContext(f, f.make()); alter(f, ctx);
    const proof = await f.make().reconcile(ctx); assert.notEqual(proof.outcome, 'idle'); assert.deepEqual(f.state.effects, []);
  });
});

test('an uncertain launcher cannot be reconciled as healthy even if every runtime is present', async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.state.launch.requiresReconciliation = true;
  const adapter = f.make(), proof = await adapter.reconcile(inspectionContext(f, adapter, { restoreStartAttempted: true }));
  assert.equal(proof.outcome, 'unknown'); assert.deepEqual(f.state.effects, []);
});

test('a forced reconciliation does not reuse an older in-flight runtime read for a different manifest', async t => {
  const f = fixture(t); f.protect(); f.healthy(); const oldSessions = clone(f.state.sessions), oldCounts = new Map(f.state.counts);
  const gate = deferred(), entered = deferred(); let sessionReads = 0, countReads = 0;
  f.options.getSessions = async () => {
    if (++sessionReads === 1) { entered.resolve(); await gate.promise; return oldSessions; }
    return clone(f.state.sessions);
  };
  f.master.restore.runtimeInspector = async () => ++countReads === 1 ? oldCounts : new Map(f.state.counts);
  const adapter = f.make(), oldRead = adapter.readiness(f.context()); await entered.promise;
  f.state.saved.sessions[1].id = 'mjobs-new-worker'; f.healthy(); f.state.sessions[0].controllerRunning = false;
  const checking = adapter.reconcile(inspectionContext(f, adapter, { restoreStartAttempted: true, restoreStarted: true }));
  gate.resolve(); await oldRead; const proof = await checking;
  assert.notEqual(proof.outcome, 'ready_paused'); assert.equal(proof.allExpectedControllersHealthy, false);
  assert.ok(sessionReads >= 2); assert.deepEqual(f.state.effects, []);
});

test('Startup preview and restore submission preserve a previous durable uncertain native operation', async t => {
  for (const ledger of ['inflight', 'uncertain error']) await t.test(ledger, async st => {
    const f = fixture(st); f.protect();
    if (ledger === 'inflight') f.master.restore.state.inflight = { 'mjobs-primary': { command: 'resume-agent', at: new Date(f.state.now - 60000).toISOString() } };
    else f.master.restore.state.errors = { 'mjobs-primary': { uncertain: true } };
    const previous = clone(f.master.restore.state), adapter = f.make();
    const preview = await adapter.bootstrap(f.context()); assert.equal(preview.ok, false); assert.match(preview.reason, /unverified/);
    assert.equal(preview.reconciliationRequired, true); assert.equal(preview.targetCount, 3);
    assert.equal(preview.manifestRevision, adapter.manifest().revision); assert.deepEqual(preview.targetIds, f.state.saved.sessions.map(s => s.id));
    assert.deepEqual(preview.primaryIds, ['mjobs-primary']);
    await assert.rejects(adapter.restoreStart(f.context()), /unverified/);
    assert.deepEqual(f.master.restore.state, previous); assert.deepEqual(f.state.effects, []);
  });
});

test('zero current processes cannot prove idle while an earlier Restore has a durable submitted command', async t => {
  const f = fixture(t); f.protect(); f.state.counts = new Map(f.state.saved.sessions.map(s => [s.id, 0]));
  f.master.restore.state.inflight = { 'mjobs-primary': { command: 'resume-agent', at: new Date(f.state.now - 60000).toISOString() } };
  const previous = clone(f.master.restore.state), adapter = f.make();
  const proof = await adapter.reconcile(inspectionContext(f, adapter));
  assert.equal(proof.outcome, 'unknown'); assert.equal(proof.noOperationInFlight, false);
  assert.deepEqual(proof.unresolvedSubmittedIds, ['mjobs-primary']); assert.match(proof.reason, /unverified/);
  assert.deepEqual(f.master.restore.state, previous); assert.deepEqual(f.state.effects, []);
});

test('fresh healthy evidence may reconcile ready without erasing prior submitted-command identities', async t => {
  const f = fixture(t); f.protect(); f.healthy();
  f.master.restore.state.inflight = { 'mjobs-primary': { command: 'resume-agent', at: new Date(f.state.now - 60000).toISOString() } };
  f.master.restore.state.errors = { 'mjobs-codex-1': { uncertain: true } };
  const previous = clone(f.master.restore.state), adapter = f.make();
  const proof = await adapter.reconcile(inspectionContext(f, adapter, { restoreStartAttempted: true, restoreStarted: true }));
  assert.equal(proof.outcome, 'ready_paused'); assert.equal(proof.noOperationInFlight, true);
  assert.deepEqual(proof.durableSubmittedIds.sort(), ['mjobs-codex-1', 'mjobs-primary']); assert.deepEqual(proof.unresolvedSubmittedIds, []);
  assert.deepEqual(f.master.restore.state, previous); assert.deepEqual(f.state.effects, []);
});

test('an unresolved submitted ID outside the current healthy manifest cannot be ignored', async t => {
  const f = fixture(t); f.protect(); f.healthy();
  f.master.restore.state.inflight = { 'other-old-primary': { command: 'resume-agent' } };
  const adapter = f.make(), proof = await adapter.reconcile(inspectionContext(f, adapter));
  assert.equal(proof.outcome, 'unknown'); assert.equal(proof.noOperationInFlight, false);
  assert.equal((await adapter.readiness(f.context())).ready, false);
  assert.deepEqual(proof.unresolvedSubmittedIds, ['other-old-primary']); assert.deepEqual(f.state.effects, []);
});

test('bootstrap retires only default-safe terminal launch locks, without opting in to cancellation recovery', async t => {
  const f = fixture(t); f.state.launch.pending = true; const calls = [];
  f.launcher.reconcile = options => {
    calls.push(options); f.state.launch.pending = false; return clone(f.state.launch);
  };
  const result = await f.make().bootstrap(f.context());
  assert.equal(result.ok, true); assert.deepEqual(calls, [undefined]); assert.deepEqual(f.state.effects, []);
});

test('an unresolved prior launch keeps validated target identities and becomes explicitly recheckable', async t => {
  const f = fixture(t); f.state.launch.pending = true; f.state.launch.requiresReconciliation = true;
  const adapter = f.make(), result = await adapter.bootstrap(f.context());
  assert.equal(result.ok, false); assert.equal(result.reconciliationRequired, true);
  assert.equal(result.targetCount, 3); assert.equal(result.manifestRevision, adapter.manifest().revision);
  assert.deepEqual(result.targetIds, f.state.saved.sessions.map(s => s.id));
  assert.match(result.reason, /Recheck safely/); assert.deepEqual(f.state.effects, []);
});

test('only explicit reconciliation requests verified completed-cancellation retirement; refresh does not', async t => {
  const f = fixture(t); f.protect(); f.healthy(); const calls = [];
  f.state.launch = { pending: true, requiresReconciliation: true, cancelRequested: true };
  f.launcher.reconcile = options => {
    calls.push(options);
    if (options?.acceptCompletedCancellation === true) f.state.launch = { pending: false, requiresReconciliation: false, cancelRequested: true, state: 'ok' };
    return clone(f.state.launch);
  };
  const adapter = f.make();
  await adapter.readiness(f.context(), { force: true }); assert.deepEqual(calls, []);
  const boot = await adapter.bootstrap(f.context()); assert.equal(boot.reconciliationRequired, true);
  assert.deepEqual(calls, [undefined]);
  const proof = await adapter.reconcile(inspectionContext(f, adapter));
  assert.deepEqual(calls, [undefined, { acceptCompletedCancellation: true }]);
  assert.equal(proof.outcome, 'ready_paused'); assert.equal(proof.launchActive, false); assert.deepEqual(f.state.effects, []);
});

test('cancellation during explicit runtime inspection cannot retire a launcher lock afterward', async t => {
  const f = fixture(t); f.protect(); f.healthy(); const gate = deferred(), entered = deferred(), abort = new AbortController();
  f.options.getSessions = async () => { entered.resolve(); await gate.promise; return clone(f.state.sessions); };
  const adapter = f.make(), ctx = inspectionContext(f, adapter); ctx.signal = abort.signal;
  const work = adapter.reconcile(ctx); await entered.promise; abort.abort(); gate.resolve();
  await assert.rejects(work, /cancelled/);
  assert.equal(f.state.reads.some(x => x?.op === 'launcher.reconcile'), false); assert.deepEqual(f.state.effects, []);
});

test('a previous manual Restore can reach explicit healthy adoption through Startup without replay or ledger loss', async t => {
  const f = fixture(t); f.protect(); f.healthy();
  f.master.restore.state.inflight = { 'mjobs-primary': { command: 'resume-agent', at: new Date(f.state.now - 60000).toISOString() } };
  const previous = clone(f.master.restore.state), adapter = f.make();
  const controller = new StartupController({ root: f.root, deps: adapter.deps(), now: () => f.state.now, pollMs: 1 });
  controller.start({ confirm: 'STARTUP', requestId: 'prior-manual-restore-01' });
  const blocked = await controller.waitForIdle();
  assert.equal(blocked.phase, 'uncertain'); assert.equal(blocked.reconciliationRequired, true);
  assert.equal(blocked.bootstrap.manifestRevision, adapter.manifest().revision);
  assert.deepEqual(blocked.bootstrap.targetIds, f.state.saved.sessions.map(s => s.id));
  assert.throws(() => controller.start({ confirm: 'STARTUP', requestId: 'prior-manual-restore-02' }), /Reconcile/);
  const result = await controller.reconcile({ confirm: 'RECHECK STARTUP', runId: blocked.runId });
  assert.equal(result.reconciled, true); assert.equal(result.run.phase, 'ready_paused');
  assert.deepEqual(f.master.restore.state, previous); assert.deepEqual(f.state.effects, []);
});
