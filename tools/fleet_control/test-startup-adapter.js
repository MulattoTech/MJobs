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
const { nativeSessionIdentity, attachWindowsStoppedEvidence } = require('./windows-runtime-evidence');

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
    sessions: [], counts: new Map(), stoppedEvidence: new Map(),
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
      runtimeInspector: async projects => {
        state.reads.push({ op: 'runtimes', projects }); const counts = new Map(state.counts);
        counts.stoppedEvidence = clone(state.stoppedEvidence); return counts;
      },
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
function proveNativeAbsent(f) {
  f.state.saved.sessions.forEach((s, i) => { s.mode = i === 0 ? 'chat' : 'tui'; });
  f.state.sessions = f.state.saved.sessions.map(s => ({ ...s, activity: { state: 'exited' }, isTerminated: false,
    statusReadiness: 'ready', createdAt: new Date(f.state.now - 60000).toISOString(), updatedAt: new Date(f.state.now).toISOString(),
    terminalGeneration: 'fixture-generation', controllerState: 'stopped', controllerVerified: s.mode === 'chat' }));
  f.state.counts = new Map(f.state.sessions.map(s => [s.id, null]));
  f.state.stoppedEvidence = new Map(f.state.sessions.map(s => [s.id, { version: 1, type: 'native-runtime-absent', probeId: 'a'.repeat(32),
    observedAt: new Date(f.state.now).toISOString(), identity: nativeSessionIdentity(s),
    proof: { completeInventory: true, stableInventory: true, stableRegistry: true, instanceVerified: true, stableInstance: true, noMatchingHost: true, ownershipClear: true, noSessionOwnedProvider: true, registryAbsent: s.mode === 'tui', chatOwnershipClear: s.mode === 'chat' } }]));
}

test('bootstrap is read-only and validates the preserved mixed-provider manifest while AO is offline', async t => {
  const f = fixture(t), adapter = f.make();
  const result = await adapter.bootstrap(f.context());
  assert.equal(result.ok, true); assert.equal(result.targetCount, 3); assert.deepEqual(result.primaryIds, ['mjobs-primary']);
  assert.equal(result.alreadyOnline, false); assert.deepEqual(f.state.effects, []);
  assert.equal(f.master.locked(), true); assert.equal(f.state.config.autoKillArmed, false);
  assert.equal(f.state.reads.filter(x => x === 'verify-codex').length, 1);
});

test('real Startup controller preserves the reviewed branch revision through the real adapter restore start',async t=>{
  const f=fixture(t);f.protect();const approval='a'.repeat(64);
  f.master.restore.branchApproval=()=>({revision:approval});
  const arm=f.quota.arm;f.quota.arm=()=>{const result=arm();f.state.acknowledgedRevision=configRevision(f.state.config);return result;};
  f.master.restore.start=options=>{
    f.state.effects.push({op:'restore.start',options:clone(options)});
    f.healthy();f.state.now+=4000;
    Object.assign(f.master.restore.state,{active:false,phase:'complete',total:3,restoredCount:3,startedAt:new Date(f.state.now).toISOString()});
    return clone(f.master.restore.state);
  };
  const adapter=f.make(),controller=new StartupController({root:f.root,deps:adapter.deps(),now:()=>f.state.now,pollMs:1});
  controller.start({confirm:'STARTUP',requestId:'branch-revision-propagation-001'});
  const result=await controller.waitForIdle();assert.equal(result.phase,'ready_paused');
  assert.equal(result.bootstrap.branchAdoptionsRevision,approval);
  assert.equal(JSON.parse(fs.readFileSync(path.join(f.root,'startup-state.json'),'utf8')).bootstrap.branchAdoptionsRevision,approval);
  const starts=f.state.effects.filter(value=>value.op==='restore.start');assert.equal(starts.length,1);
  assert.equal(starts[0].options.branchAdoptionsRevision,approval);assert.equal(starts[0].options.startupRunId,result.runId);
});

test('controller rejects a malformed bootstrap branch revision before startup actions',async t=>{
  const f=fixture(t);f.protect();f.master.restore.branchApproval=()=>({revision:'invalid-revision'});
  const controller=new StartupController({root:f.root,deps:f.make().deps(),now:()=>f.state.now,pollMs:1});
  controller.start({confirm:'STARTUP',requestId:'invalid-branch-revision-001'});
  const result=await controller.waitForIdle();assert.equal(result.error.code,'BOOTSTRAP_BLOCKED');assert.deepEqual(f.state.effects,[]);
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
  assert.equal(starts.length, 1); assert.deepEqual(starts[0].options, { autoDispatchAfter: false, manifest: f.state.saved, fromEmergency: true, startupRunId:ctx.runId, branchAdoptionsRevision:null });
  await assert.rejects(adapter.restoreStart(ctx), /already running/);
  assert.equal(f.state.effects.filter(x => x.op === 'restore.start').length, 1);
  assert.equal(f.master.sustain.config.enabled, false); assert.equal(f.master.turbo.config.enabled, false);
});

test('a new protected Startup binds the reviewed branch mapping revision and refuses a changed mapping before restore',async t=>{
  const f=fixture(t);f.protect();f.healthy();let revision='a'.repeat(64);
  f.master.restore.branchApproval=()=>({revision});
  const adapter=f.make(),ctx=f.context();ctx.bootstrap=await adapter.bootstrap(ctx);
  assert.equal(ctx.bootstrap.ok,true);assert.equal(ctx.bootstrap.branchAdoptionsRevision,revision);
  revision='b'.repeat(64);await assert.rejects(adapter.restoreStart(ctx),/mapping changed/);assert.deepEqual(f.state.effects,[]);
  revision='a'.repeat(64);await adapter.restoreStart(ctx);
  const operation=f.state.effects.find(x=>x.op==='restore.start');
  assert.equal(operation.options.branchAdoptionsRevision,revision);assert.equal(operation.options.startupRunId,ctx.runId);
});

test('read-only reconciliation cannot adopt a reviewed branch that was not bound to this Startup run',async t=>{
  for(const changed of ['unbound approval','different run','matching binding'])await t.test(changed,async st=>{
    const f=fixture(st);f.protect();f.healthy();
    f.master.restore.state.source={startupRunId:'startup-run-001',branchAdoptions:{revision:'a'.repeat(64)}};
    f.master.restore.runtimeTarget=async(target,session)=>({matches:true,adoption:target.kind==='worker'?{approvalRevision:'a'.repeat(64)}:null});
    const adapter=f.make(),ctx=inspectionContext(f,adapter,{runId:'startup-run-001',restoreStartAttempted:true,restoreStarted:true});
    if(changed!=='unbound approval')ctx.previousRun.bootstrap.branchAdoptionsRevision='a'.repeat(64);
    if(changed==='different run')f.master.restore.state.source.startupRunId='other-startup-run';
    const proof=await adapter.reconcile(ctx);
    assert.equal(proof.outcome,changed==='matching binding'?'ready_paused':'unknown');assert.deepEqual(f.state.effects,[]);
  });
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

test('slow or stuck runtime inspection returns bounded pending UI responses with one shared inspector', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5;
  const gate = deferred(); let inspections = 0;
  f.master.restore.runtimeInspector = async () => { inspections++; await gate.promise; return new Map(f.state.counts); };
  const adapter = f.make(), first = await adapter.readiness();
  assert.equal(first.inspectionPending, true); assert.equal(first.ready, false); assert.equal(first.runtimesReady, false);
  assert.equal(first.processCountsComplete, false); assert.equal(first.runningExpectedAgents, null); assert.equal(first.daemonOnline, null);
  assert.deepEqual(first.healthyIds, []); assert.equal(inspections, 1); assert.equal(adapter.reading.waiters.size, 0);
  const polls = await Promise.all(Array.from({ length: 8 }, () => adapter.readiness()));
  assert.ok(polls.every(value => value.inspectionPending && value.ready === false));
  assert.equal(inspections, 1); assert.equal(adapter.reading.waiters.size, 0);
  gate.resolve(); const completed = await adapter.readiness();
  assert.equal(completed.ready, true); assert.equal(completed.inspectionPending, false); assert.equal(inspections, 1);
  assert.equal(adapter.reading, null); assert.deepEqual(f.state.effects, []);
});

test('stale or clock-reversed healthy cache is never returned as ready while a refresh is slow', { timeout: 1500 }, async t => {
  for (const clockChange of [4000, -1000]) await t.test(String(clockChange), async st => {
    const f = fixture(st); f.protect(); f.healthy(); f.options.readinessWaitMs = 5; const adapter = f.make();
    const previous = await adapter.readiness(); assert.equal(previous.ready, true); f.state.now += clockChange;
    f.state.sessions[0].controllerRunning = false;
    const gate = deferred(); f.master.restore.runtimeInspector = async () => { await gate.promise; return new Map(f.state.counts); };
    const pending = await adapter.readiness();
    assert.equal(pending.ready, false); assert.equal(pending.stale, true); assert.equal(pending.inspectionPending, true);
    assert.equal(pending.verified, 0); assert.deepEqual(pending.healthyIds, []); assert.equal(pending.lastObservedAt, previous.at);
    gate.resolve(); assert.equal((await adapter.readiness()).ready, false); assert.deepEqual(f.state.effects, []);
  });
});

test('manifest changes invalidate even a young readiness cache without exposing stale health', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5; const adapter = f.make();
  assert.equal((await adapter.readiness()).ready, true);
  f.state.saved.sessions[1].id = 'mjobs-new-worker'; f.healthy();
  const gate = deferred(); f.master.restore.runtimeInspector = async () => { await gate.promise; return new Map(f.state.counts); };
  const pending = await adapter.readiness(); assert.equal(pending.ready, false); assert.equal(pending.inspectionPending, true);
  gate.resolve(); const current = await adapter.readiness(); assert.equal(current.ready, true);
  assert.ok(current.healthyIds.includes('mjobs-new-worker')); assert.deepEqual(f.state.effects, []);
});

test('forced readiness waits for a post-request observation rather than an old background result', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5;
  const oldGate = deferred(), newGate = deferred(), newEntered = deferred(); let inspections = 0, settled = false;
  f.master.restore.runtimeInspector = async () => {
    if (++inspections === 1) await oldGate.promise;
    else { newEntered.resolve(); await newGate.promise; }
    return new Map(f.state.counts);
  };
  const adapter = f.make(); assert.equal((await adapter.readiness()).inspectionPending, true);
  const forced = adapter.readiness(f.context(), { force: true }).then(value => { settled = true; return value; });
  oldGate.resolve(); await newEntered.promise;
  assert.equal(settled, false); assert.equal(inspections, 2);
  f.state.now += 4000; assert.equal((await adapter.readiness()).inspectionPending, true); assert.equal(inspections, 2);
  newGate.resolve(); assert.equal((await forced).ready, true); assert.equal(inspections, 2); assert.deepEqual(f.state.effects, []);
});

test('aborting a forced waiter leaves the shared inspector intact and does not queue a later recovery action', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5;
  f.master.restore.state.errors = { 'mjobs-primary': { uncertain: true, attempts: 2 } };
  const before = clone(f.master.restore.state), gate = deferred(), abort = new AbortController(); let inspections = 0;
  f.master.restore.runtimeInspector = async () => { inspections++; await gate.promise; return new Map(f.state.counts); };
  const adapter = f.make(); await adapter.readiness();
  const checking = adapter.reconcile({ ...inspectionContext(f, adapter), signal: abort.signal }); abort.abort();
  await assert.rejects(checking, /cancelled/); assert.equal(inspections, 1); assert.equal(adapter.reading.waiters.size, 0);
  assert.equal(f.state.reads.some(item => item?.op === 'launcher.reconcile'), false);
  gate.resolve(); await adapter.readiness(); assert.equal(inspections, 1);
  assert.deepEqual(f.master.restore.state, before); assert.deepEqual(f.state.effects, []);
});

test('a background inspection failure settles as unavailable without retry overlap or prior healthy fallback', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5; const adapter = f.make();
  assert.equal((await adapter.readiness()).ready, true); f.state.now += 4000;
  const gate = deferred(); let inspections = 0;
  f.master.restore.runtimeInspector = async () => { inspections++; await gate.promise; throw Error('Synthetic private inspector detail'); };
  assert.equal((await adapter.readiness()).inspectionPending, true); gate.resolve();
  const failed = await adapter.readiness(); assert.equal(failed.ready, false); assert.equal(failed.daemonOnline, null);
  assert.equal(failed.errorCode, 'RUNTIME_INSPECTION_FAILED', 'An unreadable native inventory does not establish daemon failure');
  assert.equal(failed.inspectionPending, false); assert.doesNotMatch(JSON.stringify(failed), /private inspector/);
  assert.equal(inspections, 1); assert.equal(adapter.reading, null); assert.deepEqual(f.state.effects, []);
});

test('completed inspection uses controller state observed after a slow native probe, not a pre-probe healthy snapshot', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5;
  const gate = deferred();
  f.master.restore.runtimeInspector = async () => { await gate.promise; return new Map(f.state.counts); };
  const adapter = f.make(); assert.equal((await adapter.readiness()).inspectionPending, true);
  f.state.sessions[0].controllerRunning = false; gate.resolve();
  const result = await adapter.readiness(); assert.equal(result.ready, false); assert.equal(result.runtimesReady, false);
  assert.ok(result.missing.includes('mjobs-primary')); assert.deepEqual(f.state.effects, []);
});

test('a slow forced bootstrap cannot mistake a bounded UI pending result for passed safety validation', { timeout: 1500 }, async t => {
  const f = fixture(t); f.protect(); f.healthy(); f.options.readinessWaitMs = 5;
  f.state.sessions.push({ ...clone(f.state.sessions[0]), id: 'conflicting-primary' });
  const gate = deferred(), entered = deferred(); let settled = false;
  f.master.restore.runtimeInspector = async () => { entered.resolve(); await gate.promise; return new Map(f.state.counts); };
  const adapter = f.make(), bootstrap = adapter.bootstrap(f.context()).then(value => { settled = true; return value; });
  await entered.promise; const pending = await adapter.readiness(); assert.equal(pending.inspectionPending, true);
  assert.equal(settled, false); assert.deepEqual(f.state.effects, []);
  gate.resolve(); const result = await bootstrap; assert.equal(result.ok, false); assert.match(result.reason, /primary controller conflicts/);
  assert.deepEqual(f.state.effects, []);
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

function partialRecovery(f) {
  f.protect(); f.state.saved.at = new Date(f.state.now - 60000).toISOString(); proveNativeAbsent(f);
  const primary = f.state.sessions[0];
  Object.assign(primary, { activity: { state: 'idle' }, controllerRunning: true, controllerState: 'ready', controllerVerified: true });
  f.state.counts.set(primary.id, 3); f.state.stoppedEvidence.delete(primary.id);
  const at = delta => new Date(f.state.now + delta).toISOString();
  Object.assign(f.master.restore.state, { active: false, phase: 'paused', startedAt: at(-8000),
    source: { snapshotAt: f.state.saved.at, targets: f.state.saved.sessions.map(s => ({ id: s.id, projectId: s.projectId, kind: s.kind, harness: s.harness, model: s.model, mode: s.mode })) },
    desiredIds: f.state.saved.sessions.map(s => s.id), inflight: {},
    errors: { 'mjobs-codex-1': { message: 'Previous read-only evidence was unavailable', uncertain: false } },
  });
  const adapter = f.make(), ctx = inspectionContext(f, adapter, {
    runId: 'startup-run-001', startedAt: at(-10000), finishedAt: at(-1000),
    restoreStartAttempted: true, restoreStarted: true, actionPending: null,
    events: [{ phase: 'starting_restore', at: at(-9000) }, { phase: 'restoring', at: at(-7000) }],
  });
  return { adapter, ctx };
}
function archivedPartialRecovery(f) {
  const { adapter, ctx } = partialRecovery(f);
  for (const saved of f.state.saved.sessions) Object.assign(saved, { isTerminated: false, model: 'preserved-model', branch: 'preserved/' + saved.id });
  for (const live of f.state.sessions) {
    const saved = f.state.saved.sessions.find(s => s.id === live.id);
    Object.assign(live, { model: saved.model, branch: saved.branch, isTerminated: live.kind !== 'orchestrator' });
    if (live.isTerminated) f.state.counts.delete(live.id);
  }
  f.master.restore.state.source.targets = f.state.saved.sessions.map(s => ({ id: s.id, projectId: s.projectId,
    kind: s.kind, harness: s.harness, model: s.model, mode: s.mode }));
  ctx.previousRun.bootstrap.manifestRevision = adapter.manifest().revision;
  f.master.restore.runtimeInspector = async (projects, { restorationScope } = {}) => attachWindowsStoppedEvidence(
    new Map(f.state.counts), clone(f.state.sessions), { platform: 'win32', now: () => f.state.now, restorationScope,
      readSessions: async () => clone(f.state.sessions), runProbe: async request => ({ version: 1, probeId: request.probeId,
        observedAt: new Date(f.state.now).toISOString(), complete: true, results: request.targets.map(s => ({ id: s.id, absent: true,
          proof: { completeInventory: true, stableInventory: true, stableRegistry: true, noMatchingHost: true,
            ownershipClear: true, noSessionOwnedProvider: true, registryAbsent: true, chatOwnershipClear: true, instanceVerified: true, stableInstance: true } })) }),
    });
  return { adapter, ctx };
}

function uncommittedRecovery(f,{legacy=true}={}){
  const {adapter,ctx}=archivedPartialRecovery(f),at=delta=>new Date(f.state.now+delta).toISOString();
  Object.assign(f.master.restore.state,{branchAdoptionReceipts:{}});
  if(!legacy)f.master.restore.state.source.startupRunId='prior-committed-startup';
  const file=path.join(f.root,'restore-state.json');f.master.restore.statePath=file;
  const persist=()=>{
    const value=clone(f.master.restore.state);if(legacy)delete value.branchAdoptionReceipts;
    fs.writeFileSync(file,JSON.stringify(value));
  };
  persist();
  ctx.runId='rejected-startup-001';ctx.previousRun.runId=ctx.runId;
  Object.assign(ctx.previousRun,{startedAt:at(-4000),finishedAt:at(-1000),restoreStartAttempted:true,restoreStarted:false,restoreReceipt:null,
    actionPending:{name:'start existing RestoreFleet',startedAt:at(-2500)},events:[{phase:'starting_restore',at:at(-3000)}]});
  return {adapter,ctx,persist,file};
}

test('legacy branch-revision rejection is reproduced through controller and real adapter; Recheck preserves the older partial ledger',async t=>{
  const f=fixture(t),{adapter,file}=uncommittedRecovery(f);
  f.master.restore.branchApproval=()=>({revision:'a'.repeat(64)});
  const arm=f.quota.arm;f.quota.arm=()=>{const result=arm();f.state.acknowledgedRevision=configRevision(f.state.config);return result;};
  const deps=adapter.deps(),bootstrap=deps.bootstrap;
  // Reproduce the deployed older controller allowlist, which omitted this field.
  deps.bootstrap=async ctx=>{const value=await bootstrap(ctx);delete value.branchAdoptionsRevision;return value;};
  const ledger=clone(f.master.restore.state),bytes=fs.readFileSync(file),controller=new StartupController({root:f.root,deps,now:()=>f.state.now,pollMs:1});
  const requested=controller.start({confirm:'STARTUP',requestId:'rejected-branch-map-start-001'});
  const failed=await controller.waitForIdle();
  assert.equal(failed.phase,'uncertain');assert.match(failed.error.message,/Reviewed workspace branch mapping changed/);
  assert.equal(failed.restoreStartAttempted,true);assert.equal(failed.restoreStarted,false);assert.equal(failed.restoreReceipt,null);
  assert.equal(failed.actionPending.name,'start existing RestoreFleet');
  assert.equal(failed.events.some(event=>event.phase==='restoring'),false);
  assert.equal(f.state.effects.some(effect=>effect.op==='restore.start'),false);
  assert.deepEqual(f.master.restore.state,ledger);assert.deepEqual(fs.readFileSync(file),bytes);
  const effects=clone(f.state.effects),rechecked=await controller.reconcile({confirm:'RECHECK STARTUP',runId:requested.run.runId});
  assert.equal(rechecked.run.phase,'resumable');assert.equal(rechecked.run.restore.restoredCount,1);assert.equal(rechecked.run.restore.verifiedStoppedCount,2);
  assert.equal(rechecked.run.reconciliation.restoreBinding.kind,'uncommitted-start');
  assert.equal(rechecked.run.reconciliation.restoreBinding.legacyPriorOwner,true);
  assert.equal(rechecked.run.reconciliation.restoreBinding.startNotCommitted,true);
  assert.equal(rechecked.run.reconciliation.previousAction.name,'start existing RestoreFleet');
  assert.equal(rechecked.run.restoreStarted,false,'Historical precommit failure is retained');
  assert.deepEqual(f.master.restore.state,ledger);assert.deepEqual(fs.readFileSync(file),bytes);assert.deepEqual(f.state.effects,effects);
  assert.equal(controller.start({confirm:'STARTUP',requestId:'rejected-branch-map-start-001'}).idempotent,true);
  assert.deepEqual(f.state.effects,effects,'Recheck or retry cannot launch anything');
});

test('uncommitted-start proof binds a different known prior owner as well as the exact legacy source',async t=>{
  for(const legacy of [true,false])await t.test(legacy?'legacy source':'known prior owner',async st=>{
    const f=fixture(st),{adapter,ctx,file}=uncommittedRecovery(f,{legacy}),bytes=fs.readFileSync(file),ledger=clone(f.master.restore.state);
    const proof=await adapter.reconcile(ctx);assert.equal(proof.outcome,'resumable');assert.equal(proof.restoreBinding.kind,'uncommitted-start');
    assert.equal(proof.restoreBinding.legacyPriorOwner,legacy);assert.equal(proof.restoreBinding.priorRunId,legacy?null:'prior-committed-startup');
    assert.equal(proof.restoreBinding.durableStateVerified,true);assert.equal(proof.restoreBinding.durableStateFingerprint,proof.restoreBinding.stateFingerprint);
    assert.deepEqual(f.master.restore.state,ledger);assert.deepEqual(fs.readFileSync(file),bytes);assert.deepEqual(f.state.effects,[]);
  });
});

test('ambiguous or committed starts cannot use the uncommitted-start exception',async t=>{
  const changes={
    'same current source owner':(f,x)=>{f.master.restore.state.source.startupRunId=x.ctx.runId;x.persist();},
    'current start persisted before callback failed':(f,x)=>{f.master.restore.state.startedAt=x.ctx.previousRun.startedAt;f.master.restore.state.source.startupRunId=x.ctx.runId;x.persist();},
    'newer restore time':(f,x)=>{f.master.restore.state.startedAt=new Date(f.state.now-1000).toISOString();x.persist();},
    'disk missing':(f,x)=>fs.unlinkSync(x.file),
    'disk corrupt':(f,x)=>fs.writeFileSync(x.file,'{bad json'),
    'disk differs from memory':(f,x)=>{const value=JSON.parse(fs.readFileSync(x.file,'utf8'));value.lastError='external change';fs.writeFileSync(x.file,JSON.stringify(value));},
    'disk missing explicit inflight ledger':(f,x)=>{const value=JSON.parse(fs.readFileSync(x.file,'utf8'));delete value.inflight;fs.writeFileSync(x.file,JSON.stringify(value));},
    'disk missing explicit errors ledger':(f,x)=>{const value=JSON.parse(fs.readFileSync(x.file,'utf8'));delete value.errors;fs.writeFileSync(x.file,JSON.stringify(value));},
    'inflight healthy target':(f,x)=>{f.master.restore.state.inflight['mjobs-primary']={command:'restore'};x.persist();},
    'uncertain healthy target':(f,x)=>{f.master.restore.state.errors['mjobs-primary']={uncertain:true};x.persist();},
    'inflight archived target':(f,x)=>{f.master.restore.state.inflight['mjobs-codex-1']={command:'restore'};x.persist();},
    'wrong pending action':(f,x)=>{x.ctx.previousRun.actionPending.name='another native operation';},
    'pending action predates phase':(f,x)=>{x.ctx.previousRun.actionPending.startedAt=new Date(f.state.now-3500).toISOString();},
    'restoring event':(f,x)=>x.ctx.previousRun.events.push({phase:'restoring',at:new Date(f.state.now-2000).toISOString()}),
    'restore receipt present':(f,x)=>{x.ctx.previousRun.restoreReceipt={startedAt:f.master.restore.state.startedAt,total:3};},
    'restore accepted flag':(f,x)=>{x.ctx.previousRun.restoreStarted=true;},
    'unknown accepted flag':(f,x)=>{delete x.ctx.previousRun.restoreStarted;},
    'missing old start':(f,x)=>{delete f.master.restore.state.startedAt;x.persist();},
    'prior owner field malformed':(f,x)=>{f.master.restore.state.source.startupRunId=null;x.persist();},
    'desired targets changed':(f,x)=>{f.master.restore.state.desiredIds.pop();x.persist();},
    'source identity changed':(f,x)=>{f.master.restore.state.source.targets[1].model='other';x.persist();},
    'runtime target missing':f=>f.state.sessions.pop(),
    'unscoped archived absence':f=>{const inspector=f.master.restore.runtimeInspector;f.master.restore.runtimeInspector=projects=>inspector(projects);},
    'guard unacknowledged':f=>{f.state.acknowledgedRevision='other';},
    'native start still active':(f,x)=>{f.master.restore.state.active=true;x.persist();},
    'native tick still running':f=>{f.master.restore.running=true;},
    'launch outcome unknown':f=>{f.state.launch.requiresReconciliation=true;},
  };
  for(const [name,change]of Object.entries(changes))await t.test(name,async st=>{
    const f=fixture(st),x=uncommittedRecovery(f);change(f,x);const before=clone(f.master.restore.state);
    const proof=await x.adapter.reconcile(x.ctx);assert.equal(proof.outcome,'unknown');assert.deepEqual(f.master.restore.state,before);assert.deepEqual(f.state.effects,[]);
  });
});

test('a durable restore byte change during fresh inspection invalidates an otherwise matching old ledger',async t=>{
  const f=fixture(t),x=uncommittedRecovery(f),original=f.master.restore.runtimeInspector;
  f.master.restore.runtimeInspector=async(...args)=>{const result=await original(...args);fs.appendFileSync(x.file,'\n');return result;};
  const proof=await x.adapter.reconcile(x.ctx);assert.equal(proof.outcome,'unknown');assert.deepEqual(f.state.effects,[]);
});

test('controller independently rejects incomplete uncommitted-start binding claims',async t=>{
  const changes={
    'missing durable proof':proof=>{proof.restoreBinding.durableStateVerified=false;},
    'different disk identity':proof=>{proof.restoreBinding.durableStateFingerprint='0'.repeat(64);},
    'missing raw byte hash':proof=>{delete proof.restoreBinding.durableBytesFingerprint;},
    'current prior owner':(proof,run)=>{proof.restoreBinding.legacyPriorOwner=false;proof.restoreBinding.priorRunId=run.runId;},
    'missing legacy declaration':proof=>{proof.restoreBinding.legacyPriorOwner=false;},
    'not proven uncommitted':proof=>{proof.restoreBinding.startNotCommitted=false;},
    'old operation not older':(proof,run)=>{proof.restoreBinding.startedAt=run.startedAt;},
    'wrong kind':proof=>{proof.restoreBinding.kind='unknown';},
    'missing accounted target':proof=>{proof.stoppedTargetIds.pop();},
    'duplicate accounted target':proof=>{proof.stoppedTargetIds.push(proof.healthyTargetIds[0]);},
  };
  for(const [name,change]of Object.entries(changes))await t.test(name,async st=>{
    const f=fixture(st),x=uncommittedRecovery(f),previous={...x.ctx.previousRun,revision:'uncommitted-test-revision',ownerId:'old-owner',active:false,reconciliationRequired:true,requestId:'uncommitted-proof-test-001',recentRequests:[]};
    const file=path.join(f.root,'startup-state.json');fs.writeFileSync(file,JSON.stringify(previous));const bytes=fs.readFileSync(file);
    const deps=x.adapter.deps(),inspect=deps.reconcile;deps.reconcile=async ctx=>{const proof=await inspect(ctx);assert.equal(proof.outcome,'resumable');change(proof,previous);return proof;};
    const controller=new StartupController({root:f.root,deps,now:()=>f.state.now,pollMs:1});
    const result=await controller.reconcile({confirm:'RECHECK STARTUP',runId:previous.runId});
    assert.equal(result.reconciled,false);assert.deepEqual(fs.readFileSync(file),bytes);assert.deepEqual(f.state.effects,[]);
  });
});

test('real adapter/controller reconciliation can account for scoped archived TUI targets without clearing native ledgers or launching', async t => {
  const f=fixture(t),{adapter,ctx}=archivedPartialRecovery(f);
  const previous={...ctx.previousRun,revision:'archived-partial-1',ownerId:'prior-owner',active:false,reconciliationRequired:true,
    requestId:'archived-partial-request-001',recentRequests:[{runId:ctx.runId,requestId:'archived-partial-request-001'}]};
  fs.writeFileSync(path.join(f.root,'startup-state.json'),JSON.stringify(previous));
  const controller=new StartupController({root:f.root,deps:adapter.deps(),now:()=>f.state.now,pollMs:1});
  const ledger=clone(f.master.restore.state),beforeCounts=[...f.state.counts];
  const result=await controller.reconcile({confirm:'RECHECK STARTUP',runId:ctx.runId});
  assert.equal(result.run.phase,'resumable');assert.equal(result.run.restore.restoredCount,1);assert.equal(result.run.restore.verifiedStoppedCount,2);
  assert.deepEqual(result.run.reconciliation.stoppedTargetIds,['mjobs-codex-1','mjobs-agy-1']);
  assert.deepEqual(f.master.restore.state,ledger);assert.deepEqual([...f.state.counts],beforeCounts);assert.deepEqual(f.state.effects,[]);
  assert.equal(controller.start({confirm:'STARTUP',requestId:previous.requestId}).idempotent,true);
  assert.deepEqual(f.state.effects,[],'Recheck and response retries cannot submit restoration');
});

test('archived partial reconciliation preserves uncertainty for unscoped proof, changed identities or unresolved submissions', async t => {
  const changes={
    'no explicit proof scope':f=>{const inspector=f.master.restore.runtimeInspector;f.master.restore.runtimeInspector=projects=>inspector(projects);},
    'changed live branch':f=>{f.state.sessions[1].branch='different/branch';},
    'changed source branch':f=>{f.master.restore.state.source.targets[1].branch='different/branch';},
    'native submission still inflight':f=>{f.master.restore.state.inflight['mjobs-codex-1']={command:'restore',at:new Date(f.state.now).toISOString()};},
    'uncertain native outcome':f=>{f.master.restore.state.errors['mjobs-codex-1'].uncertain=true;},
    'identity newer than preservation':f=>{f.state.sessions[1].createdAt=new Date(f.state.now-1).toISOString();},
  };
  for(const [name,change]of Object.entries(changes))await t.test(name,async st=>{
    const f=fixture(st),{adapter,ctx}=archivedPartialRecovery(f);change(f);const ledger=clone(f.master.restore.state);
    const proof=await adapter.reconcile(ctx);assert.equal(proof.outcome,'unknown');assert.deepEqual(f.master.restore.state,ledger);assert.deepEqual(f.state.effects,[]);
  });
});

test('partial reconciliation accounts for healthy and exactly stopped targets without changing the restore ledger', async t => {
  for (const evidence of ['native absence', 'explicit zero']) await t.test(evidence, async st => {
    const f = fixture(st), { adapter, ctx } = partialRecovery(f);
    if (evidence === 'explicit zero') { f.state.stoppedEvidence.clear(); for (const s of f.state.sessions.slice(1)) f.state.counts.set(s.id, 0); }
    const ledger = clone(f.master.restore.state), proof = await adapter.reconcile(ctx);
    assert.equal(proof.outcome, 'resumable'); assert.equal(proof.resumeAuditComplete, true);
    assert.deepEqual(proof.healthyTargetIds, ['mjobs-primary']);
    assert.deepEqual(proof.stoppedTargetIds, ['mjobs-codex-1', 'mjobs-agy-1']);
    assert.equal(proof.verifiedTargetCount, 1); assert.equal(proof.verifiedStoppedCount, 2);
    assert.equal(proof.restoreBinding.verified, true); assert.equal(proof.restoreBinding.runId, ctx.runId);
    assert.deepEqual(proof.durableSubmittedIds, []); assert.deepEqual(proof.unresolvedSubmittedIds, []);
    assert.deepEqual(f.master.restore.state, ledger); assert.deepEqual(f.state.effects, []);
  });
});

test('partial recovery rejects missing evidence, uncertain submissions, changed targets and unbound operations', async t => {
  const cases = {
    'missing stopped proof': f => f.state.stoppedEvidence.delete('mjobs-codex-1'),
    'stale stopped proof': f => { f.state.stoppedEvidence.get('mjobs-codex-1').observedAt = new Date(f.state.now - 10001).toISOString(); },
    'changed native generation': f => { f.state.sessions[1].terminalGeneration = 'new-unverified-generation'; },
    'positive count contradicts absence': f => f.state.counts.set('mjobs-codex-1', 2),
    'zero with active status': f => { f.state.counts.set('mjobs-codex-1', 0); f.state.sessions[1].activity.state = 'active'; },
    'zero with missing identity': f => { f.state.counts.set('mjobs-codex-1', 0); delete f.state.sessions[1].createdAt; },
    'zero with terminated session': f => { f.state.counts.set('mjobs-codex-1', 0); f.state.sessions[1].isTerminated = true; },
    'unknown runtime': f => { f.state.sessions.pop(); },
    'inflight stopped target': f => { f.master.restore.state.inflight['mjobs-codex-1'] = { command: 'resume-agent' }; },
    'inflight healthy target': f => { f.master.restore.state.inflight['mjobs-primary'] = { command: 'resume-agent' }; },
    'uncertain healthy target': f => { f.master.restore.state.errors['mjobs-primary'] = { uncertain: true }; },
    'uncertain outside target': f => { f.master.restore.state.errors['another-session'] = { uncertain: true }; },
    'malformed durable ledger': f => { f.master.restore.state.inflight = []; },
    'restore still active': f => { f.master.restore.state.active = true; },
    'native tick still running': f => { f.master.restore.running = true; },
    'launch uncertain': f => { f.state.launch.requiresReconciliation = true; },
    'dispatch on': f => { f.master.sustain.config.enabled = true; },
    'drain enabled': f => { f.master.drainState.enabled = true; },
    'emergency engaged': f => f.writeLatch(true),
    'stale quota': f => { f.state.quota.fresh = false; },
    'guard unacknowledged': f => { f.state.acknowledgedRevision = 'old'; },
    'guard disabled': f => { f.state.config.enabled = false; },
    'quota reserve reached': f => { f.state.quota.selectedRemainingPercent = 15; },
    'source model differs': f => { f.master.restore.state.source.targets[1].model = 'different'; },
    'live model differs': f => { f.state.sessions[1].model = 'different'; },
    'live branch differs': f => { f.state.sessions[1].branch = 'different-branch'; },
    'source mode differs': f => { f.master.restore.state.source.targets[1].mode = 'chat'; },
    'source target missing': f => { f.master.restore.state.source.targets.pop(); },
    'source snapshot differs': f => { f.master.restore.state.source.snapshotAt = 'different-snapshot'; },
    'desired ID duplicate': f => { f.master.restore.state.desiredIds[2] = f.master.restore.state.desiredIds[1]; },
    'manifest changed': f => { f.state.saved.sessions[1].branch = 'another-branch'; },
    'different restore receipt': (f, ctx) => { ctx.previousRun.restoreReceipt = { startedAt: new Date(f.state.now - 8001).toISOString(), total: 3 }; },
    'restore before this run': f => { f.master.restore.state.startedAt = new Date(f.state.now - 20000).toISOString(); },
    'restore after this run': f => { f.master.restore.state.startedAt = new Date(f.state.now).toISOString(); },
    'restore was not acknowledged': (f, ctx) => { ctx.previousRun.restoreStarted = false; },
    'unknown pending action': (f, ctx) => { ctx.previousRun.actionPending = { name: 'restore start' }; },
    'missing restore phase event': (f, ctx) => { ctx.previousRun.events.pop(); },
    'malformed phase events': (f, ctx) => { ctx.previousRun.events = {}; },
    'different prior run': (f, ctx) => { ctx.previousRun.runId = 'another-run'; },
    'failed source status unclear': f => { f.master.restore.state.phase = 'idle'; },
    'conflicting primary': f => { f.state.sessions.push({ ...f.state.sessions[0], id: 'other-primary' }); },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st), { adapter, ctx } = partialRecovery(f); alter(f, ctx);
    const ledger = clone(f.master.restore.state), proof = await adapter.reconcile(ctx);
    assert.equal(proof.outcome, 'unknown'); assert.deepEqual(f.master.restore.state, ledger); assert.deepEqual(f.state.effects, []);
  });
});

test('restore state changing during a partial recheck cannot authorize continuation', async t => {
  const f = fixture(t), { adapter, ctx } = partialRecovery(f), original = f.master.restore.runtimeInspector;
  f.master.restore.runtimeInspector = async projects => { const counts = await original(projects); f.master.restore.state.lastTick = new Date(f.state.now).toISOString(); return counts; };
  const proof = await adapter.reconcile(ctx); assert.equal(proof.outcome, 'unknown'); assert.deepEqual(f.state.effects, []);
});

test('real adapter and Startup controller preserve partial evidence and require a new explicit start', async t => {
  const f = fixture(t), { adapter, ctx } = partialRecovery(f), file = path.join(f.root, 'startup-state.json');
  const previous = { ...ctx.previousRun, revision: 'partial-state-1', ownerId: 'prior-owner', active: false,
    reconciliationRequired: true, requestId: 'partial-request-001', recentRequests: [{ runId: ctx.runId, requestId: 'partial-request-001' }] };
  fs.writeFileSync(file, JSON.stringify(previous));
  const controller = new StartupController({ root: f.root, deps: adapter.deps(), now: () => f.state.now, pollMs: 1 });
  const ledger = clone(f.master.restore.state);
  const result = await controller.reconcile({ confirm: 'RECHECK STARTUP', runId: ctx.runId });
  assert.equal(result.run.phase, 'resumable'); assert.equal(result.run.reconciliationRequired, false);
  assert.equal(result.run.restore.restoredCount, 1); assert.equal(result.run.restore.verifiedStoppedCount, 2);
  assert.deepEqual(result.run.reconciliation.stoppedTargetIds, ['mjobs-codex-1', 'mjobs-agy-1']);
  assert.deepEqual(f.master.restore.state, ledger); assert.deepEqual(f.state.effects, []);
  assert.equal(controller.start({ confirm: 'STARTUP', requestId: previous.requestId }).idempotent, true);
  assert.deepEqual(f.state.effects, [], 'Retrying the old request cannot launch new work');
  const arm = f.quota.arm;
  f.quota.arm = () => { const result = arm(); f.state.acknowledgedRevision = configRevision(f.state.config); return result; };
  f.master.restore.start = options => {
    f.state.effects.push({ op: 'restore.start', options: clone(options) });
    f.healthy(); f.state.now += 4000; Object.assign(f.master.restore.state, { active: false, phase: 'complete', total: 3, restoredCount: 3, startedAt: new Date(f.state.now).toISOString() });
    return clone(f.master.restore.state);
  };
  const accepted = controller.start({ confirm: 'STARTUP', requestId: 'partial-continue-002' });
  assert.notEqual(accepted.run.runId, ctx.runId); const completed = await controller.waitForIdle();
  assert.equal(completed.phase, 'ready_paused'); assert.equal(f.state.effects.filter(x => x.op === 'restore.start').length, 1);
  assert.equal(completed.restoreReceipt.total, 3); assert.equal(f.master.sustain.config.enabled, false);
  assert.ok(f.state.effects.findIndex(x => x.op === 'quota.arm') < f.state.effects.findIndex(x => x.op === 'restore.start'));
});

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

test('complete native absence can prove never-submitted idle without faking counts or readiness', async t => {
  const f = fixture(t); f.protect(); proveNativeAbsent(f); const adapter = f.make();
  const live = await adapter.readiness(f.context(), { force: true });
  assert.equal(live.processCountsComplete, false); assert.equal(live.runtimeAbsenceComplete, true);
  assert.equal(live.verified, 0); assert.equal(live.runtimesReady, false); assert.equal(live.ready, false);
  assert.deepEqual(live.nativeAbsentIds, f.state.saved.sessions.map(s => s.id));
  assert.ok([...f.state.counts.values()].every(count => count === null));
  const proof = await adapter.reconcile(inspectionContext(f, adapter));
  assert.equal(proof.outcome, 'idle'); assert.equal(proof.submissionAuditComplete, true); assert.equal(proof.submittedResume, false);
  assert.equal(proof.nativeRuntimeAbsenceVerified, true); assert.equal(proof.verifiedTargetCount, 0);
  assert.equal(proof.allExpectedRuntimesHealthy, false); assert.deepEqual(f.state.effects, []);
});

test('partial, stale, mismatched or contradicted native absence cannot prove idle', async t => {
  const cases = {
    'no sidecar': f => { f.state.stoppedEvidence.clear(); },
    'missing one target': f => { f.state.stoppedEvidence.delete('mjobs-codex-1'); },
    'wrong evidence id': f => { f.state.stoppedEvidence.get('mjobs-codex-1').identity.id = 'different-worker'; },
    'wrong evidence project': f => { f.state.stoppedEvidence.get('mjobs-codex-1').identity.projectId = 'different-project'; },
    'wrong evidence mode': f => { f.state.stoppedEvidence.get('mjobs-codex-1').identity.mode = 'chat'; },
    'wrong evidence harness': f => { f.state.stoppedEvidence.get('mjobs-codex-1').identity.harness = 'agy'; },
    'wrong session creation': f => { f.state.sessions[1].createdAt = new Date(f.state.now - 1).toISOString(); },
    'wrong terminal identity': f => { f.state.sessions[1].terminalGeneration = 'another-terminal'; },
    'live changed mode': f => { f.state.sessions[1].mode = 'chat'; },
    'live changed project': f => { f.state.sessions[1].projectId = 'different-project'; },
    'duplicate session id': f => { f.state.sessions.push(clone(f.state.sessions[1])); },
    'stale proof': f => { f.state.now += 10001; },
    'future proof': f => { f.state.stoppedEvidence.get('mjobs-codex-1').observedAt = new Date(f.state.now + 1).toISOString(); },
    'unstable registry': f => { f.state.stoppedEvidence.get('mjobs-codex-1').proof.stableRegistry = false; },
    'registry target present': f => { f.state.stoppedEvidence.get('mjobs-codex-1').proof.registryAbsent = false; },
    'provider still owned': f => { f.state.stoppedEvidence.get('mjobs-codex-1').proof.noSessionOwnedProvider = false; },
    'positive count': f => { f.state.counts.set('mjobs-codex-1', 1); },
    'invalid count': f => { f.state.counts.set('mjobs-codex-1', -1); },
    'controller running': f => { f.state.sessions[0].controllerState = 'ready'; },
    'activity running': f => { f.state.sessions[1].activity.state = 'idle'; },
    'conflicting primary': f => { f.state.sessions.push({ ...clone(f.state.sessions[0]), id: 'conflicting-primary', activity: { state: 'idle' } }); },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); f.protect(); proveNativeAbsent(f); const adapter = f.make(), ctx = inspectionContext(f, adapter); alter(f);
    const proof = await adapter.reconcile(ctx);
    assert.equal(proof.outcome, 'unknown'); assert.equal(proof.allExpectedRuntimesHealthy, false); assert.deepEqual(f.state.effects, []);
  });
});

test('complete native absence does not erase a possible submission or weaken the never-attempted audit', async t => {
  const cases = {
    'inflight target': (f, ctx) => { f.master.restore.state.inflight = { 'mjobs-primary': { command: 'resume-agent' } }; },
    'uncertain target': (f, ctx) => { f.master.restore.state.errors = { 'mjobs-primary': { uncertain: true } }; },
    'inflight old target': (f, ctx) => { f.master.restore.state.inflight = { 'outside-manifest': { command: 'resume-agent' } }; },
    'attempted restore': (f, ctx) => { ctx.previousRun.restoreStartAttempted = true; },
    'started restore': (f, ctx) => { ctx.previousRun.restoreStarted = true; },
    'unknown attempted flag': (f, ctx) => { delete ctx.previousRun.restoreStartAttempted; },
    'pending native action': (f, ctx) => { ctx.previousRun.actionPending = { name: 'restore' }; },
    'unknown pending action': (f, ctx) => { delete ctx.previousRun.actionPending; },
    'unreadable prior state': (f, ctx) => { ctx.previousRun.error = { code: 'STATE_UNREADABLE' }; },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); f.protect(); proveNativeAbsent(f); const adapter = f.make(), ctx = inspectionContext(f, adapter); alter(f, ctx);
    const before = clone(f.master.restore.state), proof = await adapter.reconcile(ctx);
    assert.equal(proof.outcome, 'unknown'); assert.deepEqual(f.master.restore.state, before); assert.deepEqual(f.state.effects, []);
    assert.equal((await adapter.readiness(f.context(), { force: true })).ready, false);
  });
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
