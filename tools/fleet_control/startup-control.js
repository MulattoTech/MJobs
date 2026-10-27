'use strict';

const fs = require('node:fs');
const path = require('node:path');
const { randomUUID, createHash } = require('node:crypto');

const POLICY = Object.freeze({ thresholdPercent: 7, pollSeconds: 5, window: 'any', enabled: true, brakePercent: 15 });
const DEFAULT_TIMEOUTS = Object.freeze({ readMs: 30000, actionMs: 15000, watchdogMs: 45000, guardAckMs: 25000, restoreMs: 600000, readinessMs: 90000, totalMs: 900000 });
const ACTIVE_PHASES = new Set(['validating', 'holding_dispatch', 'unlocking', 'waiting_watchdog', 'arming_guard', 'waiting_guard_ack', 'starting_restore', 'restoring', 'verifying', 'opening_ui', 'starting_dispatch']);

class StartupError extends Error {
  constructor(code, message, uncertain = false) { super(message); this.name = 'StartupError'; this.code = code; this.uncertain = uncertain; }
}
const clone = value => structuredClone(value);
const shortError = error => String(error?.message || error).slice(0, 600);

class StartupController {
  constructor({ root, deps, audit = () => {}, now = () => Date.now(), pollMs = 1000, timeouts = {} }) {
    if (!root || !path.isAbsolute(root)) throw Error('An absolute startup-state directory is required');
    for (const name of ['safety', 'bootstrap', 'holdDispatch', 'unlock', 'watchdogDisarmed', 'arm', 'guardAck', 'readiness']) {
      if (typeof deps?.[name] !== 'function') throw Error('Missing Startup dependency: ' + name);
    }
    for (const name of ['start', 'status', 'pause']) if (typeof deps?.restore?.[name] !== 'function') throw Error('Missing restore.' + name);
    if (!Number.isFinite(pollMs) || pollMs < 1 || pollMs > 10000) throw Error('pollMs must be 1..10000');
    this.root = root; this.deps = deps; this.audit = audit; this.now = now; this.pollMs = pollMs;
    this.timeouts = { ...DEFAULT_TIMEOUTS, ...timeouts };
    for (const [key, value] of Object.entries(this.timeouts)) if (!Number.isFinite(value) || value < 1) throw Error('Invalid timeout ' + key);
    fs.mkdirSync(root, { recursive: true });
    this.file = path.join(root, 'startup-state.json');
    this.ownerId = randomUUID(); this.task = null; this.abort = null; this.readError = null;
    this.pendingMutations = new Map(); this.reconcileTask = null;
    this.state = this._load();
    if (this.state.active || ACTIVE_PHASES.has(this.state.phase) || this.state.cleanup?.active === true) {
      this.state = { ...this.state, active: false, phase: 'uncertain', reconciliationRequired: true, finishedAt: this._iso(), error: { code: 'LOST_OWNER', message: 'The previous Startup owner ended before completion. Verify its operation and runtimes before another launch.' } };
      this._persist(false);
      this._log('lost_owner', { runId: this.state.runId });
    }
  }

  _iso() { return new Date(this.now()).toISOString(); }
  _initial() { return { schemaVersion: 1, revision: null, runId: null, requestId: null, ownerId: null, ownerPid: null, phase: 'idle', active: false, reconciliationRequired: false, recentRequests: [], events: [] }; }
  _load() {
    try {
      const value = JSON.parse(fs.readFileSync(this.file, 'utf8').replace(/^\uFEFF/, ''));
      if (value.schemaVersion !== 1 || typeof value.phase !== 'string' || typeof value.active !== 'boolean' || !Array.isArray(value.recentRequests)) throw Error('Invalid Startup state schema');
      return value;
    } catch (error) {
      if (error.code === 'ENOENT') return this._initial();
      this.readError = shortError(error);
      return { ...this._initial(), phase: 'uncertain', reconciliationRequired: true, error: { code: 'STATE_UNREADABLE', message: 'Startup state is unreadable. Preserve and reconcile it before launching.' } };
    }
  }
  _persist(checkOwner = true) {
    if (this.readError) throw new StartupError('STATE_UNREADABLE', 'Startup state is unreadable; refusing to overwrite it', true);
    if (checkOwner && this.state.runId && fs.existsSync(this.file)) {
      const disk = this._load();
      if (this.readError || disk.runId !== this.state.runId || disk.ownerId !== this.ownerId || disk.revision !== this.state.revision) throw new StartupError('OWNER_CHANGED', 'Startup ownership or revision changed; no more actions are allowed', true);
    }
    this.state.updatedAt = this._iso(); this.state.revision = randomUUID();
    const temp = this.file + '.' + this.ownerId + '.tmp';
    fs.writeFileSync(temp, JSON.stringify(this.state, null, 2), 'utf8');
    fs.renameSync(temp, this.file);
  }
  _log(event, details = {}) { try { this.audit('startup_' + event, details); } catch {} }
  status() { return clone(this.state); }

  start({ confirm, requestId, openUi = false, startWorkAfter = false } = {}) {
    if (confirm !== 'STARTUP') throw new StartupError('CONFIRMATION_REQUIRED', 'Exact STARTUP confirmation is required');
    if (typeof requestId !== 'string' || !/^[A-Za-z0-9._:-]{8,128}$/.test(requestId)) throw new StartupError('REQUEST_ID_REQUIRED', 'Provide a stable 8–128 character Startup request ID');
    if (typeof startWorkAfter !== 'boolean') throw new StartupError('INVALID_OPTION', 'startWorkAfter must be boolean');
    if (startWorkAfter && typeof this.deps.startDispatch !== 'function') throw new StartupError('DISPATCH_UNAVAILABLE', 'Protected start-work integration is unavailable');
    if (typeof openUi !== 'boolean') throw new StartupError('INVALID_OPTION', 'openUi must be boolean');
    if (openUi && typeof this.deps.openUi !== 'function') throw new StartupError('UI_UNAVAILABLE', 'The Orchestrator desktop opener is unavailable');
    if (!this.task && !this.readError) this.state = this._load();
    const previous = this.state.recentRequests.find(item => item.requestId === requestId);
    if (this.task || this.reconcileTask || this.state.active || previous || this.state.requestId === requestId) {
      return { statusCode: 202, idempotent: true, requestedRunId: previous?.runId || this.state.runId, run: this.status() };
    }
    if (this.pendingMutations.size) throw new StartupError('RECONCILIATION_REQUIRED', 'A previously submitted Startup operation is still in flight. Reconcile it after it settles before another launch', true);
    if (this.readError || this.state.reconciliationRequired || this.state.phase === 'uncertain') throw new StartupError('RECONCILIATION_REQUIRED', 'Previous Startup outcome is uncertain. Reconcile it before attempting another launch', true);
    const runId = randomUUID();
    this.state = {
      ...this._initial(), runId, requestId, ownerId: this.ownerId, ownerPid: process.pid, phase: 'validating', active: true,
      startedAt: this._iso(), deadlineAt: new Date(this.now() + this.timeouts.totalMs).toISOString(), finishedAt: null,
      options: { openUi, autoDispatchAfter: false, startWorkAfter }, policy: { ...POLICY },
      recentRequests: [...this.state.recentRequests, { requestId, runId }].slice(-32),
      dispatchHeld: false, latchReleased: false, initialEmergencyRevision: null,
      guardReceipt: null, guardAcknowledged: false, restoreStartAttempted: false, restoreStarted: false, restoreReceipt: null,
      actionPending: null, restore: null, cleanup: null, error: null,
    };
    this._persist(false);
    this.abort = new AbortController();
    this._log('requested', { runId, openUi });
    this.task = Promise.resolve().then(() => this._run()).catch(error => this._failure(error)).catch(error => {
      if (error.code === 'OWNER_CHANGED') {
        this.abort?.abort(); this.state = this._load();
        this._log('ownership_lost', { message: shortError(error) }); return;
      }
      // A failed state write or cleanup must not become an unhandled rejection
      // or silently make another launch eligible.
      this.abort?.abort(); this.readError = shortError(error);
      this.state = { ...this.state, active: false, phase: 'uncertain', reconciliationRequired: true,
        error: { code: 'STATE_PERSISTENCE_FAILED', message: 'Startup could not safely record its result. Reconcile state and runtimes before retrying.' } };
      this._log('persistence_failed', { runId: this.state.runId, message: shortError(error) });
    }).finally(() => { this.task = null; });
    return { statusCode: 202, idempotent: false, requestedRunId: runId, run: this.status() };
  }

  cancel({ runId } = {}) {
    if (!runId || runId !== this.state.runId) throw new StartupError('RUN_MISMATCH', 'Cancel must identify the current Startup run');
    if (!this.state.active) return { statusCode: 202, idempotent: true, run: this.status() };
    const uncertain = !!this.state.actionPending || this.state.restoreStartAttempted;
    this.state.active = false;
    this.state.phase = uncertain ? 'uncertain' : 'cancelled';
    this.state.reconciliationRequired = uncertain;
    this.state.finishedAt = this._iso();
    this.state.error = { code: 'CANCELLED', message: uncertain ? 'Startup cancelled. A possibly started operation must be reconciled; no further launches will be attempted.' : 'Startup cancelled. No further actions will be attempted.' };
    this.abort?.abort(); this._persist(); this._log('cancelled', { runId, uncertain });
    return { statusCode: 202, idempotent: false, run: this.status() };
  }

  async waitForIdle() { if (this.task) await this.task; return this.status(); }
  _assertOwned() {
    const disk = this._load();
    if (this.readError || disk.runId !== this.state.runId || disk.ownerId !== this.ownerId || disk.revision !== this.state.revision) throw new StartupError('OWNER_CHANGED', 'Startup ownership or revision was lost; no further actions are permitted', true);
    return disk;
  }
  _assertCurrent() {
    if (!this.state.active || this.abort?.signal.aborted) throw new StartupError('CANCELLED', 'Startup is no longer active', !!this.state.actionPending);
    if (this.now() >= Date.parse(this.state.deadlineAt)) throw new StartupError('TOTAL_TIMEOUT', 'Startup exceeded its overall deadline', this.state.restoreStartAttempted);
    if (!this._assertOwned().active) throw new StartupError('OWNER_CHANGED', 'Startup is no longer active on disk', true);
  }
  _transition(phase, detail = {}) {
    this._assertCurrent();
    this.state.phase = phase; Object.assign(this.state, detail);
    this.state.events = [...(this.state.events || []), { phase, at: this._iso() }].slice(-64);
    this._persist(); this._log('phase', { runId: this.state.runId, phase });
  }
  _ctx(signal) {
    return { runId: this.state.runId, requestId: this.state.requestId, signal, policy: { ...POLICY }, expectedEmergencyRevision: this.state.initialEmergencyRevision, guardReceipt: clone(this.state.guardReceipt), bootstrap: clone(this.state.bootstrap), autoDispatchAfter: false, startWorkAfter: this.state.options?.startWorkAfter === true };
  }
  _trackMutation(name, operation) {
    const id = randomUUID();
    this.pendingMutations.set(id, { runId: this.state.runId, name, startedAt: this._iso() });
    operation.then(() => this.pendingMutations.delete(id), () => this.pendingMutations.delete(id));
    return operation;
  }
  async _call(name, fn, { mutates = false, timeoutMs = this.timeouts.readMs } = {}) {
    this._assertCurrent();
    if (mutates) { this.state.actionPending = { name, startedAt: this._iso() }; this._persist(); }
    const controller = new AbortController();
    const runSignal = this.abort.signal;
    const callDeadline = Math.min(this.now() + timeoutMs, Date.parse(this.state.deadlineAt));
    let timer, abortListener;
    try {
      const stop = new Promise((resolve, reject) => {
        abortListener = () => { controller.abort(); reject(new StartupError('CANCELLED', 'Startup cancelled during ' + name, mutates)); };
        runSignal.addEventListener('abort', abortListener, { once: true });
        const boundedTimeout = Math.max(1, callDeadline - this.now());
        timer = setTimeout(() => { controller.abort(); reject(new StartupError('STAGE_TIMEOUT', name + ' exceeded its deadline', mutates)); }, boundedTimeout);
      });
      let operation = Promise.resolve().then(() => {
        this._assertCurrent();
        if (controller.signal.aborted) throw new StartupError('CANCELLED', 'Startup operation was aborted before invocation', mutates);
        return fn(this._ctx(controller.signal));
      });
      if (mutates) operation = this._trackMutation(name, operation);
      const result = await Promise.race([operation, stop]);
      // A synchronous adapter or a delayed event loop can settle before the
      // timer callback is dispatched, despite having exceeded its deadline.
      if (this.now() >= callDeadline) throw new StartupError('STAGE_TIMEOUT', name + ' completed after its deadline', mutates);
      this._assertCurrent();
      if (mutates) { this.state.actionPending = null; this._persist(); }
      return result;
    } catch (caught) {
      controller.abort();
      const error = caught instanceof Error ? caught : new StartupError('ADAPTER_FAILED', shortError(caught), mutates);
      if (mutates) error.uncertain = true;
      throw error;
    } finally {
      clearTimeout(timer); runSignal.removeEventListener('abort', abortListener);
    }
  }
  async _checkSafety({ allowLatched = false, requireGuard = false, baseline = false } = {}) {
    const value = await this._call('safety read', this.deps.safety);
    const q = value?.quota, emergency = value?.emergency;
    if (!q || q.fresh !== true || !Number.isFinite(q.remainingPercent) || q.remainingPercent < 0 || q.remainingPercent > 100) throw new StartupError('QUOTA_STALE', 'Fresh, valid Codex quota telemetry is required');
    if (q.ordinaryUsageAllowed === false) throw new StartupError('USAGE_UNAVAILABLE', 'Codex ordinary usage is unavailable');
    if (q.remainingPercent <= POLICY.brakePercent) throw new StartupError('QUOTA_LOW', 'Quota must exceed ' + POLICY.brakePercent + '% before or during Startup');
    if (!emergency || typeof emergency.latched !== 'boolean') throw new StartupError('EMERGENCY_UNKNOWN', 'Emergency-latch state is unavailable');
    if (baseline) {
      if (emergency.latched && (typeof emergency.revision !== 'string' || !emergency.revision)) throw new StartupError('EMERGENCY_UNKNOWN', 'The latched emergency revision must be identified before release');
      this.state.initialEmergencyRevision = emergency.latched ? emergency.revision : null;
      this.state.latchReleased = !emergency.latched; this._persist();
    }
    if (emergency.latched && (!allowLatched || this.state.latchReleased)) throw new StartupError('EMERGENCY_ACTIVE', 'Emergency stop was engaged; Startup cannot continue');
    if (emergency.latched && this.state.initialEmergencyRevision !== emergency.revision) throw new StartupError('EMERGENCY_CHANGED', 'A newer emergency stop superseded this Startup request');
    if (this.state.dispatchHeld && value.dispatchPaused !== true) throw new StartupError('DISPATCH_NOT_HELD', 'Automatic dispatch is not confirmed paused');
    if (requireGuard && (q.armed !== true || q.tripped === true || q.thresholdPercent !== POLICY.thresholdPercent)) throw new StartupError('GUARD_UNARMED', 'The exact 7% quota guard is not armed');
    if (requireGuard && ((q.monitorEnabled !== undefined && q.monitorEnabled !== true) || (q.pollSeconds !== undefined && q.pollSeconds !== POLICY.pollSeconds) || (q.window !== undefined && q.window !== POLICY.window))) throw new StartupError('GUARD_CONFIG_CHANGED', 'Quota guard settings changed during Startup');
    return value;
  }
  async _pollPause(name, deadline) {
    // Sleeping is not an external operation. Timer scheduling jitter must not
    // manufacture an uncertain native submission. The stage/overall deadlines,
    // cancellation and ownership remain authoritative before the next probe.
    this._assertCurrent();
    if (this.now() >= deadline) throw new StartupError('STAGE_TIMEOUT', name + ' did not become ready before its deadline', this.state.restoreStartAttempted);
    const signal = this.abort.signal;
    await new Promise((resolve, reject) => {
      let timer;
      const finish = error => { clearTimeout(timer); signal.removeEventListener('abort', cancel); error ? reject(error) : resolve(); };
      const cancel = () => finish(new StartupError('CANCELLED', 'Polling aborted', this.state.restoreStartAttempted));
      signal.addEventListener('abort', cancel, { once: true });
      if (signal.aborted) { cancel(); return; }
      timer = setTimeout(() => finish(), Math.min(this.pollMs, Math.max(1, deadline - this.now())));
    });
    this._assertCurrent();
    if (this.now() >= deadline) throw new StartupError('STAGE_TIMEOUT', name + ' did not become ready before its deadline', this.state.restoreStartAttempted);
  }
  async _wait(name, phase, timeoutMs, probe, { requireGuard = false } = {}) {
    this._transition(phase, { stageDeadlineAt: new Date(this.now() + timeoutMs).toISOString() });
    const deadline = this.now() + timeoutMs;
    while (true) {
      await this._checkSafety({ requireGuard });
      const result = await this._call(name, probe, { timeoutMs: Math.max(1, Math.min(this.timeouts.readMs, deadline - this.now())) });
      if (this.now() >= deadline) throw new StartupError('STAGE_TIMEOUT', name + ' completed after its verification deadline', this.state.restoreStartAttempted);
      if (result === true || result?.ready === true) return result;
      if (result?.failed) throw new StartupError('STAGE_FAILED', name + ': ' + String(result.reason || 'verification failed'), this.state.restoreStartAttempted);
      if (this.now() >= deadline) throw new StartupError('STAGE_TIMEOUT', name + ' did not become ready before its deadline', this.state.restoreStartAttempted);
      await this._pollPause(name, deadline);
    }
  }

  async _run() {
    await this._checkSafety({ allowLatched: true, baseline: true });
    const boot = await this._call('bootstrap validation', this.deps.bootstrap);
    if (Number.isInteger(boot?.targetCount) && boot.targetCount > 0) {
      if (boot.branchAdoptionsRevision != null && (typeof boot.branchAdoptionsRevision !== 'string' || !/^[a-f0-9]{64}$/.test(boot.branchAdoptionsRevision))) throw new StartupError('BOOTSTRAP_BLOCKED', 'Reviewed workspace branch mapping returned an invalid revision');
      this.state.bootstrap = { targetCount: boot.targetCount, alreadyOnline: boot.alreadyOnline === true, primaryIds: Array.isArray(boot.primaryIds) ? boot.primaryIds.map(String) : [], targetIds: Array.isArray(boot.targetIds) ? boot.targetIds.map(String) : [], manifestRevision: boot.manifestRevision || null, ...(Object.hasOwn(boot,'branchAdoptionsRevision')?{branchAdoptionsRevision:boot.branchAdoptionsRevision??null}:{}) }; this._persist();
    }
    if (boot?.ok !== true || !Number.isInteger(boot.targetCount) || boot.targetCount < 1) throw new StartupError('BOOTSTRAP_BLOCKED', String(boot?.reason || 'Manifest, resources, binaries and primary controllers must pass bootstrap validation'), boot?.reconciliationRequired === true);
    await this._checkSafety({ allowLatched: true });
    this._transition('holding_dispatch');
    await this._call('hold dispatch', this.deps.holdDispatch, { mutates: true, timeoutMs: this.timeouts.actionMs });
    this.state.dispatchHeld = true; this._persist();
    await this._checkSafety({ allowLatched: true });
    if (!this.state.latchReleased) {
      this._transition('unlocking');
      await this._call('release emergency latch', this.deps.unlock, { mutates: true, timeoutMs: this.timeouts.actionMs });
      this.state.latchReleased = true; this._persist();
    }
    await this._checkSafety();
    await this._wait('watchdog disarmed verification', 'waiting_watchdog', this.timeouts.watchdogMs, this.deps.watchdogDisarmed);
    this._transition('arming_guard');
    await this._checkSafety();
    const receipt = await this._call('arm 7% guard', this.deps.arm, { mutates: true, timeoutMs: this.timeouts.actionMs });
    const token = receipt?.token || receipt?.revision;
    if (receipt?.armed !== true || typeof token !== 'string' || !token) throw new StartupError('GUARD_ACK_MISSING', 'Arming did not return a monitor-acknowledgment token', true);
    this.state.guardReceipt = { token, armedAt: receipt.armedAt || this._iso() }; this._persist();
    await this._wait('guard acknowledgment', 'waiting_guard_ack', this.timeouts.guardAckMs, this.deps.guardAck, { requireGuard: true });
    this.state.guardAcknowledged = true; this._persist();
    await this._checkSafety({ requireGuard: true });
    if (!boot.alreadyOnline) {
      this._transition('starting_restore');
      this.state.restoreStartAttempted = true; this._persist();
      const receipt = await this._call('start existing RestoreFleet', ctx => this.deps.restore.start(ctx), { mutates: true, timeoutMs: this.timeouts.actionMs });
      if (typeof receipt?.startedAt === 'string' && Number.isFinite(Date.parse(receipt.startedAt)) && receipt.total === boot.targetCount) this.state.restoreReceipt = { startedAt: receipt.startedAt, total: receipt.total };
      this.state.restoreStarted = true; this._persist();
      await this._wait('fleet restoration', 'restoring', this.timeouts.restoreMs, async ctx => {
        const status = await this.deps.restore.status(ctx);
        this._assertCurrent();
        this.state.restore = { phase: status?.phase || 'unknown', active: !!status?.active, total: status?.total ?? null, restoredCount: status?.restoredCount ?? null, lastError: status?.lastError || null }; this._persist();
        if (status?.phase === 'complete' && Number.isInteger(status.total) && status.total > 0 && status.restoredCount === status.total && status.active === false) return { ready: true };
        if (status?.active === false && ['failed', 'cancelled', 'paused', 'partial'].includes(status.phase)) return { failed: true, reason: status.lastError || status.phase };
        return { ready: false };
      }, { requireGuard: true });
    }
    await this._wait('current runtime readiness', 'verifying', this.timeouts.readinessMs, this.deps.readiness, { requireGuard: true });
    if (this.state.options.openUi) {
      this._transition('opening_ui'); await this._checkSafety({ requireGuard: true });
      await this._call('open Orchestrator UI', this.deps.openUi, { mutates: true, timeoutMs: this.timeouts.actionMs });
    }
    await this._checkSafety({ requireGuard: true });
    if (this.state.options.startWorkAfter) {
      this._transition('starting_dispatch');
      const receipt = await this._call('enable verified Sustained dispatch', this.deps.startDispatch, { mutates: true, timeoutMs: this.timeouts.readMs });
      if (receipt?.enabled !== true || receipt.mode !== 'sustained') throw new StartupError('DISPATCH_UNVERIFIED', 'Sustained dispatch did not confirm enablement', true);
      this.state.dispatchHeld = false; this.state.dispatchReceipt = receipt; this._persist();
      await this._checkSafety({ requireGuard: true });
    }
    this.state.active = false; this.state.phase = this.state.options.startWorkAfter ? 'ready_dispatching' : 'ready_paused'; this.state.finishedAt = this._iso(); this.state.stageDeadlineAt = null;
    this.state.error = null; this._persist(); this._log(this.state.phase, { runId: this.state.runId });
  }

  async _cleanup() {
    // Cleanup does not launch anything. Pause only the restore operation this run attempted.
    const outcomes = [];
    this.state.cleanup = { active: true, startedAt: this._iso(), outcomes: [] }; this._persist();
    const limited = async (name, fn) => {
      this._assertOwned();
      const controller = new AbortController(); let timer;
      try {
        const operation = this._trackMutation(name, Promise.resolve().then(() => {
          this._assertOwned();
          if (controller.signal.aborted) throw Error('Cleanup was aborted before invocation');
          return fn(this._ctx(controller.signal));
        }));
        await Promise.race([operation, new Promise((resolve, reject) => { timer = setTimeout(() => { controller.abort(); reject(Error('cleanup deadline exceeded')); }, this.timeouts.actionMs); })]);
        outcomes.push({ name, ok: true });
      } catch (error) { controller.abort(); if (error.code === 'OWNER_CHANGED') throw error; outcomes.push({ name, ok: false, error: shortError(error) }); }
      finally { clearTimeout(timer); }
    };
    await limited('hold dispatch', this.deps.holdDispatch);
    if (this.state.restoreStartAttempted) await limited('pause restore', ctx => this.deps.restore.pause(ctx));
    this._assertOwned();
    this.state.cleanup = { active: false, at: this._iso(), outcomes };
  }
  async _failure(error) {
    if (error.code === 'OWNER_CHANGED' || this.readError) {
      this.abort?.abort();
      this.state = this._load();
      this._log('ownership_lost', { message: shortError(error) });
      return;
    }
    const wasCancelled = this.state.error?.code === 'CANCELLED';
    const uncertain = error.uncertain === true || !!this.state.actionPending || this.state.restoreStartAttempted || this.state.reconciliationRequired;
    this.state.active = false;
    this.state.phase = uncertain ? 'uncertain' : wasCancelled ? 'cancelled' : 'failed';
    this.state.reconciliationRequired = uncertain; this.state.finishedAt = this._iso();
    if (!wasCancelled) this.state.error = { code: error.code || 'STARTUP_FAILED', message: shortError(error) };
    this._persist();
    // Read-only validation failures before dispatch-hold must not mutate anything.
    if (this.state.dispatchHeld || this.state.dispatchReceipt || this.state.actionPending || this.state.restoreStartAttempted) await this._cleanup();
    if (this.pendingMutations.size || this.state.cleanup?.outcomes?.some(outcome => !outcome.ok)) {
      this.state.phase = 'uncertain'; this.state.reconciliationRequired = true;
    }
    this._persist(); this._log('failed', { runId: this.state.runId, phase: this.state.phase, code: this.state.error?.code });
  }

  _stateFingerprint() {
    return createHash('sha256').update(fs.readFileSync(this.file)).digest('hex');
  }
  async reconcile({ confirm, runId } = {}) {
    if (confirm !== 'RECHECK STARTUP') throw new StartupError('CONFIRMATION_REQUIRED', 'Exact RECHECK STARTUP confirmation is required');
    if (!runId || runId !== this.state.runId) throw new StartupError('RUN_MISMATCH', 'Recheck must identify the current Startup run');
    if (this.reconcileTask) return this.reconcileTask;
    const pending = reason => ({ statusCode: 202, reconciled: false, pending: true, note: reason, run: this.status() });
    if (this.task || this.state.active || this.pendingMutations.size) return pending('Startup or a previously submitted operation is still in flight. No state was cleared.');
    if (this.readError) throw new StartupError('STATE_UNREADABLE', 'Unreadable Startup state requires manual preservation and repair before reconciliation', true);
    if (this.state.phase !== 'uncertain' && !this.state.reconciliationRequired) return { statusCode: 200, reconciled: false, idempotent: true, note: 'Startup does not currently need reconciliation.', run: this.status() };
    if (typeof this.deps.reconcile !== 'function') throw new StartupError('RECONCILE_UNAVAILABLE', 'A read-only reconciliation inspector is not configured', true);
    // Read the exact durable revision once. In-memory status may belong to a
    // previous owner and must never authorize overwriting a newer disk run.
    let original, fingerprint;
    try {
      const raw = fs.readFileSync(this.file, 'utf8');
      original = JSON.parse(raw.replace(/^\uFEFF/, ''));
      if (original.schemaVersion !== 1 || typeof original.active !== 'boolean' || !Array.isArray(original.recentRequests)) throw Error('Invalid Startup state');
      fingerprint = createHash('sha256').update(raw).digest('hex');
    } catch { throw new StartupError('STATE_UNREADABLE', 'The durable Startup revision could not be inspected; nothing was cleared', true); }
    if (original.runId !== runId) throw new StartupError('RECONCILE_REVISION_CHANGED', 'A different Startup run is now recorded. Refresh before rechecking.', true);
    this.state = clone(original);
    if (original.active || (original.phase !== 'uncertain' && !original.reconciliationRequired)) return pending('The durable Startup state changed. Refresh its current status; nothing was cleared.');
    const expectedStateRevision = original.revision || fingerprint;
    const beganAt = this.now();
    const controller = new AbortController();
    const ctx = { ...this._ctx(controller.signal), previousRun: clone(original), expectedStateRevision, inspectionStartedAt: new Date(beganAt).toISOString() };
    const inspect = async () => {
      let timer;
      try {
        const proof = await Promise.race([
          Promise.resolve().then(() => this.deps.reconcile(ctx)),
          new Promise((resolve, reject) => { timer = setTimeout(() => { controller.abort(); reject(new StartupError('RECONCILE_TIMEOUT', 'Read-only Startup inspection timed out. Uncertainty remains.', true)); }, this.timeouts.readMs); }),
        ]);
        const observed = Date.parse(proof?.observedAt || '');
        if (proof?.runId !== runId || proof?.stateRevision !== expectedStateRevision || !Number.isFinite(observed) || observed < beganAt - 1000 || observed > this.now() + 1000 || this.now() - observed > this.timeouts.readMs) return pending('The inspection did not provide fresh evidence for this exact Startup revision. Nothing was cleared.');
        if (this.task || this.state.active || this.pendingMutations.size) return pending('A Startup operation is still in flight. Nothing was cleared.');
        if (this.state.runId !== runId || this._stateFingerprint() !== fingerprint) throw new StartupError('RECONCILE_REVISION_CHANGED', 'Startup state changed while it was being inspected. Recheck the new revision.', true);
        if (proof.noOperationInFlight !== true || proof.dispatchPaused !== true || proof.restoreActive !== false || proof.launchActive !== false) return pending(proof.reason || 'No-operation-in-flight and paused-dispatch evidence is incomplete. Nothing was cleared.');
        let outcome = null;
        const guard = proof.guard;
        const validGuard = guard?.fresh === true && guard.armed === true && guard.tripped === false && Number.isFinite(guard.remainingPercent) && guard.remainingPercent > POLICY.brakePercent && guard.remainingPercent <= 100 && guard.ordinaryUsageAllowed !== false && guard.thresholdPercent === POLICY.thresholdPercent && guard.pollSeconds === POLICY.pollSeconds && guard.window === POLICY.window && typeof guard.revision === 'string' && guard.revision.length > 0 && guard.acknowledgedRevision === guard.revision;
        if (proof.outcome === 'ready_paused') {
          const expected = original.bootstrap?.targetCount;
          const count = proof.expectedTargetCount;
          if (proof.emergencyLatched !== false || !validGuard || proof.expectedTargetsMatch !== true || proof.allExpectedControllersHealthy !== true || proof.allExpectedRuntimesHealthy !== true || !Number.isInteger(count) || count <= 0 || (Number.isInteger(expected) && count !== expected) || proof.verifiedTargetCount !== count) return pending(proof.reason || 'Current guard acknowledgment and every expected controller/runtime have not all been positively verified. Nothing was cleared.');
          outcome = 'ready_paused';
        } else if (proof.outcome === 'resumable') {
          const expected = original.bootstrap?.targetIds, healthy = proof.healthyTargetIds, stopped = proof.stoppedTargetIds, binding = proof.restoreBinding;
          const sameIds = (left, right) => Array.isArray(left) && Array.isArray(right) && left.every(id => typeof id === 'string') && right.every(id => typeof id === 'string') && new Set(left).size === left.length && new Set(right).size === right.length && JSON.stringify([...left].sort()) === JSON.stringify([...right].sort());
          const events = Array.isArray(original.events) ? original.events : [];
          const stage = events.filter(event => event?.phase === 'starting_restore');
          const waiting = events.filter(event => event?.phase === 'restoring');
          const uncommitted = binding?.kind === 'uncommitted-start';
          const times = (uncommitted ? [binding?.startedAt, original.startedAt, stage[0]?.at, original.actionPending?.startedAt, original.finishedAt] : [original.startedAt, stage[0]?.at, binding?.startedAt, waiting[0]?.at, original.finishedAt]).map(value => Date.parse(value || ''));
          const ordered = times.every(Number.isFinite) && times.every((time, index) => index === 0 || time >= times[index - 1]) && times[4] <= this.now() + 1000;
          const priorOwner = (binding?.legacyPriorOwner === true && binding.priorRunId === null) || (binding?.legacyPriorOwner === false && typeof binding.priorRunId === 'string' && binding.priorRunId.length > 0 && binding.priorRunId !== runId);
          const operationBound = uncommitted ? ordered && times[0] < times[1] && waiting.length === 0 && events.at(-1)?.phase === 'starting_restore' && original.restoreStarted === false && original.restoreReceipt === null && original.actionPending?.name === 'start existing RestoreFleet' && priorOwner && binding.startNotCommitted === true && binding.durableStateVerified === true && binding.durableStateFingerprint === binding.stateFingerprint && /^[a-f0-9]{64}$/.test(binding.durableBytesFingerprint || '') : (!binding?.kind || ['committed-restore','completed-restore'].includes(binding.kind)) && ordered && waiting.length === 1 && original.restoreStarted === true && original.actionPending === null && (!original.restoreReceipt || original.restoreReceipt.startedAt === binding.startedAt && original.restoreReceipt.total === original.bootstrap?.targetCount);
          const verifying=events.filter(event=>event?.phase==='verifying');
          const completionTimes=[waiting[0]?.at,binding?.completedAt,verifying[0]?.at,original.finishedAt].map(value=>Date.parse(value||''));
          const completedBound=binding?.kind==='completed-restore'&&binding.phase==='complete'&&verifying.length===1&&completionTimes.every(Number.isFinite)&&completionTimes.every((time,index)=>index===0||time>=completionTimes[index-1])&&binding.durableStateVerified===true&&binding.durableStateFingerprint===binding.stateFingerprint&&/^[a-f0-9]{64}$/.test(binding.durableBytesFingerprint||'')&&sameIds(expected,binding.completedTargetIds);
          const bound = binding?.verified === true && binding.runId === runId && (['paused', 'partial', 'failed'].includes(binding.phase)||completedBound) && typeof binding.stateFingerprint === 'string' && /^[a-f0-9]{64}$/.test(binding.stateFingerprint) && stage.length === 1 && operationBound;
          const targetsValid = sameIds(expected, proof.expectedTargetIds) && sameIds(expected, binding?.targetIds) && sameIds(expected, binding?.sourceTargetIds) && sameIds(expected, binding?.desiredIds) && Array.isArray(healthy) && Array.isArray(stopped) && stopped.length > 0 && sameIds(expected, [...healthy, ...stopped]) && proof.expectedTargetCount === expected.length && original.bootstrap.targetCount === expected.length && proof.verifiedTargetCount === healthy.length && proof.verifiedStoppedCount === stopped.length;
          const noUnresolved = Array.isArray(proof.durableSubmittedIds) && proof.durableSubmittedIds.length === 0 && Array.isArray(proof.unresolvedSubmittedIds) && proof.unresolvedSubmittedIds.length === 0;
          if (!bound || !targetsValid || !noUnresolved || original.restoreStartAttempted !== true || proof.emergencyLatched !== false || !validGuard || guard.monitorEnabled !== true || proof.expectedTargetsMatch !== true || proof.resumeAuditComplete !== true || proof.allExpectedTargetsAccounted !== true || !original.bootstrap?.manifestRevision || proof.manifestRevision !== original.bootstrap.manifestRevision || binding.manifestRevision !== original.bootstrap.manifestRevision) return pending('The partial restore is not fully accounted for by fresh protected runtime evidence and the exact saved operation. No state was cleared.');
          outcome = 'resumable';
        } else if (proof.outcome === 'idle') {
          if (proof.submissionAuditComplete !== true || proof.submittedResume !== false || proof.runningExpectedAgents !== 0) return pending(proof.reason || 'A submitted resume or running agent cannot be ruled out. Unknown work is not cleared for a retry.');
          outcome = 'idle';
        } else return pending(proof.reason || 'The inspector could not establish a safe ready or never-submitted state. Nothing was cleared.');
        // No awaits after this comparison: the state change is tied to exactly
        // the revision inspected. The server must also hold its mutation lane.
        if (this._stateFingerprint() !== fingerprint || this.pendingMutations.size || this.state.active || this.task) throw new StartupError('RECONCILE_REVISION_CHANGED', 'Startup changed before the reconciliation result could be committed', true);
        this.state = {
          ...original, ownerId: this.ownerId, ownerPid: process.pid, active: false,
          phase: outcome, reconciliationRequired: false, actionPending: null,
          finishedAt: this._iso(), stageDeadlineAt: null, error: null,
          cleanup: { ...(original.cleanup || {}), active: false },
          reconciliation: { at: this._iso(), outcome, inspectedRevision: expectedStateRevision, previousError: original.error || null, previousAction: original.actionPending || null },
        };
        if (outcome === 'ready_paused') {
          this.state.dispatchHeld = true; this.state.guardAcknowledged = true;
          this.state.guardReceipt = { token: guard.revision, armedAt: guard.armedAt || proof.observedAt };
          this.state.restore = { phase: 'complete', active: false, total: proof.expectedTargetCount, restoredCount: proof.verifiedTargetCount, verifiedAt: proof.observedAt };
        } else if (outcome === 'resumable') {
          this.state.dispatchHeld = true; this.state.guardAcknowledged = true;
          this.state.guardReceipt = { token: guard.revision, armedAt: guard.armedAt || proof.observedAt };
          this.state.restore = { phase: proof.restoreBinding.phase, active: false, total: proof.expectedTargetCount, restoredCount: proof.verifiedTargetCount, verifiedStoppedCount: proof.verifiedStoppedCount, verifiedAt: proof.observedAt };
          Object.assign(this.state.reconciliation, { manifestRevision: proof.manifestRevision, healthyTargetIds: [...proof.healthyTargetIds], stoppedTargetIds: [...proof.stoppedTargetIds], restoreBinding: clone(proof.restoreBinding) });
        } else {
          this.state.guardAcknowledged = false;
          this.state.restore = null;
        }
        this._persist(false);
        this._log('reconciled', { runId, outcome, inspectedRevision: expectedStateRevision });
        return { statusCode: 200, reconciled: true, pending: false, note: outcome === 'ready_paused' ? 'Current protected runtimes verified. Automatic dispatch remains paused; no agents were launched by this recheck.' : outcome === 'resumable' ? 'The partial fleet is accounted for: every preserved target is healthy or verified stopped, with no unresolved native submission. Use a new explicit Startup to continue. Recheck launched no agents and preserved the restore ledger.' : 'No pending launch, running expected agent or submitted resume was found. A new explicit Startup request is allowed; no agents were launched by this recheck.', run: this.status() };
      } finally { clearTimeout(timer); controller.abort(); }
    };
    this.reconcileTask = inspect().finally(() => { this.reconcileTask = null; });
    return this.reconcileTask;
  }
}

module.exports = { StartupController, StartupError, POLICY, DEFAULT_TIMEOUTS };
