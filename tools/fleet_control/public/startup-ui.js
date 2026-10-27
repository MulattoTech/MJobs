'use strict';

const FleetStartupUI = (() => {
  const root = document.getElementById('startupSection');
  if (!root) return { refresh: async () => {} };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
  const byId = id => document.getElementById(id);
  const storeKey = 'mjobs-fleet-startup-request-v1';
  const phases = {
    idle: ['Not started', 'Startup is an explicit action. Reading this page does not launch agents.', 0],
    validating: ['Checking startup prerequisites', 'Verifying fresh quota, the saved fleet, resources and primary-controller prerequisites.', 1],
    holding_dispatch: ['Holding automatic dispatch', 'Sustained and Turbo dispatch are being kept paused.', 1],
    unlocking: ['Releasing the verified stop latch', 'Only the emergency revision authorized by this Startup request may be released.', 2],
    waiting_watchdog: ['Waiting for the stop watcher', 'No agent restoration is allowed until the emergency watcher is confirmed disarmed.', 2],
    arming_guard: ['Arming the 7% emergency guard', 'Configuring quota protection before any fleet restoration.', 3],
    waiting_guard_ack: ['Verifying guard acknowledgment', 'Waiting for the quota monitor to acknowledge this exact guard configuration.', 3],
    starting_restore: ['Starting preserved-fleet recovery', 'Using the existing restore controller; session identities and worktrees are preserved.', 4],
    restoring: ['Restoring the preserved fleet', 'Primary controllers are verified before workers are restored in bounded batches.', 5],
    verifying: ['Checking current runtime readiness', 'Historical completion alone is not sufficient; current runtime and controller checks must pass.', 6],
    opening_ui: ['Opening Orchestrator', 'Opening the desktop UI without starting a duplicate restore operation.', 7],
    starting_dispatch: ['Starting verified work dispatch', 'Enabling Sustained only after all preserved runtimes and quota protection are verified.', 7],
    ready_dispatching: ['Startup completed with dispatch enabled', 'Sustained dispatch is enabled. Current agent activity is reported separately.', 7],
    ready_paused: ['Startup completed', 'The startup sequence completed. Automatic dispatch remains paused.', 7],
    failed: ['Startup blocked', 'Resolve the reported prerequisite or verification failure before trying again.', 0],
    cancelled: ['Startup cancelled', 'No further startup steps will be submitted. Already restored runtimes are not claimed to be stopped.', 0],
    uncertain: ['Startup needs reconciliation', 'A prior launch or restore needs verification. Recheck safely can recognize verified completion; it never resends a launch or restores agents.', 0],
    resumable: ['Interrupted startup can continue', 'Existing runtimes and stopped sessions were verified. Run Startup to restore the remaining sessions; healthy agents will be preserved.', 0],
  };
  const stepLabels = ['Validate', 'Stop watcher', 'Arm 7%', 'Start restore', 'Restore fleet', 'Verify now', 'Ready / UI'];
  let run = null, readiness = null, preview = null, lastSuccess = 0, readError = null, lastProgress = null;
  let readPromise = null, refreshQueued = false, mutationEpoch = 0, actionBusy = false, modalOpen = false;
  let pendingRequest = null;
  try {
    const saved = JSON.parse(sessionStorage.getItem(storeKey) || 'null');
    if (saved && typeof saved.requestId === 'string' && /^[A-Za-z0-9._:-]{8,128}$/.test(saved.requestId) && typeof saved.openUi === 'boolean') pendingRequest = saved;
  } catch {}

  root.classList.add('startup-panel');
  root.innerHTML = '<div class="startup-heading"><div><p class="startup-eyebrow">Protected launch</p><h2>Start the fleet safely</h2><p class="startup-intro">One explicit startup sequence: verify the allowance, arm the 7% stop, then recover the preserved fleet.</p></div><span id="startupBadge" class="startup-badge">Checking status…</span></div>' +
    '<div class="startup-current" role="status" aria-live="polite" aria-atomic="true"><h3 id="startupPhase">Loading startup state</h3><p id="startupDetail">No agents are being started by this refresh.</p></div>' +
    '<ol id="startupSteps" class="startup-steps" aria-label="Startup stages">' + stepLabels.map((label, index) => '<li data-startup-step="' + (index + 1) + '"><span>' + (index + 1) + '</span>' + label + '</li>').join('') + '</ol>' +
    '<div id="startupRestoreProgress" class="startup-restore" hidden><label id="startupRestoreLabel" for="startupRestoreMeter">Restoration progress</label><progress id="startupRestoreMeter" max="1" value="0"></progress><p id="startupRestoreDetail"></p></div>' +
    '<div id="startupIssue" class="startup-issue" role="alert" hidden></div>' +
    '<p id="startupFreshness" class="startup-freshness">Waiting for a status response.</p>' +
    '<div class="startup-actions"><button id="startupStart" type="button" class="primary-button" disabled>Startup…</button><button id="startupOpenUi" type="button" class="ghost-button" disabled>Open Orchestrator</button><button id="startupRecheck" type="button" class="ghost-button" hidden>Recheck safely</button><button id="startupCancel" type="button" class="danger-button" hidden>Cancel startup</button><button id="startupRefresh" type="button" class="ghost-button">Refresh status</button></div>' +
    '<p class="startup-footnote">Startup can resume agent work and consume usage. Select Start working after verification to enable Sustained automatically; uncheck it for restore-only. The 7% cutoff cannot eliminate provider-metering delay.</p>';

  function setText(id, text) { const el = byId(id); if (el && el.textContent !== text) el.textContent = text; }
  function fresh() { return !!run && !readError && Date.now() - lastSuccess < 20000; }
  function alreadyStarted() { return ['ready_paused','ready_dispatching'].includes(run?.phase) && (readiness?.ready === true || readiness?.inspectionPending === true); }
  function uncertain() { return !!run && (run.phase === 'uncertain' || run.reconciliationRequired === true); }
  function relativeTime(value) {
    const age = Date.now() - Date.parse(value || '');
    if (!Number.isFinite(age) || age < 0) return 'time unavailable';
    if (age < 60000) return Math.floor(age / 1000) + 's ago';
    if (age < 3600000) return Math.floor(age / 60000) + 'm ago';
    return new Date(value).toLocaleString();
  }
  function savePending() {
    try { if (pendingRequest) sessionStorage.setItem(storeKey, JSON.stringify(pendingRequest)); else sessionStorage.removeItem(storeKey); } catch {}
  }
  function requestId() {
    if (globalThis.crypto?.randomUUID) return crypto.randomUUID();
    if (globalThis.crypto?.getRandomValues) return 'startup-' + Array.from(crypto.getRandomValues(new Uint8Array(16)), byte => byte.toString(16).padStart(2, '0')).join('');
    return 'startup-' + Date.now().toString(36) + '-' + Math.random().toString(36).slice(2);
  }
  function notify(message, error = false) { if (typeof toast === 'function') toast(message, error); }
  function phaseInfo() { return phases[run?.phase] || ['Startup status: ' + (run?.phase || 'unknown'), 'Check the server status before starting more work.', 0]; }
  function issuesText() {
    const issues = readiness?.issues;
    if (Array.isArray(issues)) return issues.map(issue => String(issue.message || issue.code || issue)).join(' ');
    return String(readiness?.reason || readiness?.message || '');
  }
  function accept(payload, fromRead) {
    const candidate = payload?.run || payload;
    if (!candidate || typeof candidate.phase !== 'string') throw Error('Startup status response is invalid');
    run = candidate;
    readiness = payload?.readiness || payload?.preflight || candidate.readiness || null;
    preview = payload?.preview || candidate.preview || null;
    if (fromRead) { lastSuccess = Date.now(); readError = null; }
    if (pendingRequest && run.requestId === pendingRequest.requestId) {
      pendingRequest.acknowledged = true; pendingRequest.uncertain = false; savePending();
    }
    render();
  }
  function render() {
    const [label, description, step] = phaseInfo();
    const stale = !fresh();
    let badge = run?.active ? 'STARTING' : uncertain() ? 'RECHECK REQUIRED' : run?.phase === 'resumable' ? 'CONTINUE STARTUP' : 'PAUSED';
    let title = label, detail = description;
    if (['ready_paused','ready_dispatching'].includes(run?.phase)) {
      if (!stale && readiness?.inspectionPending === true) {
        badge = 'CHECKING HEALTH'; title = 'Fleet is started';
        detail = 'Checking current runtime health without restarting anything. The last completed count remains visible below; new work still requires current verification.';
      } else if (stale || readiness?.ready !== true) {
        badge = stale ? 'STATUS STALE' : readiness?.ready === false ? 'NOT READY NOW' : 'VERIFY CURRENT STATE';
        title = 'Earlier startup completed · current readiness ' + (readiness?.ready === false && !stale ? 'blocked' : 'not verified');
        detail = 'Last completion: ' + (run.finishedAt ? new Date(run.finishedAt).toLocaleString() : 'time unavailable') + '. ' + (issuesText() || 'Current live readiness must be checked before assuming the fleet is available.');
      } else {
        title = 'Fleet is started';
        badge = readiness.dispatchPaused === false ? 'READY · DISPATCH ON' : 'READY · DISPATCH PAUSED';
        detail = readiness.dispatchPaused === false ? 'Current runtime readiness is verified. Sustained or Turbo dispatch is enabled. Agent activity below confirms whether work is actually underway.' : 'Current readiness is verified. Preserved runtimes are restored; automatic task dispatch remains paused. Use Sustained or Turbo explicitly to begin new automatic assignments.';
      }
    }
    if (stale && !['ready_paused','ready_dispatching'].includes(run?.phase)) badge = 'STATUS STALE';
    if (!run) { title = 'Startup status unavailable'; detail = 'Waiting for a valid server response. No startup request has been sent.'; }
    setText('startupBadge', badge); setText('startupPhase', title); setText('startupDetail', detail);
    byId('startupBadge').className = 'startup-badge' + (stale || uncertain() || run?.phase === 'failed' ? ' startup-badge-warning' : run?.active ? ' startup-badge-active' : readiness?.ready === true && ['ready_paused','ready_dispatching'].includes(run?.phase) ? ' startup-badge-ready' : '');
    for (const el of root.querySelectorAll('[data-startup-step]')) {
      const index = Number(el.dataset.startupStep);
      el.classList.toggle('is-current', !!run?.active && step === index);
      el.classList.toggle('is-done', (!!run?.active && step > index) || (['ready_paused','ready_dispatching'].includes(run?.phase) && !stale && readiness?.ready === true));
      if (run?.active && step === index) el.setAttribute('aria-current', 'step'); else el.removeAttribute('aria-current');
    }
    const restore = run?.restore;
    if (!stale && !readiness?.inspectionPending && Number.isInteger(readiness?.total) && Number.isInteger(readiness?.verified)) lastProgress = { total:readiness.total,verified:readiness.verified,at:readiness.at };
    const liveTotal = readiness?.total;
    const liveVerified = readiness?.verified;
    const liveProgress = !stale && readiness?.inspectionPending !== true && Number.isInteger(liveTotal) && liveTotal > 0 && Number.isInteger(liveVerified);
    const progressTotal = liveProgress ? liveTotal : lastProgress?.total ?? restore?.total;
    const progressVerified = liveProgress ? liveVerified : lastProgress?.verified ?? restore?.restoredCount;
    const hasProgress = liveProgress || !!lastProgress || (!!run?.active && Number.isInteger(progressTotal) && progressTotal > 0 && Number.isInteger(progressVerified));
    byId('startupRestoreProgress').hidden = !hasProgress;
    if (hasProgress) {
      byId('startupRestoreMeter').max = progressTotal; byId('startupRestoreMeter').value = Math.max(0, Math.min(progressTotal, progressVerified));
      setText('startupRestoreLabel', progressVerified + ' / ' + progressTotal + (liveProgress ? ' runtimes verified' : ' runtimes at last completed check; checking current health'));
      setText('startupRestoreDetail', restore?.lastError || (liveProgress ? 'Usage remaining and an armed stop guard do not mean the fleet is running. ' + (readiness.dispatchPaused ? 'Automatic dispatch is paused.' : 'Automatic dispatch is enabled.') : 'Final current-controller readiness is checked separately before Startup completes.'));
    }
    const problem = readError ? 'Refresh failed: ' + readError + '. The last displayed state is not current.' : run?.error?.message || (uncertain() ? 'Recheck the guard and runtime outcome before retrying. Recheck does not launch agents.' : '');
    byId('startupIssue').hidden = !problem; setText('startupIssue', problem);
    setText('startupFreshness', lastSuccess ? 'Last successful check: ' + new Date(lastSuccess).toLocaleTimeString() + (run?.runId ? ' · Run ' + run.runId.slice(0, 8) : '') : 'Waiting for a status response.');
    byId('startupStart').disabled = actionBusy || stale || !!run?.active || uncertain() || alreadyStarted();
    setText('startupStart', alreadyStarted() ? 'Already started' : pendingRequest?.uncertain ? 'Retry original Startup…' : run?.phase === 'resumable' ? 'Continue Startup…' : 'Startup…');
    byId('startupStart').title = alreadyStarted() ? 'The fleet is already restored. Use Pause/Resume Sustained to control assignments; do not start another restore.' : '';
    byId('startupOpenUi').disabled = actionBusy || stale || !!run?.active || uncertain() || run?.phase === 'resumable';
    byId('startupCancel').hidden = !run?.active; byId('startupCancel').disabled = actionBusy || !run?.runId;
    byId('startupRecheck').hidden = !uncertain(); byId('startupRecheck').disabled = actionBusy || !run?.runId;
    byId('startupRefresh').disabled = actionBusy;
    updateModal();
  }
  function updateModal() {
    if (!modalOpen || !byId('startupConfirmText')) { modalOpen = false; return; }
    const confirm = byId('startupConfirmButton');
    confirm.disabled = actionBusy || !fresh() || !!run?.active || uncertain() || alreadyStarted() || byId('startupConfirmText').value.trim() !== 'STARTUP';
    const message = !fresh() ? 'Live status is stale. Wait for a successful refresh.' : run?.active ? 'Another Startup is already running. No duplicate request will be submitted.' : uncertain() ? 'Reconciliation is required before Startup can run.' : 'The server revalidates quota, guard, watcher and runtime state at each step.';
    setText('startupModalStatus', message);
  }
  async function refresh(manual = false) {
    if (readPromise) { if (manual) refreshQueued = true; return readPromise; }
    const epoch = mutationEpoch;
    readPromise = (async () => {
      try {
        const payload = await api('/api/startup');
        if (epoch === mutationEpoch) accept(payload, true);
      } catch (error) {
        if (epoch === mutationEpoch) { readError = String(error.message || error); render(); if (manual) notify(readError, true); }
      } finally {
        readPromise = null; render();
        if (refreshQueued) { refreshQueued = false; queueMicrotask(() => refresh()); }
      }
    })();
    render(); return readPromise;
  }
  function previewStartup() {
    if (actionBusy || !fresh() || run?.active || uncertain() || alreadyStarted()) return;
    const retry = !!pendingRequest?.uncertain;
    const count = Number.isInteger(preview?.totalTargets) ? preview.totalTargets : Number.isInteger(preview?.targetCount) ? preview.targetCount : null;
    const quota = readiness?.quota || preview?.quota;
    const remaining = quota?.remainingPercent ?? quota?.remaining;
    const quotaText = Number.isFinite(remaining) && quota?.fresh === true ? remaining + '% currently reported remaining.' : 'Fresh allowance is verified again by the server before launch.';
    openModal('<h2>Start the preserved fleet?</h2><p>This explicitly authorizes releasing the current emergency lock, arming the <strong>7% emergency stop</strong>, and restoring the saved primary and workers. Restored agents may resume work and consume usage. Choose whether to start automatic work after every runtime is verified. No duplicate fleet is created.</p>' +
      '<div class="startup-modal-note"><strong>' + escape(quotaText) + '</strong><p>' + (count === null ? 'The server validates the saved recovery manifest before starting.' : escape(count) + ' preserved session targets reported by the preview.') + '</p><p>No worker restoration is allowed before the stop watcher is disarmed and the quota monitor acknowledges the guard.</p></div>' +
      (retry ? '<p class="startup-modal-warning">The previous request has an unconfirmed response. Retrying reuses its original request ID and options; it does not authorize a duplicate startup.</p>' : '') +
      '<label class="startup-check"><input id="startupWorkOption" type="checkbox" ' + ((retry ? pendingRequest.startWorkAfter === true : true) ? 'checked' : '') + (retry ? ' disabled' : '') + '> Start working after verification (enable Sustained at the saved target)</label>' +
      '<label class="startup-check"><input id="startupOpenUiOption" type="checkbox" ' + ((retry ? pendingRequest.openUi : true) ? 'checked' : '') + (retry ? ' disabled' : '') + '> Open the Orchestrator desktop UI after verification</label>' +
      '<div class="form-field"><label for="startupConfirmText">Type <strong>STARTUP</strong> to confirm</label><input id="startupConfirmText" autocomplete="off" spellcheck="false" placeholder="STARTUP"></div><p id="startupModalStatus" class="startup-modal-status"></p>' +
      '<div class="modal-actions"><button type="button" class="ghost-button" data-modal="cancel">Cancel</button><button type="button" id="startupConfirmButton" class="primary-button" disabled>Start with 7% protection</button></div>');
    modalOpen = true;
    byId('startupConfirmText').addEventListener('input', updateModal);
    byId('startupConfirmButton').addEventListener('click', submitStartup);
    updateModal(); byId('startupConfirmText').focus();
  }
  async function submitStartup() {
    if (actionBusy || !fresh() || run?.active || uncertain() || alreadyStarted() || byId('startupConfirmText')?.value.trim() !== 'STARTUP') return;
    if (!pendingRequest?.uncertain) pendingRequest = { requestId: requestId(), openUi: !!byId('startupOpenUiOption').checked, startWorkAfter: !!byId('startupWorkOption')?.checked, uncertain: false, acknowledged: false };
    pendingRequest.uncertain = true; savePending();
    actionBusy = true; mutationEpoch++; render();
    setText('startupConfirmButton', 'Submitting protected startup…');
    try {
      const response = await api('/api/startup', { confirm: 'STARTUP', requestId: pendingRequest.requestId, openUi: pendingRequest.openUi, startWorkAfter: pendingRequest.startWorkAfter === true });
      accept(response, false); pendingRequest.uncertain = false; pendingRequest.acknowledged = true; savePending();
      closeModal(); modalOpen = false;
      notify(response.idempotent ? 'Existing Startup request recovered. No duplicate launch was requested.' : (pendingRequest.startWorkAfter ? 'Protected Startup accepted. Sustained will start only after all runtimes are verified.' : 'Restore-only Startup accepted. Automatic dispatch will remain paused.'));
    } catch (error) {
      closeModal(); modalOpen = false;
      notify('Startup response unconfirmed: ' + String(error.message || error) + '. Refresh status; any retry will reuse the original request ID.', true);
    } finally { actionBusy = false; render(); refresh(true); }
  }
  async function openOrchestrator() {
    if (actionBusy || !fresh() || run?.active || uncertain()) return;
    actionBusy = true; mutationEpoch++; render();
    try {
      const response = await api('/api/orchestrator/open', {});
      notify(response.note || response.message || 'Orchestrator UI open request completed. This is separate from fleet Startup.');
    } catch (error) { notify('Cannot confirm Orchestrator UI launch: ' + String(error.message || error), true); }
    finally { actionBusy = false; render(); refresh(true); }
  }
  async function cancelStartup() {
    if (actionBusy || !run?.active || !run.runId) return;
    actionBusy = true; mutationEpoch++; const target = run.runId; render();
    try {
      accept(await api('/api/startup/cancel', { runId: target }), false);
      notify('Startup cancellation requested. Check the result; already restored runtimes are not automatically claimed to be stopped.');
    } catch (error) { notify('Cancellation outcome is unconfirmed: ' + String(error.message || error), true); }
    finally { actionBusy = false; render(); refresh(true); }
  }
  async function recheckStartup() {
    if (actionBusy || !uncertain() || !run.runId) return;
    actionBusy = true; mutationEpoch++; const target = run.runId; render();
    try {
      const response = await api('/api/startup/reconcile', { confirm: 'RECHECK STARTUP', runId: target });
      accept(response, false);
      notify(response.note || (uncertain() ? 'Recheck completed; the uncertain operation still needs attention. No agents were started.' : 'Startup state rechecked. No agents were started.'));
    } catch (error) { notify('Recheck could not establish a safe state: ' + String(error.message || error), true); }
    finally { actionBusy = false; render(); refresh(true); }
  }
  byId('startupStart').addEventListener('click', previewStartup);
  byId('startupOpenUi').addEventListener('click', openOrchestrator);
  byId('startupCancel').addEventListener('click', cancelStartup);
  byId('startupRecheck').addEventListener('click', recheckStartup);
  byId('startupRefresh').addEventListener('click', () => refresh(true));
  document.addEventListener('visibilitychange', () => { if (!document.hidden) { render(); refresh(true); } });
  window.addEventListener('focus', () => { render(); refresh(); });
  refresh();
  let pollTimer; const poll = async () => { if (!document.hidden || run?.active) await refresh(); pollTimer = setTimeout(poll, run?.active ? 2000 : 8000); }; pollTimer = setTimeout(poll, 8000);
  const freshnessTimer = setInterval(render, 1000);
  return { refresh, status: () => ({ run: run && structuredClone(run), fresh: fresh(), readError }), dispose: () => { clearTimeout(pollTimer); clearInterval(freshnessTimer); } };
})();
