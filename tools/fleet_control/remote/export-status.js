'use strict';

// A one-way status publisher. These fixed GET URLs are the complete source set.
// No browser traffic is forwarded to either local control plane.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const {projectAgent,providerOverview}=require('../agent-observability');
const { setTimeout: delay } = require('node:timers/promises');

const SOURCES = Object.freeze({
  fleetHealth: 'http://127.0.0.1:8787/api/health',
  fleetState: 'http://127.0.0.1:8787/api/state',
  fleetMaster: 'http://127.0.0.1:8787/api/master',
  quota: 'http://127.0.0.1:8787/api/quota',
  missionHealth: 'http://127.0.0.1:18729/api/launcher/health',
  missionState: 'http://127.0.0.1:18729/api/state',
});
const INTERVAL_MS = 10000;
const STALE_AFTER_SECONDS = 45;
const MAX_BYTES = 2 * 1024 * 1024;
const record = value => value && typeof value === 'object' && !Array.isArray(value) ? value : {};
const text = (value, limit = 100) => typeof value === 'string' ? value.replace(/[\u0000-\u001f\u007f]/g, '').slice(0, limit) : null;
const number = value => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const percent = value => number(value) !== null && value <= 100 ? value : null;
const bool = value => typeof value === 'boolean' ? value : null;
const timestamp = value => typeof value === 'string' && Number.isFinite(Date.parse(value)) ? new Date(value).toISOString() : null;
const count = value => Number.isSafeInteger(value) && value >= 0 ? value : null;
const code = value => typeof value === 'string' && /^[A-Z][A-Z0-9_]{0,79}$/.test(value) ? value : null;

function schemaValid(name, value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  if (name === 'fleetState') return Array.isArray(value.sessions) && !!timestamp(value.timestamp);
  if (name === 'missionState') return !!value.lanes && typeof value.lanes === 'object' && !Array.isArray(value.lanes) && !!timestamp(value.time);
  if (name === 'fleetHealth') return value.ok === true;
  if (name === 'missionHealth') return value.service === 'mjobs-mission-control' && value.status === 'ready';
  if (name === 'fleetMaster') return !!value.dispatch && !!value.emergency;
  if (name === 'quota') return typeof value.fresh === 'boolean' && !!value.config;
  return false;
}

async function fetchSource(name, fetcher = fetch, now = () => Date.now()) {
  if (!Object.hasOwn(SOURCES, name)) throw new Error('Unknown status source');
  // Master returns a bounded "inspection pending" response while its single
  // runtime inspector continues. Re-read that same GET to observe its result;
  // the entire sequence, including response bodies and delays, shares one cap.
  const signal = AbortSignal.timeout(8000);
  const attempts = name === 'fleetMaster' ? 3 : 1;
  try {
    for (let attempt = 0; attempt < attempts; attempt++) {
      signal.throwIfAborted();
      const response = await fetcher(SOURCES[name], {
        method: 'GET', redirect: 'error', signal,
        headers: { Accept: 'application/json' },
      });
      if (!response.ok) return { available: false, error: 'HTTP_' + response.status };
      if (Number(response.headers.get('content-length') || 0) > MAX_BYTES) return { available: false, error: 'RESPONSE_TOO_LARGE' };
      const chunks = []; let bytes = 0;
      for await (const chunk of response.body) {
        bytes += chunk.byteLength;
        if (bytes > MAX_BYTES) return { available: false, error: 'RESPONSE_TOO_LARGE' };
        chunks.push(Buffer.from(chunk));
      }
      const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
      if (!schemaValid(name, data)) return { available: false, error: 'INVALID_STATUS' };
      const sourceTime = name === 'fleetState' || name === 'fleetMaster' ? data.timestamp : name === 'missionState' ? data.time : null;
      if (sourceTime) {
        const age = now() - Date.parse(sourceTime);
        if (age < -10000 || age > STALE_AFTER_SECONDS * 1000) return { available: false, error: 'STALE_SOURCE' };
      }
      if (name === 'fleetMaster' && data.currentReadiness?.inspectionPending === true && attempt + 1 < attempts) {
        await delay(250, undefined, { signal });
        continue;
      }
      return { available: true, checkedAt: new Date(now()).toISOString(), data };
    }
  } catch (error) {
    return { available: false, error: ['TimeoutError', 'AbortError'].includes(error?.name) ? 'TIMEOUT' : 'UNAVAILABLE' };
  }
}

function projectSnapshot(results, now = Date.now()) {
  const get = name => results[name]?.available === true ? record(results[name].data) : {};
  const available = name => results[name]?.available === true;
  const fleet = get('fleetState'), health = get('fleetHealth'), master = get('fleetMaster');
  const quota = get('quota'), mission = get('missionState');
  const dispatch = record(master.dispatch), emergency = record(master.emergency);
  const startup = record(master.startup), restore = record(master.restore), drain = record(master.drain);
  const current = record(master.currentReadiness);
  const readinessAge = now - Date.parse(current.at || '');
  const readinessAvailable = available('fleetMaster') && current.inspectionPending !== true && current.stale !== true
    && Number.isFinite(readinessAge) && readinessAge >= -10000 && readinessAge <= STALE_AFTER_SECONDS * 1000
    && count(current.total) !== null && count(current.verified) !== null && current.verified <= current.total;
  const config = record(quota.config);
  const runtimeAvailable = available('fleetState') && available('fleetHealth') && fleet.offline !== true;
  let dispatchState = 'unknown'; let dispatchReason = 'Dispatch status is unavailable';
  if (available('fleetMaster') && available('fleetHealth')) {
    if (emergency.latched === true) { dispatchState = 'blocked'; dispatchReason = 'Emergency stop is latched'; }
    else if (drain.enabled === true) { dispatchState = 'blocked'; dispatchReason = 'Fleet is draining'; }
    else if (startup.active === true || restore.active === true) { dispatchState = 'blocked'; dispatchReason = 'Startup or restoration is in progress'; }
    else if (typeof health.dispatchBlockedReason === 'string' && health.dispatchBlockedReason.length) {
      dispatchState = 'blocked';
      dispatchReason = config.autoKillArmed === false ? 'Quota protection is not armed'
        : quota.fresh === false ? 'Quota reading is unavailable or stale'
        : quota.policy?.blocked === true && percent(config.minimumRemainingPercent)!==null ? 'Quota hold: '+percent(quota.selectedRemainingPercent)+'% remaining; start/new work requires more than '+percent(config.minimumRemainingPercent)+'%. Change it on the local board.'
        : quota.guardAcknowledged === false ? 'Waiting for quota guard acknowledgement'
        : startup.reconciliationRequired === true ? 'Startup requires reconciliation'
        : health.dispatchBlockedReason === 'No live primary orchestrator' ? 'Primary orchestrator is stopped; use protected Startup locally'
        : health.dispatchBlockedReason === 'GitHub snapshot unavailable or stale; no automatic assignments' ? 'GitHub repository inventory is unavailable or stale'
        : 'Local control plane reports a dispatch hold';
    } else if (!runtimeAvailable) { dispatchState = 'unknown'; dispatchReason = 'Live fleet runtime is unavailable'; }
    else if (dispatch.sustainedEnabled === true || dispatch.turboEnabled === true) { dispatchState = 'enabled'; dispatchReason = dispatch.turboEnabled ? 'Turbo dispatch is enabled' : 'Sustained dispatch is enabled'; }
    else { dispatchState = 'paused'; dispatchReason = 'Automatic dispatch is paused'; }
  }
  const quotaAge = now - Date.parse(quota.sampledAt || '');
  const quotaFresh = available('quota') && quota.fresh === true && quota.monitorOnline === true
    && Number.isFinite(quotaAge) && quotaAge >= -10000 && quotaAge <= STALE_AFTER_SECONDS * 1000;
  const sessions = Array.isArray(fleet.sessions) ? fleet.sessions.slice(0,256).map(session => projectAgent(session,{live:runtimeAvailable,readiness:current,now})) : [];
  const summary = record(fleet.summary);
  const independentLanes = Object.entries(record(mission.lanes)).slice(0,24).map(([id, lane]) => {
    const runtime = record(lane.runtime), cfg = record(lane.config), activity = record(lane.activity);
    return { id: text(id), model: text(cfg.model), effort: text(cfg.reasoning_effort || cfg.effort),
      status: text(runtime.status), activity: text(activity.state),
      startedAt: timestamp(runtime.started_at), stoppedAt: timestamp(runtime.stopped_at) };
  });
  return {
    schemaVersion: 1, readOnly: true, generatedAt: new Date(now).toISOString(),
    refreshSeconds: INTERVAL_MS / 1000, staleAfterSeconds: STALE_AFTER_SECONDS,
    sources: Object.fromEntries(Object.keys(SOURCES).map(name => [name, {
      available: available(name), checkedAt: timestamp(results[name]?.checkedAt),
      error: available(name) ? null : code(results[name]?.error) || 'UNAVAILABLE',
    }])),
    fleet: { available: available('fleetState'), runtimeAvailable,
      observedAt: timestamp(fleet.timestamp), preservedSnapshot: fleet.offline === true,
      summary: Object.fromEntries(['total','working','idle','idleFree','idlePending','ciAttention','other'].map(key => [key,
        !available('fleetState') ? null : key === 'working' && !runtimeAvailable ? 0 : count(summary[key])])),
      sessions,
    },
    dispatch: { state: dispatchState, reason: dispatchReason,
      sustainedEnabled: available('fleetMaster') ? bool(dispatch.sustainedEnabled) : null,
      turboEnabled: available('fleetMaster') ? bool(dispatch.turboEnabled) : null },
    startup: { available: available('fleetMaster'), phase: text(startup.phase), active: bool(startup.active),
      reconciliationRequired: bool(startup.reconciliationRequired), startedAt: timestamp(startup.startedAt),
      finishedAt: timestamp(startup.finishedAt), errorCode: code(record(startup.error).code),
      restorePhase: text(restore.phase), restoreActive: bool(restore.active),
      emergencyLatched: bool(emergency.latched) },
    readiness: { available: readinessAvailable, observedAt: timestamp(current.at),
      inspectionPending: bool(current.inspectionPending),
      total: readinessAvailable ? count(current.total) : null,
      verified: readinessAvailable ? count(current.verified) : null,
      ready: readinessAvailable ? bool(current.ready) : null,
      runtimesReady: readinessAvailable ? bool(current.runtimesReady) : null,
      dispatchPaused: available('fleetMaster') ? bool(current.dispatchPaused) : null },
    quota: { available: available('quota'), fresh: quotaFresh,
      sampledAt: timestamp(quota.sampledAt), armed: available('quota') ? bool(config.autoKillArmed) : null,
      guardAcknowledged: quotaFresh ? bool(quota.guardAcknowledged) : null,
      remainingPercent: quotaFresh ? percent(quota.selectedRemainingPercent) : null,
      minimumRemainingPercent: percent(config.minimumRemainingPercent), startWorkBlocked: quotaFresh ? quota.policy?.blocked===true : true, thresholdPercent: percent(config.thresholdPercent), monitoredWindow: text(quota.monitoredWindow),
      windows: quotaFresh && Array.isArray(quota.windows) ? quota.windows.slice(0,8).map(window => ({
        label: text(window.label), remainingPercent: percent(window.remainingPercent), resetsAt: timestamp(window.resetsAt),
      })) : [] },
    missionControl: { available: runtimeAvailable || (available('missionHealth') && available('missionState')),
      observedAt: timestamp(mission.time), lanes: providerOverview(sessions,independentLanes,{fleetAvailable:runtimeAvailable,missionAvailable:available('missionHealth')&&available('missionState')}) },
  };
}

async function collectSnapshot(fetcher = fetch, now = () => Date.now()) {
  const entries = await Promise.all(Object.keys(SOURCES).map(async name => [name, await fetchSource(name, fetcher, now)]));
  return projectSnapshot(Object.fromEntries(entries), now());
}

function writeSnapshot(output, snapshot) {
  fs.mkdirSync(output, { recursive: true });
  const destination = path.join(output, 'status.json');
  const temp = path.join(output, '.status-' + process.pid + '-' + crypto.randomUUID() + '.tmp');
  try {
    fs.writeFileSync(temp, JSON.stringify(snapshot) + '\n', { encoding: 'utf8', flag: 'wx' });
    fs.renameSync(temp, destination);
  } finally { try { fs.unlinkSync(temp); } catch (error) { if (error.code !== 'ENOENT') throw error; } }
}

async function main(args = process.argv.slice(2)) {
  let output; let watch = false;
  for (let index=0; index<args.length; index++) {
    if (args[index] === '--output' && args[index+1] && !args[index+1].startsWith('--')) output = path.resolve(args[++index]);
    else if (args[index] === '--watch') watch = true;
    else throw new Error('Usage: node export-status.js --output <public-directory> [--watch]');
  }
  if (!output) throw new Error('An explicit --output directory is required');
  let stopped = false; let lastFailure = null;
  process.once('SIGTERM', () => { stopped = true; });
  process.once('SIGINT', () => { stopped = true; });
  do {
    const started = Date.now();
    try { writeSnapshot(output, await collectSnapshot()); lastFailure = null; }
    catch (error) {
      const failure = error?.code || 'EXPORT_FAILED';
      if (failure !== lastFailure) process.stderr.write('Remote status publication failed: ' + failure + '\n');
      lastFailure = failure;
      if (!watch) throw error;
    }
    if (!watch || stopped) break;
    await delay(Math.max(1000, INTERVAL_MS - (Date.now() - started)));
  } while (!stopped);
}

if (require.main === module) main().catch(() => { process.stderr.write('Remote status exporter stopped. Check output permissions and invocation.\n'); process.exitCode=1; });
module.exports = { SOURCES, STALE_AFTER_SECONDS, schemaValid, fetchSource, projectSnapshot, collectSnapshot, writeSnapshot };
