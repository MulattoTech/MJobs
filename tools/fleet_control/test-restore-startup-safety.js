'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { RestoreFleet, INITIAL_STATE } = require('./restore-fleet');
const { addControllerHealth, runtimeHealthy, controllerStopped } = require('./runtime-health');
const { nativeSessionIdentity } = require('./windows-runtime-evidence');

const clone = value => structuredClone(value);
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }
function fixture(t, { primaryMode = 'chat' } = {}) {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-restore-startup-safe-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const state = {
    clock: Date.now(), latched: false, block: null, commands: [], events: [], launches: 0, dispatchResumed: 0,
    exitUnconfirmed: false, resumeTimeout: false, readCount: 0, inspectCount: 0, onExit: null, onResume: null, onRead: null, onInspect: null,
    stoppedEvidence: new Map(),
    resources: { ramGiB: 30, diskGiB: 100, cpuPercent: 10 },
    live: [
      { id: 'mjobs-primary', projectId: 'mjobs-luna', kind: 'orchestrator', harness: 'codex', mode: primaryMode, model: 'test-model', activity: { state: 'idle' }, isTerminated: false },
      { id: 'mjobs-worker', projectId: 'mjobs-luna', kind: 'worker', harness: 'agy', mode: 'tui', model: 'test-model', activity: { state: 'exited' }, isTerminated: false },
    ],
    counts: new Map([['mjobs-primary', 3], ['mjobs-worker', 0]]),
    controllers: new Map([['mjobs-primary', 'stopped']]),
  };
  const targets = state.live.map(s => ({ ...clone(s), activity: 'idle' }));
  fs.writeFileSync(path.join(root, 'restore-snapshot.json'), JSON.stringify({ sessions: targets, restorableCount: targets.length }));
  // Seed an active operation directly so no automatic scheduling timer is
  // created by start(). Every tick below is deliberately driven by the test.
  fs.writeFileSync(path.join(root, 'restore-state.json'), JSON.stringify({ ...clone(INITIAL_STATE), active: true,
    phase: 'preparing', startedAt: new Date(state.clock).toISOString(), desiredIds: targets.map(s => s.id),
    source: { targets }, allowDispatchAfter: false }));
  const getGlobalSessions = async () => {
    state.readCount++; if (state.onRead) await state.onRead(state.readCount);
    return addControllerHealth(clone(state.live), async id => {
      const value = state.controllers.get(id); if (value instanceof Error) throw value;
      return { controller: value, messages: ['synthetic-transcript-must-not-be-retained'] };
    });
  };
  const restore = new RestoreFleet({ root, autoSchedule: false, now: () => state.clock,
    locked: () => state.latched, startBlockReason: () => state.block,
    getGlobalSessions, runtimeInspector: async () => {
      state.inspectCount++; if (state.onInspect) await state.onInspect(state.inspectCount);
      const counts = new Map(state.counts); counts.stoppedEvidence = clone(state.stoppedEvidence); return counts;
    },
    runtimeHealth: runtimeHealthy, controllerStopped,
    resourceReader: async () => clone(state.resources),
    watchdogInspector: async () => ({ active: false, watchdogPids: [] }),
    launchAO: async () => { state.launches++; }, onComplete: async () => { state.dispatchResumed++; },
    audit: (event, data) => state.events.push({ event, data }),
    command: async args => {
      state.commands.push(clone(args));
      const target = state.live.find(s => s.id === args[2]);
      assert.ok(target, 'Every native target must be preserved in the manifest');
      if (args[1] === 'exit-agent') {
        if (state.onExit) await state.onExit();
        if (!state.exitUnconfirmed) { target.activity.state = 'exited'; state.counts.set(target.id, 0); }
      } else if (args[1] === 'resume-agent' || args[1] === 'restore') {
        if (state.onResume) await state.onResume();
        if (state.resumeTimeout) { const error = Error('Synthetic native timeout after submit'); error.code = 'ETIMEDOUT'; throw error; }
        target.activity.state = 'idle'; target.isTerminated = false; state.counts.set(target.id, 2);
        if (target.mode === 'chat') state.controllers.set(target.id, 'starting');
      } else throw Error('Unexpected native operation in test: ' + args[1]);
      return '{}';
    },
  });
  const resumes = id => state.commands.filter(args => ['resume-agent', 'restore'].includes(args[1]) && (!id || args[2] === id));
  const readyPrimary = () => { state.live[0].activity.state = 'idle'; state.controllers.set('mjobs-primary', 'ready'); state.counts.set('mjobs-primary', 2); };
  return { root, state, restore, resumes, readyPrimary, getGlobalSessions };
}
async function proveNativeAbsent(f, id = 'mjobs-primary') {
  const session = f.state.live.find(s => s.id === id);
  Object.assign(session, { activity: { state: 'exited' }, statusReadiness: 'ready', createdAt: new Date(f.state.clock - 60000).toISOString(), updatedAt: new Date(f.state.clock).toISOString(), terminalGeneration: 'fixture-generation' });
  f.state.controllers.set(id, 'stopped'); f.state.counts.set(id, null);
  const current = (await f.getGlobalSessions()).find(s => s.id === id);
  const evidence = { version: 1, type: 'native-runtime-absent', probeId: 'a'.repeat(32), observedAt: new Date(f.state.clock).toISOString(), identity: nativeSessionIdentity(current),
    proof: { completeInventory: true, stableInventory: true, stableRegistry: true, instanceVerified: true, stableInstance: true, noMatchingHost: true, ownershipClear: true, noSessionOwnedProvider: true, registryAbsent: session.mode === 'tui', chatOwnershipClear: session.mode === 'chat' } };
  f.state.stoppedEvidence.set(id, evidence); return evidence;
}

test('native conversation controller strings map to health without retaining conversation content', async () => {
  const sessions = [
    { id: 'chat-ready', mode: 'chat' }, { id: 'chat-stopped', mode: 'chat' },
    { id: 'chat-unavailable', mode: 'chat' }, { id: 'chat-wrong-shape', mode: 'chat' },
    { id: 'tui-session', mode: 'tui' }, { id: 'terminated-chat', mode: 'chat', isTerminated: true },
  ];
  const read = [];
  const result = await addControllerHealth(sessions, async id => {
    read.push(id); if (id === 'chat-unavailable') throw Error('Unavailable');
    return { controller: id === 'chat-ready' ? 'ready' : id === 'chat-stopped' ? 'stopped' : { state: 'ready' },
      messages: ['DO_NOT_PERSIST_SYNTHETIC_CONTENT'], account: 'do-not-copy' };
  });
  assert.deepEqual(read.sort(), ['chat-ready', 'chat-stopped', 'chat-unavailable', 'chat-wrong-shape']);
  assert.equal(result[0].controllerState, 'ready'); assert.equal(result[0].controllerVerified, true);
  assert.equal(controllerStopped(result[1]), true); assert.equal(result[2].controllerVerified, false);
  assert.equal(result[3].controllerVerified, false); assert.equal(result[3].controllerState, 'unknown');
  assert.equal(JSON.stringify(result).includes('DO_NOT_PERSIST'), false); assert.equal(JSON.stringify(result).includes('do-not-copy'), false);
});

test('positive wrapper process counts alone cannot make a chat controller healthy', async t => {
  const session = { mode: 'chat', activity: { state: 'idle' }, controllerVerified: true, isTerminated: false };
  for (const state of ['ready', 'busy', 'stopped', 'starting', 'unknown', 'unavailable']) await t.test(state, () => {
    assert.equal(runtimeHealthy({ ...session, controllerState: state }, 3), ['ready', 'busy'].includes(state));
    assert.equal(controllerStopped({ ...session, controllerState: state }), state === 'stopped');
  });
  assert.equal(runtimeHealthy({ ...session, controllerState: 'ready', controllerVerified: false }, 3), false);
  assert.equal(runtimeHealthy({ ...session, controllerState: 'ready' }, undefined), false);
  assert.equal(runtimeHealthy({ ...session, controllerState: 'ready', activity: { state: 'exited' } }, 3), false);
});

test('verified stopped chat primary with a live wrapper exits/resumes once; workers wait for a ready controller', async t => {
  const f = fixture(t);
  const first = await f.restore.tick();
  assert.equal(first.phase, 'restoring_primary');
  assert.deepEqual(f.state.commands.map(args => [args[1], args[2]]), [['exit-agent', 'mjobs-primary'], ['resume-agent', 'mjobs-primary']]);
  assert.equal(f.resumes('mjobs-worker').length, 0); assert.equal(f.restore.status().restoredCount, 0);
  const waiting = await f.restore.tick(); assert.deepEqual(waiting.blockedRoots, ['mjobs-primary']);
  assert.equal(f.state.commands.length, 2); assert.equal(f.resumes('mjobs-worker').length, 0);
  f.readyPrimary(); const workers = await f.restore.tick();
  assert.equal(workers.phase, 'restoring_workers'); assert.equal(f.resumes('mjobs-primary').length, 1); assert.equal(f.resumes('mjobs-worker').length, 1);
  const complete = await f.restore.tick(); assert.equal(complete.complete, true); assert.equal(f.restore.status().restoredCount, 2);
  assert.equal(f.state.dispatchResumed, 0); assert.equal(f.state.launches, 0);
  assert.ok(f.state.commands.every(args => args[0] === 'session' && ['exit-agent', 'resume-agent'].includes(args[1])));
});

test('an unavailable chat controller with a running wrapper is not guessed stopped or resumed', async t => {
  const f = fixture(t); f.state.controllers.set('mjobs-primary', Error('Conversation unavailable'));
  const result = await f.restore.tick(); assert.deepEqual(result.blockedRoots, ['mjobs-primary']);
  assert.deepEqual(f.state.commands, []); assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true);
});

test('cancel during awaited exit withholds resume and does not report a submitted resume', async t => {
  const f = fixture(t), entered = deferred(), gate = deferred();
  f.state.onExit = async () => { entered.resolve(); await gate.promise; };
  const working = f.restore.tick(); await entered.promise; f.restore.pause(); gate.resolve();
  const result = await working;
  assert.deepEqual(f.state.commands.map(x => x[1]), ['exit-agent']); assert.equal(f.resumes().length, 0);
  assert.equal(f.restore.state.active, false); assert.equal(f.restore.state.phase, 'paused');
  assert.equal(f.restore.state.inflight['mjobs-primary'], undefined); assert.deepEqual(result.submitted || [], []);
});

test('emergency or quota stop during awaited exit wins before any resume', async t => {
  for (const reason of ['emergency', 'quota']) await t.test(reason, async st => {
    const f = fixture(st), entered = deferred(), gate = deferred();
    f.state.onExit = async () => { entered.resolve(); await gate.promise; };
    const working = f.restore.tick(); await entered.promise;
    if (reason === 'emergency') f.state.latched = true; else f.state.block = 'Quota guard lost fresh acknowledgment';
    gate.resolve(); const result = await working; await f.restore.tick();
    assert.deepEqual(f.state.commands.map(x => x[1]), ['exit-agent']); assert.equal(f.resumes().length, 0); assert.deepEqual(result.submitted || [], []);
  });
});

test('unconfirmed original exit withholds resume and never repeats the uncertain native exit', async t => {
  const f = fixture(t); f.state.exitUnconfirmed = true;
  const first = await f.restore.tick(); assert.equal(f.resumes().length, 0); assert.deepEqual(first.submitted || [], []);
  assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true); assert.ok(f.restore.state.inflight['mjobs-primary']);
  f.state.clock += 40000; await f.restore.tick();
  assert.deepEqual(f.state.commands.map(x => x[1]), ['exit-agent']); assert.equal(f.resumes('mjobs-worker').length, 0);
  assert.ok(f.restore.state.inflight['mjobs-primary']);
});

test('failed fresh post-exit inspection never falls through to resume', async t => {
  const f = fixture(t); f.state.onRead = async () => { if (f.state.commands.some(args => args[1] === 'exit-agent')) throw Error('Fresh status unavailable after exit'); };
  await f.restore.tick(); assert.deepEqual(f.state.commands.map(x => x[1]), ['exit-agent']);
  assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true); assert.equal(f.state.launches, 0);
});

test('timed-out resume remains tracked after 40 seconds and is never automatically resent', async t => {
  const f = fixture(t); f.state.resumeTimeout = true;
  await f.restore.tick(); assert.equal(f.resumes('mjobs-primary').length, 1);
  const original = clone(f.restore.state.inflight['mjobs-primary']);
  f.state.clock += 40000; const waiting = await f.restore.tick();
  assert.equal(f.resumes('mjobs-primary').length, 1); assert.equal(f.resumes('mjobs-worker').length, 0);
  assert.deepEqual(f.restore.state.inflight['mjobs-primary'], original); assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true);
  assert.deepEqual(waiting.blockedRoots, ['mjobs-primary']);
  // Late positive health can finish verification without sending another resume.
  f.state.resumeTimeout = false; f.readyPrimary(); await f.restore.tick();
  assert.equal(f.resumes('mjobs-primary').length, 1); assert.equal(f.resumes('mjobs-worker').length, 1);
  assert.equal(f.restore.state.inflight['mjobs-primary'], undefined);
});

test('accepted but unconfirmed resume also remains single-submit beyond 40 seconds', async t => {
  const f = fixture(t); await f.restore.tick(); f.state.clock += 40000;
  await f.restore.tick(); await f.restore.tick();
  assert.equal(f.resumes('mjobs-primary').length, 1); assert.equal(f.resumes('mjobs-worker').length, 0);
  assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true); assert.ok(f.restore.state.inflight['mjobs-primary']);
});

test('concurrent restore ticks cannot both exit or resume the same stopped primary', async t => {
  const f = fixture(t), entered = deferred(), gate = deferred();
  f.state.onExit = async () => { entered.resolve(); await gate.promise; };
  const first = f.restore.tick(); await entered.promise;
  assert.equal((await f.restore.tick()).skipped, 'inactive or busy'); gate.resolve(); await first;
  assert.deepEqual(f.state.commands.map(x => x[1]), ['exit-agent', 'resume-agent']);
});

test('missing or invalid runtime count is unknown, never permission to resume a TUI root', async t => {
  for (const count of [undefined, null, -1, 0.5, NaN]) await t.test(String(count), async st => {
    const f = fixture(st, { primaryMode: 'tui' });
    if (count === undefined) f.state.counts.delete('mjobs-primary'); else f.state.counts.set('mjobs-primary', count);
    await f.restore.tick(); assert.deepEqual(f.state.commands, []); assert.equal(f.resumes().length, 0);
  });
});

test('fresh native absence permits one existing-identity resume without inventing a numeric count or healthy runtime', async t => {
  for (const primaryMode of ['chat', 'tui']) await t.test(primaryMode, async st => {
    const f = fixture(st, { primaryMode }); await proveNativeAbsent(f);
    f.state.onResume = async () => { assert.equal(f.state.counts.get('mjobs-primary'), null); };
    const result = await f.restore.tick();
    assert.deepEqual(result.submitted, ['mjobs-primary']);
    assert.deepEqual(f.state.commands.map(args => args[1]), ['resume-agent']);
    assert.equal(f.restore.status().restoredCount, 0); assert.equal(f.resumes('mjobs-worker').length, 0);
    await f.restore.tick(); assert.equal(f.resumes('mjobs-primary').length, 1);
  });
});

test('native absence is rejected for wrong identity, expired proof, incomplete ownership or contradictory counts', async t => {
  const cases = {
    'wrong session id': (f, e) => { e.identity.id = 'different-session'; },
    'wrong project': (f, e) => { e.identity.projectId = 'other-project'; },
    'wrong mode': (f, e) => { e.identity.mode = 'tui'; },
    'wrong harness': (f, e) => { e.identity.harness = 'agy'; },
    'wrong creation identity': (f, e) => { e.identity.createdAt = new Date(f.state.clock - 1000).toISOString(); },
    'updated session': (f, e) => { e.identity.updatedAt = new Date(f.state.clock - 1).toISOString(); },
    'wrong terminal generation': (f, e) => { e.identity.terminalGeneration = 'other-generation'; },
    'wrong activity': (f, e) => { e.identity.activity = 'idle'; },
    'wrong termination state': (f, e) => { e.identity.isTerminated = true; },
    'unready status': (f, e) => { e.identity.statusReadiness = 'unknown'; },
    'unverified controller': (f, e) => { e.identity.controllerVerified = false; },
    'running controller': (f, e) => { e.identity.controllerState = 'ready'; },
    'stale evidence': (f, e) => { e.observedAt = new Date(f.state.clock - 10001).toISOString(); },
    'future evidence': (f, e) => { e.observedAt = new Date(f.state.clock + 1).toISOString(); },
    'incomplete inventory': (f, e) => { e.proof.completeInventory = false; },
    'unowned processes': (f, e) => { e.proof.ownershipClear = false; },
    'owned provider remains': (f, e) => { e.proof.noSessionOwnedProvider = false; },
    'positive process count': f => { f.state.counts.set('mjobs-primary', 2); },
    'invalid process count': f => { f.state.counts.set('mjobs-primary', -1); },
    'fractional process count': f => { f.state.counts.set('mjobs-primary', 0.5); },
  };
  for (const [name, alter] of Object.entries(cases)) await t.test(name, async st => {
    const f = fixture(st); const evidence = await proveNativeAbsent(f); alter(f, evidence);
    await f.restore.tick(); assert.deepEqual(f.state.commands, []); assert.equal(f.restore.status().restoredCount, 0);
  });
});

test('a valid native proof for a different preserved identity cannot authorize a restore', async t => {
  for (const key of ['id', 'projectId', 'mode', 'harness']) await t.test(key, async st => {
    const f = fixture(st); await proveNativeAbsent(f);
    f.restore.state.source.targets[0][key] = key === 'mode' ? 'tui' : 'different-value';
    await f.restore.tick(); assert.deepEqual(f.state.commands, []);
  });
});

test('mutation-boundary inspection withholds resume if absence becomes unknown, running, stale or changes identity', async t => {
  const changes = {
    unknown: f => { f.state.stoppedEvidence.clear(); },
    running: f => { f.readyPrimary(); },
    stale: f => { f.state.clock += 10001; },
    'changed project': f => { f.state.live[0].projectId = 'other-project'; },
    'changed mode': f => { f.state.live[0].mode = 'tui'; },
    'changed creation identity': f => { f.state.live[0].createdAt = new Date(f.state.clock - 1).toISOString(); },
    'new primary': f => { f.state.live.push({ ...clone(f.state.live[0]), id: 'other-primary', activity: { state: 'idle' } }); },
  };
  for (const [name, alter] of Object.entries(changes)) await t.test(name, async st => {
    const f = fixture(st); await proveNativeAbsent(f);
    f.state.onInspect = async count => { if (count === 2) alter(f); };
    await f.restore.tick(); assert.deepEqual(f.state.commands, []); assert.deepEqual(f.restore.state.inflight, {});
    assert.notEqual(f.restore.state.errors['mjobs-primary']?.uncertain, true);
  });
});

test('unknown or positive counts after a submitted exit retain an uncertain ledger and never fall through to resume', async t => {
  for (const count of [null, undefined, 1]) await t.test(String(count), async st => {
    const f = fixture(st);
    f.state.onInspect = async number => { if (number === 3) f.state.counts.set('mjobs-primary', count); };
    await f.restore.tick(); assert.deepEqual(f.state.commands.map(args => args[1]), ['exit-agent']);
    assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true); assert.ok(f.restore.state.inflight['mjobs-primary']);
    f.state.clock += 40000; await proveNativeAbsent(f); await f.restore.tick();
    assert.deepEqual(f.state.commands.map(args => args[1]), ['exit-agent']); assert.ok(f.restore.state.inflight['mjobs-primary']);
  });
});

test('a freshly regenerated proof cannot transfer a pending job to a changed native session', async t => {
  for (const key of ['createdAt', 'updatedAt', 'terminalGeneration']) await t.test(key, async st => {
    const f = fixture(st); await proveNativeAbsent(f);
    f.state.onInspect = async number => {
      if (number !== 2) return;
      f.state.live[0][key] = key === 'terminalGeneration' ? 'new-generation' : new Date(f.state.clock - 1).toISOString();
      const fresh = (await f.getGlobalSessions()).find(s => s.id === 'mjobs-primary');
      f.state.stoppedEvidence.get('mjobs-primary').identity = nativeSessionIdentity(fresh);
    };
    await f.restore.tick(); assert.deepEqual(f.state.commands, []); assert.deepEqual(f.restore.state.inflight, {});
  });
});

test('an exit followed by a different native creation identity cannot resume the replacement', async t => {
  const f = fixture(t); f.state.live[0].createdAt = new Date(f.state.clock - 60000).toISOString();
  f.state.onInspect = async number => { if (number === 3) f.state.live[0].createdAt = new Date(f.state.clock).toISOString(); };
  await f.restore.tick(); assert.deepEqual(f.state.commands.map(args => args[1]), ['exit-agent']);
  assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true); assert.ok(f.restore.state.inflight['mjobs-primary']);
});

test('fresh absence never clears uncertain or inflight outcomes across polling and restart', async t => {
  for (const ledger of ['accepted resume', 'uncertain error']) await t.test(ledger, async st => {
    const f = fixture(st); await proveNativeAbsent(f);
    if (ledger === 'accepted resume') { await f.restore.tick(); f.state.clock += 40000; await proveNativeAbsent(f); }
    else f.restore.state.errors['mjobs-primary'] = { attempts: 1, uncertain: true, message: 'Previous result unverified' };
    f.restore.save(); const before = clone(f.restore.state.inflight), submitted = f.resumes('mjobs-primary').length;
    const original = f.restore;
    const restarted = new RestoreFleet({ root: f.root, autoSchedule: false, now: () => f.state.clock,
      getGlobalSessions: f.getGlobalSessions, runtimeInspector: original.runtimeInspector, runtimeHealth: runtimeHealthy, controllerStopped,
      command: original.command, audit: original.audit, resourceReader: original.resourceReader, watchdogInspector: original.watchdogInspector });
    await restarted.tick(); await restarted.tick();
    assert.equal(f.resumes('mjobs-primary').length, submitted); assert.deepEqual(restarted.state.inflight, before);
    assert.equal(restarted.state.errors['mjobs-primary'].uncertain, true); assert.equal(restarted.status().restoredCount, 0);
    restarted.pause(); assert.throws(() => restarted.start(), /unverified/);
  });
});

test('a primary conflict preserves an existing uncertain submission flag', async t => {
  const f = fixture(t); f.restore.state.errors['mjobs-primary'] = { attempts: 2, uncertain: true, message: 'Previous native outcome unknown' };
  f.state.live.push({ ...clone(f.state.live[0]), id: 'conflicting-primary' });
  await f.restore.tick(); assert.deepEqual(f.state.commands, []);
  assert.equal(f.restore.state.errors['mjobs-primary'].uncertain, true); assert.equal(f.restore.state.errors['mjobs-primary'].attempts, 2);
});

test('a later restore tick also fails closed when resource evidence is unavailable', async t => {
  for (const [field, value] of [['ramGiB', NaN], ['ramGiB', null], ['diskGiB', null], ['diskGiB', undefined], ['cpuPercent', NaN], ['cpuPercent', undefined]]) await t.test(field + '=' + String(value), async st => {
    const f = fixture(st); f.state.resources[field] = value;
    await f.restore.tick(); assert.deepEqual(f.state.commands, []); assert.equal(f.resumes().length, 0);
  });
});

test('fresh Restore cannot erase a paused timed-out resume ledger or replay it', async t => {
  const f = fixture(t); f.state.resumeTimeout = true; await f.restore.tick(); f.restore.pause();
  const previous = clone(f.restore.state), before = fs.readFileSync(path.join(f.root, 'restore-state.json'), 'utf8');
  try { assert.throws(() => f.restore.start(), /Previous native restore outcome is unverified/); }
  finally { f.restore.pause(); }
  assert.deepEqual(f.restore.state, previous); assert.equal(fs.readFileSync(path.join(f.root, 'restore-state.json'), 'utf8'), before);
  assert.equal(f.resumes('mjobs-primary').length, 1); assert.equal(f.resumes('mjobs-worker').length, 0);
});

test('uncertain error alone blocks fresh Restore even if an inflight entry is absent', t => {
  const f = fixture(t); f.restore.state.active = false;
  f.restore.state.errors['mjobs-primary'] = { uncertain: true, message: 'Native outcome unverified', attempts: 1 };
  const previous = clone(f.restore.state);
  try { assert.throws(() => f.restore.start(), /unverified/); }
  finally { f.restore.pause(); }
  assert.deepEqual(f.restore.state, previous); assert.deepEqual(f.state.commands, []);
});

test('a cancelled but still-running native restore tick cannot be replaced by a new state', t => {
  const f = fixture(t); f.restore.state.active = false; f.restore.running = true;
  const previous = clone(f.restore.state);
  try { assert.throws(() => f.restore.start(), /still running/); }
  finally { f.restore.running = false; f.restore.pause(); }
  assert.deepEqual(f.restore.state, previous); assert.deepEqual(f.state.commands, []);
});
