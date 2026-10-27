'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const os = require('node:os');
const { createRequire } = require('node:module');

const serverPath = path.join(__dirname, 'server.js');
const nativeRequire = createRequire(serverPath);
const serverSource = fs.readFileSync(serverPath, 'utf8');
const initMarker = "if(!fs.existsSync(AO)){";
assert.ok(serverSource.includes(initMarker), 'Locate the pre-initialization source boundary explicitly');
const clone = value => structuredClone(value);
function deferred() { let resolve; const promise = new Promise(r => { resolve = r; }); return { promise, resolve }; }

// Evaluate the real route and command implementations, not the server's bottom
// initialization. All local integrations and native execution are mocked. No
// listener, timer scheduler, network request, or child process is created.
function fixture() {
  const b = {
    effects: [], native: [], audit: [], locked: false, guardReason: null,
    run: { runId: 'startup-run-001', phase: 'idle', active: false, reconciliationRequired: false },
    snapshot: { sessions: [{ id: 'worker-001', kind: 'worker', activity: 'idle' }], offline: false },
    startupCount: 0, readiness: { ready: false, reason: 'Daemon offline' },
    onRestore: null, onLaunch: null, onReconcile: null, onReviewedCompletion: null,
  };
  const makeMode = name => ({
    running: false, config: { enabled: false },
    enable(value) { this.config.enabled = value; b.effects.push({ op: name + '.enable', value }); return clone(this.config); },
    update(value) { b.effects.push({ op: name + '.update', value }); return value; },
    summary() { return { enabled: this.config.enabled }; },
    configuration() { return this.config; },
    async tick() { b.effects.push({ op: name + '.tick' }); return {}; },
    async cycle() { b.effects.push({ op: name + '.cycle' }); return {}; },
    async allHands() { b.effects.push({ op: name + '.allHands' }); return { note: 'Mock burst' }; },
  });
  b.sustain = makeMode('sustain'); b.turbo = makeMode('turbo');
  b.master = {
    locked: () => b.locked, drainState: { enabled: false },
    restore: { state: { active: false, phase: 'idle', inflight: {} }, running: false,
      pause() { b.effects.push({ op: 'restore.pause' }); this.state.active = false; return {}; } },
    disableAutomaticDispatch() { b.effects.push({ op: 'hold' }); b.sustain.config.enabled = false; b.turbo.config.enabled = false; },
    status() { return { emergency: { latched: b.locked }, restore: clone(this.restore.state), drain: clone(this.drainState) }; },
    async restoreStart(options) { if (b.onRestore) await b.onRestore(); b.effects.push({ op: 'advanced-restore.submit', options }); this.restore.state.active = true; return this.status(); },
    restorePause() { return this.restore.pause(); },
    pause() { b.effects.push({ op: 'master.pause' }); return this.status(); },
    async drain() { b.effects.push({ op: 'master.drain' }); return this.status(); },
    async emergency() { b.effects.push({ op: 'emergency' }); b.locked = true; return this.status(); },
    rearm() { b.effects.push({ op: 'rearm' }); b.locked = false; return this.status(); },
    cancelDrain() { b.effects.push({ op: 'cancel-drain' }); return this.status(); },
  };
  b.quota = {
    dispatchBlockReason: () => b.guardReason,
    status: () => ({ fresh: true, config: { autoKillArmed: true, enabled: true, thresholdPercent: 7 }, selectedRemainingPercent: 100 }),
    update: data => { b.effects.push({ op: 'quota.update', data }); return {}; },
    arm: () => { b.effects.push({ op: 'quota.arm' }); return {}; },
    disarm: () => { b.effects.push({ op: 'quota.disarm' }); return {}; },
  };
  b.startup = {
    status: () => clone(b.run),
    start(data) {
      if (data.confirm !== 'STARTUP') throw Error('Exact STARTUP confirmation required');
      if (!b.run.active) { b.startupCount++; b.run.active = true; b.run.phase = 'validating'; }
      return { statusCode: 202, run: clone(b.run) };
    },
    cancel(data) {
      if (data.runId !== b.run.runId) throw Error('Wrong run');
      const idempotent = !b.run.active;
      if (!idempotent) { b.effects.push({ op: 'startup.cancel' }); b.run.active = false; b.run.phase = 'cancelled'; }
      return { statusCode: 202, idempotent, run: clone(b.run) };
    },
    async reconcile(data) {
      if (data.confirm !== 'RECHECK STARTUP') throw Error('Exact RECHECK STARTUP required');
      if (b.onReconcile) await b.onReconcile();
      return { statusCode: 200, run: clone(b.run), reconciled: true };
    },
  };
  b.startupAdapter = {
    readiness: async () => clone(b.readiness),
    manifest: () => ({ targets: [{ id: 'mjobs-primary', kind: 'orchestrator' }] }),
  };
  b.launcher = {
    status: () => ({ pending: false }),
    async launch(mode, options = {}) {
      if (b.onLaunch) await b.onLaunch(options);
      options.beforeTrigger?.(); b.effects.push({ op: 'native-ui-trigger', mode }); return { status: 'ok' };
    },
  };
  b.findWork = { running: false, async sendOne() { b.effects.push({ op: 'find-work' }); return {}; }, async sendAll() { b.effects.push({ op: 'find-work-all' }); return {}; } };
  b.agentPlans = { all: () => ({ plans: [] }), async save(session, data) { b.effects.push({ op: 'save-plan', data }); return { ok: true }; } };
  const dummy = class {};
  const modules = {
    './turbo': { Turbo: dummy }, './sustain': { Sustain: dummy }, './master-controls': { MasterControls: dummy },
    './startup-control': { StartupController: dummy }, './startup-adapter': { StartupAdapter: dummy },
    './post-review-control': { completeReviewedRestore: async data => {
      if(data.confirm!=='COMPLETE REVIEWED RESTORE')throw Error('Explicit reviewed completion required');
      if(b.onReviewedCompletion)await b.onReviewedCompletion();
      b.effects.push({op:'reviewed-completion'});return {phase:'complete',reviewCompleted:true};
    } },
    './orchestrator-launcher': { OrchestratorLauncher: dummy }, './quota-control': { QuotaControl: dummy },
    './find-work': { FindWork: dummy }, './dispatch-ledger': { DispatchLedger: dummy }, './agent-plans': { AgentPlans: dummy },
    './preflight': { preflight: () => ({ ready: false }) }, './runtime-health': { runtimeHealthy: () => false },
    './provider-catalog': { createProviderRuntime: () => ({ get: async () => ({ providers: [] }) }), validateSelection: x => x },
    './gemini-usage': { readGeminiUsage: () => ({ providers: [] }) },
    './agent-observability': nativeRequire('./agent-observability'),
    './terminal-delivery': {completeKnownPaste:async()=>{throw Error('Unexpected terminal operation in Startup fixtures');}},
  };
  const context = vm.createContext({
    bridge: b,
    require(id) {
      if (id === 'node:child_process') return { execFile(file, args, options, callback) { b.native.push({ file, args: clone(args) }); callback(null, { stdout: 'ok' }); } };
      if (id.startsWith('node:')) return nativeRequire(id);
      if (Object.hasOwn(modules, id)) return modules[id];
      throw Error('Unexpected unmocked integration import: ' + id);
    },
    __dirname, process: { env: { FLEET_DASHBOARD_PORT: '8787', APPDATA: 'C:\\Mock\\AppData' } },
    Buffer, URL, console, setTimeout, clearTimeout, structuredClone, AbortSignal,
    fetch: async () => { throw Error('Network is forbidden in integration tests'); },
  });
  vm.runInContext(serverSource.slice(0, serverSource.indexOf(initMarker)) + '\nglobalThis.subject={main,command,startupBlockReason,workBlockReason};', context);
  vm.runInContext(`
    master=bridge.master;quota=bridge.quota;sustain=bridge.sustain;turbo=bridge.turbo;findWork=bridge.findWork;
    startup=bridge.startup;startupAdapter=bridge.startupAdapter;orchestratorLauncher=bridge.launcher;agentPlans=bridge.agentPlans;
    state=async()=>bridge.snapshot;audit=(event,data)=>bridge.audit.push({event,data});
    nudge=async data=>{bridge.effects.push({op:'nudge',data});return {ok:true}};
    replace=async data=>{bridge.effects.push({op:'replace',data});return {ok:true}};
    retire=async data=>{bridge.effects.push({op:'retire',data});return {ok:true}};
    applyAgentPlan=async data=>{bridge.effects.push({op:'apply-plan',data});return {ok:true}};
  `, context);
  const post = async (url, data = {}, { headers = {}, gate = null, entered = null } = {}) => {
    let status, response;
    await context.subject.main({
      url, method: 'POST', headers: { host: '127.0.0.1:8787', 'x-ao-fleet': '1', 'content-type': 'application/json', ...headers },
      async *[Symbol.asyncIterator]() { entered?.resolve(); if (gate) await gate.promise; yield Buffer.from(JSON.stringify(data)); },
    }, { writeHead(code) { status = code; }, end(value) { response = JSON.parse(value); } });
    return { status, body: response };
  };
  const get = async url => {
    let status, response;
    await context.subject.main({ url, method: 'GET', headers: { host: '127.0.0.1:8787' } }, { writeHead(code) { status = code; }, end(value) { response = JSON.parse(value); } });
    return { status, body: response };
  };
  return { b, context, post, get, subject: context.subject, setLock: value => vm.runInContext('mutationLock=' + !!value, context), locked: () => vm.runInContext('mutationLock', context) };
}

test('reviewed completion route holds the existing mutation lane and cannot submit native work',async()=>{
 const f=fixture(),entered=deferred(),gate=deferred();
 f.b.run.phase='uncertain';f.b.run.reconciliationRequired=true;
 f.b.onReviewedCompletion=async()=>{entered.resolve();await gate.promise;};
 const pending=f.post('/api/master/restore/complete-reviewed',{confirm:'COMPLETE REVIEWED RESTORE',runId:f.b.run.runId,reviewRevision:'a'.repeat(64)});
 await entered.promise;assert.equal(f.locked(),true);
 const competing=await f.post('/api/startup',{confirm:'STARTUP',requestId:'competing-start-001'});
 assert.equal(competing.status,409);assert.equal(f.locked(),true);assert.deepEqual(f.b.native,[]);
 gate.resolve();const result=await pending;assert.equal(result.status,200);assert.equal(result.body.reviewCompleted,true);assert.equal(f.locked(),false);
 assert.deepEqual(f.b.effects,[{op:'reviewed-completion'}]);assert.deepEqual(f.b.native,[]);
});

test('Startup route is explicit, local-only, available under emergency lock, and returns asynchronous accepted state', async t => {
  for (const kind of ['bad host', 'bad origin', 'bad action header', 'no confirmation', 'accepted']) await t.test(kind, async () => {
    const f = fixture(); f.b.locked = true; const headers = {};
    if (kind === 'bad host') headers.host = 'public.example:8787';
    if (kind === 'bad origin') headers.origin = 'https://public.example';
    if (kind === 'bad action header') headers['x-ao-fleet'] = '0';
    const result = await f.post('/api/startup', { confirm: kind === 'no confirmation' ? '' : 'STARTUP', requestId: 'request-001' }, { headers });
    assert.equal(result.status, kind === 'accepted' ? 202 : kind === 'bad host' ? 403 : 409);
    assert.equal(f.b.startupCount, kind === 'accepted' ? 1 : 0); assert.deepEqual(f.b.native, []);
    assert.equal(f.locked(), false);
  });
});

test('rejected Startup and recheck requests never release a different operation lock', async t => {
  for (const route of ['/api/startup', '/api/startup/reconcile', '/api/orchestrator/open']) await t.test(route, async () => {
    const f = fixture(); f.setLock(true);
    assert.equal((await f.post(route, { confirm: 'STARTUP', requestId: 'request-001' })).status, 409);
    assert.equal(f.locked(), true); assert.deepEqual(f.b.effects, []); assert.equal(f.b.startupCount, 0);
  });
});

test('protected Startup blocks all manual dispatch, agent replacement and safety reconfiguration routes', async t => {
  for (const route of ['/api/nudge', '/api/replace', '/api/retire', '/api/agent-plan/apply', '/api/find-work', '/api/find-work/all', '/api/turbo/all-hands', '/api/turbo/toggle', '/api/sustain/toggle', '/api/quota/config', '/api/quota/arm', '/api/master/rearm', '/api/master/restore', '/api/master/restore/complete-reviewed']) await t.test(route, async () => {
    const f = fixture(); f.b.run.active = true; f.b.run.phase = 'waiting_guard_ack';
    const result = await f.post(route, { id: 'worker-001', enabled: true });
    assert.equal(result.status, 409); assert.deepEqual(f.b.effects, []); assert.deepEqual(f.b.native, []);
  });
});

test('staged plans remain configuration-only and are allowed while Startup or emergency is active', async () => {
  const f = fixture(); f.b.run.active = true; f.b.locked = true;
  const result = await f.post('/api/agent-plan', { id: 'worker-001', harness: 'agy', model: 'gemini-test' });
  assert.equal(result.status, 200); assert.deepEqual(f.b.effects.map(x => x.op), ['save-plan']); assert.deepEqual(f.b.native, []);
});

test('Cancel and emergency remain reachable while another operation owns the mutation lane', async t => {
  for (const route of ['/api/startup/cancel', '/api/master/emergency', '/api/master/pause']) await t.test(route, async () => {
    const f = fixture(); f.b.run.active = true; f.setLock(true);
    const result = await f.post(route, { runId: f.b.run.runId, confirm: 'KILL ALL CODEX' });
    assert.ok([200, 202].includes(result.status)); assert.equal(f.b.run.active, false); assert.equal(f.locked(), true);
    assert.ok(f.b.effects.some(x => x.op === 'startup.cancel')); assert.ok(f.b.effects.some(x => x.op === 'restore.pause'));
  });
});

test('emergency confirmation is checked before it can cancel a Startup', async () => {
  const f = fixture(); f.b.run.active = true;
  assert.equal((await f.post('/api/master/emergency', { confirm: 'wrong' })).status, 400);
  assert.equal(f.b.run.active, true); assert.deepEqual(f.b.effects, []);
});

test('only the authorized restore command can pass Startup during its restoring stages', async t => {
  for (const phase of ['validating', 'holding_dispatch', 'unlocking', 'waiting_watchdog', 'arming_guard', 'waiting_guard_ack', 'verifying', 'opening_ui', 'starting_restore', 'restoring']) await t.test(phase, async () => {
    const f = fixture(); Object.assign(f.b.run, { active: true, phase }); f.b.master.restore.state.active = true;
    const cmd = ['session', 'resume-agent', 'worker-001', '--project', 'mjobs'];
    const action = () => f.subject.command(cmd, 100, { startupRestore: true });
    if (['starting_restore', 'restoring'].includes(phase)) { await action(); assert.equal(f.b.native.length, 1); }
    else { await assert.rejects(action, /Startup/); assert.equal(f.b.native.length, 0); }
    await assert.rejects(() => f.subject.command(['send', '--session', 'worker-001', '--message', 'new assignment'], 100, { startupRestore: true }), /Startup/);
  });
});

test('uncertain Startup, emergency, and quota failures each block otherwise-authorized native restores', async t => {
  for (const block of ['uncertain', 'emergency', 'quota', 'inactive restore']) await t.test(block, async () => {
    const f = fixture(); Object.assign(f.b.run, { active: true, phase: 'restoring' }); f.b.master.restore.state.active = true;
    if (block === 'uncertain') f.b.run.reconciliationRequired = true;
    if (block === 'emergency') f.b.locked = true;
    if (block === 'quota') f.b.guardReason = 'Quota monitor unavailable';
    if (block === 'inactive restore') f.b.master.restore.state.active = false;
    await assert.rejects(() => f.subject.command(['session', 'resume-agent', 'worker-001'], 100, { startupRestore: true }));
    assert.deepEqual(f.b.native, []);
  });
});

test('reconciliation holds the manual lane until its read-only proof finishes', async () => {
  const f = fixture(), entered = deferred(), gate = deferred();
  f.b.onReconcile = async () => { entered.resolve(); await gate.promise; };
  const checking = f.post('/api/startup/reconcile', { confirm: 'RECHECK STARTUP', runId: f.b.run.runId }); await entered.promise;
  assert.equal(f.locked(), true); assert.equal((await f.post('/api/nudge', { id: 'worker-001' })).status, 409); assert.equal(f.locked(), true);
  gate.resolve(); assert.equal((await checking).status, 200); assert.equal(f.locked(), false); assert.deepEqual(f.b.effects, []);
});

test('partial Recheck reports resumable state without native effects and needs a separate explicit Startup request', async () => {
  const f = fixture();
  f.b.run = { ...f.b.run, phase: 'uncertain', reconciliationRequired: true };
  f.b.onReconcile = async () => {
    f.b.run = { ...f.b.run, phase: 'resumable', reconciliationRequired: false,
      reconciliation: { outcome: 'resumable', healthyTargetIds: ['mjobs-primary'], stoppedTargetIds: ['worker-001'] } };
  };
  const checked = await f.post('/api/startup/reconcile', { confirm: 'RECHECK STARTUP', runId: f.b.run.runId });
  assert.equal(checked.status, 200); assert.equal(checked.body.run.phase, 'resumable');
  assert.equal(f.b.startupCount, 0); assert.deepEqual(f.b.native, []); assert.deepEqual(f.b.effects, []);
  const status = await f.get('/api/startup');
  assert.equal(status.body.run.phase, 'resumable'); assert.equal(status.body.readiness.ready, false);
  assert.equal(f.b.startupCount, 0); assert.deepEqual(f.b.native, []);
  const continued = await f.post('/api/startup', { confirm: 'STARTUP', requestId: 'explicit-partial-continuation-002' });
  assert.equal(continued.status, 202); assert.equal(continued.body.run.phase, 'validating');
  assert.equal(f.b.startupCount, 1); assert.deepEqual(f.b.native, []);
});

test('advanced Restore owns the mutation lane across async preview, preventing a racing protected Startup', async () => {
  const f = fixture(), entered = deferred(), gate = deferred();
  f.b.onRestore = async () => { entered.resolve(); await gate.promise; };
  const restoring = f.post('/api/master/restore', { confirm: 'RESTORE FLEET' }); await entered.promise;
  const startup = await f.post('/api/startup', { confirm: 'STARTUP', requestId: 'racing-request-001' });
  gate.resolve(); await restoring;
  assert.equal(startup.status, 409); assert.equal(f.b.startupCount, 0); assert.equal(f.locked(), false);
});

test('delayed-body safety edits recheck Startup activity before mutation', async t => {
  for (const route of ['/api/quota/config', '/api/master/rearm']) await t.test(route, async () => {
    const f = fixture(), entered = deferred(), gate = deferred();
    const pending = f.post(route, { thresholdPercent: 0, confirm: 'REARM' }, { entered, gate }); await entered.promise;
    Object.assign(f.b.run, { active: true, phase: 'waiting_guard_ack' }); gate.resolve();
    const result = await pending; assert.ok([400, 409].includes(result.status)); assert.deepEqual(f.b.effects, []);
  });
});

test('separate Open Orchestrator rechecks emergency immediately before its native trigger', async () => {
  const f = fixture(), entered = deferred(), gate = deferred();
  f.b.onLaunch = async () => { entered.resolve(); await gate.promise; };
  const opening = f.post('/api/orchestrator/open', {}); await entered.promise; f.b.locked = true; gate.resolve();
  const result = await opening; assert.equal(result.status, 409); assert.deepEqual(f.b.effects, []); assert.equal(f.locked(), false);
});

test('Startup GET exposes current readiness separately from historical completion and performs no actions', async () => {
  const f = fixture(); f.b.run.phase = 'ready_paused'; f.b.readiness = { ready: false, reason: 'Primary controller stopped' };
  const response = await f.get('/api/startup'); assert.equal(response.status, 200);
  assert.equal(response.body.run.phase, 'ready_paused'); assert.equal(response.body.readiness.ready, false);
  assert.deepEqual(f.b.effects, []); assert.deepEqual(f.b.native, []);
});

test('server bootstrap explicitly pauses a persisted active restore before accepting requests', () => {
  const f = fixture(); f.b.master.restore.state.active = true; f.b.run.phase = 'uncertain'; f.b.run.reconciliationRequired = true;
  const statements = serverSource.match(/if\(master\.restore\.state\.active\)master\.restore\.pause\(\);\s*if\(startupBlockReason\(\)\)master\.disableAutomaticDispatch\(\);/);
  assert.ok(statements, 'Keep the production persisted-restore startup guard');
  vm.runInContext(statements[0], f.context); assert.equal(f.b.master.restore.state.active, false);
  assert.deepEqual(f.b.effects.map(x => x.op), ['restore.pause', 'hold']); assert.deepEqual(f.b.native, []);
});

test('restore UI does not label historical completion as currently verified while runtime health is false', async () => {
  const nodes = new Map();
  const element = id => {
    if (!nodes.has(id)) nodes.set(id, { textContent: '', hidden: false, disabled: false, style: {}, classList: { contains: () => false }, addEventListener() {}, scrollIntoView() {} });
    return nodes.get(id);
  };
  let value = { restore: { phase: 'complete', active: false, total: 3, restoredCount: 3, autoDispatchAfter: false }, currentReadiness: { ready: false, reason: 'All controllers offline' } };
  const context = vm.createContext({ document: { getElementById: element, querySelector: () => element('masterSection') }, api: async () => clone(value), toast() {}, openModal() {}, closeModal() {}, setInterval() {} });
  const source = fs.readFileSync(path.join(__dirname, 'public', 'restore-ui.js'), 'utf8');
  vm.runInContext(source + '\nglobalThis.ui=FleetRestoreUI;', context); await new Promise(resolve => setImmediate(resolve));
  const rendered = ['restorePhase', 'restoreNumbers', 'restoreCurrent'].map(id => element(id).textContent).join(' ');
  assert.match(rendered, /historical|previous|unverified|not (currently )?verified|offline|not current/i);
  assert.notEqual(element('restorePhase').textContent, 'Fleet restored');
  value = { ...value, currentReadiness: { ready: true } }; await context.ui.refresh();
  assert.match(element('restorePhase').textContent, /restored|complete/i);
});

test('real MasterControls rechecks Startup ownership after async advanced-restore preview', async t => {
  const { MasterControls } = nativeRequire('./master-controls');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-startup-master-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const effects = [], entered = deferred(), gate = deferred(); let blocked = null;
  const mode = name => ({ config: { enabled: true }, enable(value) { this.config.enabled = value; effects.push({ name, value }); } });
  const master = new MasterControls({ root, sustain: mode('sustain'), turbo: mode('turbo'),
    command: async () => { throw Error('Native commands forbidden'); }, audit() {}, getState: async () => ({ sessions: [] }),
    autoschedule: false, startBlockReason: () => blocked });
  master.quota = { status: () => ({ config: { autoKillArmed: true, tripped: false, thresholdPercent: 7 }, fresh: true, selectedRemainingPercent: 100 }), dispatchBlockReason: () => null };
  master.restore.getSnapshot = () => ({ sessions: [{ id: 'saved-session' }], restorableCount: 1 });
  master.restore.preview = async () => { entered.resolve(); await gate.promise; return { totalTargets: 1, alreadyOnline: 0, willResume: 1 }; };
  master.restore.start = () => { effects.push({ name: 'restore.start' }); return { total: 1 }; };
  const work = master.restoreStart(); await entered.promise; blocked = 'Protected Startup now owns restoration'; gate.resolve();
  await assert.rejects(work, /Protected Startup/); assert.deepEqual(effects, []);
});

test('real RestoreFleet cannot replay a persisted operation when Startup ownership is uncertain', async t => {
  const { RestoreFleet } = nativeRequire('./restore-fleet');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-startup-restore-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  fs.writeFileSync(path.join(root, 'restore-state.json'), JSON.stringify({ active: true, phase: 'restoring_workers', desiredIds: ['worker-001'] }));
  let nativeCalls = 0, probes = 0;
  const restore = new RestoreFleet({ root, autoSchedule: false, audit() {},
    command: async () => { nativeCalls++; }, launchAO: async () => { nativeCalls++; },
    getGlobalSessions: async () => { probes++; throw Error('Must not query before ownership check'); },
    watchdogInspector: async () => { probes++; throw Error('Must not query before ownership check'); },
    startBlockReason: () => 'Interrupted Startup needs reconciliation' });
  const result = await restore.tick();
  assert.match(result.paused, /reconciliation/); assert.equal(restore.state.phase, 'paused_quota');
  assert.equal(nativeCalls, 0); assert.equal(probes, 0);
});

test('paused quota recovery keeps its original manifest instead of recapturing an unrelated live fleet', async t => {
  const { RestoreFleet } = nativeRequire('./restore-fleet');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-startup-manifest-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  const manifest = { sessions: [{ id: 'original-primary', projectId: 'mjobs', kind: 'orchestrator', harness: 'codex', activity: 'idle' }], restorableCount: 1 };
  const snapshotFile = path.join(root, 'restore-snapshot.json'); fs.writeFileSync(snapshotFile, JSON.stringify(manifest));
  fs.writeFileSync(path.join(root, 'restore-state.json'), JSON.stringify({ active: false, phase: 'paused_quota', desiredIds: ['original-primary'] }));
  let queries = 0;
  const restore = new RestoreFleet({ root, autoSchedule: false, audit() {}, command: async () => { throw Error('Native commands forbidden'); },
    getGlobalSessions: async () => { queries++; return [{ id: 'unrelated-primary', projectId: 'other', kind: 'orchestrator', harness: 'codex', activity: { state: 'idle' } }]; } });
  assert.equal(await restore.captureSnapshot(), null); assert.equal(queries, 0);
  assert.deepEqual(JSON.parse(fs.readFileSync(snapshotFile, 'utf8')), manifest);
});

test('real MasterControls rejects a previous unverified native submission before preview or state changes', async t => {
  const { MasterControls } = nativeRequire('./master-controls');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'fleet-startup-master-ledger-'));
  t.after(() => fs.rmSync(root, { recursive: true, force: true }));
  let writes = 0, previews = 0;
  const mode = () => ({ config: { enabled: true }, enable() { writes++; } });
  const master = new MasterControls({ root, sustain: mode(), turbo: mode(), autoschedule: false,
    command: async () => { throw Error('Native commands forbidden'); }, audit() {}, getState: async () => ({ sessions: [] }) });
  master.restore.state.inflight = { 'old-worker': { command: 'resume-agent', at: new Date().toISOString() } };
  master.restore.preview = async () => { previews++; return {}; };
  await assert.rejects(master.restoreStart(), /unverified/); assert.equal(previews, 0); assert.equal(writes, 0);
  assert.ok(master.restore.state.inflight['old-worker']);
});

test('real Startup UI exposes explicit Recheck for a blocked prior operation without polling into a POST', async () => {
  const elements = new Map(), calls = [], gate = deferred();
  function element(id) {
    const value = { id, listeners: {}, classList: { add() {}, toggle() {} }, textContent: '', disabled: false, hidden: false,
      querySelectorAll: () => [], addEventListener(name, handler) { this.listeners[name] = handler; } };
    Object.defineProperty(value, 'innerHTML', { set(html) {
      for (const match of html.matchAll(/\bid="([^"]+)"/g)) if (!elements.has(match[1])) elements.set(match[1], element(match[1]));
    } });
    return value;
  }
  elements.set('startupSection', element('startupSection'));
  let payload = { run: { runId: 'blocked-prior-restore', phase: 'uncertain', active: false, reconciliationRequired: true, error: { code: 'BOOTSTRAP_BLOCKED', message: 'A prior manual restore is unverified' } }, readiness: { ready: false } };
  const ctx = vm.createContext({
    Date, structuredClone, console, setInterval: () => 1, clearInterval() {},
    document: { hidden: false, getElementById: id => elements.get(id) || null, addEventListener() {} },
    window: { addEventListener() {} }, sessionStorage: { getItem: () => null, setItem() {}, removeItem() {} }, toast() {},
    api: async (url, data) => {
      calls.push({ url, data: data && clone(data) });
      if (data) {
        await gate.promise;
        payload = { run: { ...payload.run, phase: 'ready_paused', reconciliationRequired: false, error: null }, readiness: { ready: true, dispatchPaused: true } };
      }
      return clone(payload);
    },
  });
  const source = fs.readFileSync(path.join(__dirname, 'public', 'startup-ui.js'), 'utf8');
  vm.runInContext(source + '\nglobalThis.subject=FleetStartupUI;', ctx);
  await ctx.subject.refresh(); await ctx.subject.refresh();
  assert.equal(elements.get('startupRecheck').hidden, false); assert.equal(elements.get('startupStart').disabled, true);
  assert.equal(elements.get('startupOpenUi').disabled, true); assert.match(elements.get('startupDetail').textContent, /never resends/);
  await elements.get('startupOpenUi').listeners.click(); assert.equal(calls.some(x => x.data), false);
  const checking = elements.get('startupRecheck').listeners.click();
  const duplicate = elements.get('startupRecheck').listeners.click();
  assert.equal(elements.get('startupRecheck').disabled, true);
  gate.resolve(); await checking; await duplicate; await ctx.subject.refresh();
  assert.deepEqual(calls.filter(x => x.data), [{ url: '/api/startup/reconcile', data: { confirm: 'RECHECK STARTUP', runId: 'blocked-prior-restore' } }]);
  assert.equal(elements.get('startupRecheck').hidden, true); assert.equal(ctx.subject.status().run.phase, 'ready_paused');
  ctx.subject.dispose();
});
