'use strict';

const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { performance } = require('node:perf_hooks');
const { execFile } = require('node:child_process');
const { promisify } = require('node:util');
const execFileAsync = promisify(execFile);
const TASK_NAME = 'MJobs Fleet - Orchestrator Standard';
const active = new Map();
const NONCE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

class LaunchError extends Error {
  constructor(code, message, details = null) {
    super(message); this.name = 'OrchestratorLaunchError';
    this.code = code; this.details = details;
  }
}

function readJson(file) {
  try { return JSON.parse(fs.readFileSync(file, 'utf8')); } catch { return null; }
}

function writeAtomic(file, value) {
  const temporary = file + '.' + process.pid + '.' + crypto.randomUUID() + '.tmp';
  try {
    fs.writeFileSync(temporary, JSON.stringify(value, null, 2), { flag: 'wx', mode: 0o600 });
    fs.renameSync(temporary, file);
  } finally {
    try { fs.unlinkSync(temporary); } catch (error) { if (error.code !== 'ENOENT') throw error; }
  }
}

function quotedPs(value) { return "'" + String(value).replace(/'/g, "''") + "'"; }

async function defaultRunTask({ root, request, signal }) {
  if (signal?.aborted) return { started: false, code: 'REQUEST_ABORTED' };
  if (process.platform !== 'win32') {
    return { started: false, code: 'WINDOWS_REQUIRED', error: 'Task Scheduler launcher requires Windows.' };
  }
  const expectedVbs = path.join(root, 'Start-Orchestrator-Requested.vbs');
  const script = [
    "$ErrorActionPreference = 'Stop'",
    '$started = $false',
    '$triggerAttempted = $false',
    'try {',
    '  $task = Get-ScheduledTask -TaskName ' + quotedPs(TASK_NAME) + ' -ErrorAction Stop',
    "  if ([string]$task.Principal.RunLevel -ne 'Limited') { throw 'TASK_NOT_LIMITED' }",
    "  if ([string]$task.Principal.LogonType -notin @('Interactive','InteractiveToken')) { throw 'TASK_NOT_INTERACTIVE' }",
    "  if (@($task.Actions).Count -ne 1) { throw 'TASK_ACTION_MISMATCH' }",
    "  $expectedHost = Join-Path $env:WINDIR 'System32\\wscript.exe'",
    "  if (-not [string]::Equals([IO.Path]::GetFullPath($task.Actions[0].Execute), $expectedHost, [StringComparison]::OrdinalIgnoreCase)) { throw 'TASK_HOST_MISMATCH' }",
    '  $expectedVbs = ' + quotedPs(expectedVbs),
    "  $expectedArguments = '//B //NoLogo " + '"' + "' + $expectedVbs + '" + '"' + "'",
    "  if (-not [string]::Equals([string]$task.Actions[0].Arguments, $expectedArguments, [StringComparison]::OrdinalIgnoreCase)) { throw 'TASK_ARGUMENTS_MISMATCH' }",
    '  $waitUntil = [DateTimeOffset]::UtcNow.AddSeconds(3)',
    "  while ([string]$task.State -eq 'Running' -and [DateTimeOffset]::UtcNow -lt $waitUntil) {",
    '    Start-Sleep -Milliseconds 100',
    '    $task = Get-ScheduledTask -TaskName ' + quotedPs(TASK_NAME) + ' -ErrorAction Stop',
    '  }',
    "  if ([string]$task.State -eq 'Running') { throw 'TASK_ALREADY_RUNNING' }",
    '  $cancelPath = ' + quotedPs(path.join(root, 'orchestrator-launch-cancel.json')),
    '  if (Test-Path -LiteralPath $cancelPath) {',
    '    $cancel = Get-Content -LiteralPath $cancelPath -Raw -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop',
    '    if ($cancel.version -eq 1 -and $cancel.nonce -ceq ' + quotedPs(request.nonce) + ' -and $cancel.mode -ceq ' + quotedPs(request.mode) + ") { throw 'REQUEST_ABORTED' }",
    '  }',
    '  $triggerAttempted = $true',
    '  Start-ScheduledTask -TaskName ' + quotedPs(TASK_NAME) + ' -ErrorAction Stop',
    '  $started = $true',
    '  @{started=$true} | ConvertTo-Json -Compress',
    '} catch {',
    "  $code = if ($_.Exception.Message -match '^(?:TASK_[A-Z_]+|REQUEST_ABORTED)$') { $_.Exception.Message } else { 'TASK_TRIGGER_FAILED' }",
    "  if ($triggerAttempted -or $code -eq 'TASK_ALREADY_RUNNING') { $started = $null }",
    '  @{started=$started;code=$code;error=$code} | ConvertTo-Json -Compress',
    '}'
  ].join('\r\n');
  const powershell = path.join(process.env.WINDIR || 'C:\\Windows', 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe');
  const result = await execFileAsync(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', script], {
    windowsHide: true, timeout: 6500, maxBuffer: 65536, cwd: root
  });
  let acknowledgement;
  try { acknowledgement = JSON.parse(String(result.stdout).trim()); } catch {
    throw new LaunchError('TASK_ACK_UNCERTAIN', 'The task trigger did not return an unambiguous acknowledgement.');
  }
  if (acknowledgement.started !== true && acknowledgement.started !== false) {
    throw new LaunchError('TASK_ACK_UNCERTAIN', 'The task trigger acknowledgement is invalid.');
  }
  return acknowledgement;
}

function matchingResult(request, result) {
  return !!request && !!result && result.version === 1 && result.nonce === request.nonce &&
    result.mode === request.mode && Number.isFinite(Date.parse(result.completedAt)) &&
    Date.parse(result.completedAt) >= Date.parse(request.requestedAt) - 5000;
}

function matchingCancellation(request, cancellation) {
  return !!request && cancellation?.version === 1 && cancellation.nonce === request.nonce && cancellation.mode === request.mode;
}

function successfulResult(result) {
  const processReady = processInfo => !!processInfo && Number.isInteger(processInfo.pid) && processInfo.pid > 0 &&
    processInfo.isElevated === false && processInfo.integrityRid === 8192 &&
    Number.isInteger(processInfo.sessionId) && processInfo.sessionId > 0 && processInfo.sessionId === result.sessionId;
  return result.status === 'ok' && result.isElevated === false && result.integrityRid === 8192 &&
    Number.isInteger(result.pid) && result.pid > 0 && Number.isInteger(result.sessionId) && result.sessionId > 0 &&
    (result.mode === 'daemon'
      ? result.daemon?.verified === true && result.daemon.present === true && result.daemon.healthy === true && processReady(result.daemon)
      : result.ui?.verified === true && result.ui.running === true && processReady(result.ui));
}

class OrchestratorLauncher {
  constructor(options = {}) {
    if (!options.root || !path.isAbsolute(options.root)) throw new TypeError('An absolute launcher root is required.');
    this.root = path.resolve(options.root);
    this.requestPath = path.join(this.root, 'orchestrator-launch-request.json');
    this.resultPath = path.join(this.root, 'orchestrator-launch-result.json');
    this.lockPath = path.join(this.root, 'orchestrator-launch-pending.json');
    this.cancelPath = path.join(this.root, 'orchestrator-launch-cancel.json');
    this.runTask = options.runTask || defaultRunTask;
    this.now = options.now || Date.now;
    this.monotonic = options.monotonic || (() => performance.now());
    this.sleep = options.sleep || (milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds)));
    this.timeoutMs = Math.min(25000, Math.max(10, options.timeoutMs ?? 25000));
    this.pollMs = Math.min(1000, Math.max(1, options.pollMs ?? 200));
  }

  status() {
    const request = readJson(this.requestPath);
    const pending = readJson(this.lockPath);
    const hasLock = fs.existsSync(this.lockPath);
    const result = readJson(this.resultPath);
    const matched = matchingResult(request, result);
    const pendingMatches = pending?.version === 1 && pending.nonce === request?.nonce && pending.mode === request?.mode;
    const cancelled = matchingCancellation(request, readJson(this.cancelPath)) ||
      (pending?.nonce === request?.nonce && pending?.mode === request?.mode && pending?.cancelRequested === true);
    return {
      taskName: TASK_NAME, pending: hasLock,
      state: hasLock && (!pendingMatches || cancelled) ? 'uncertain' : matched ? (successfulResult(result) ? 'ok' : result.status === 'ok' ? 'uncertain' : result.status)
        : hasLock ? (active.has(this.root) ? 'pending' : 'uncertain') : 'idle',
      request, result: matched ? result : null,
      cancelRequested: cancelled,
      requiresReconciliation: hasLock && (!pendingMatches || cancelled || (!active.has(this.root) && (!matched || (result.status !== 'ok' && result.safeToRetry !== true) || (result.status === 'ok' && !successfulResult(result)))))
    };
  }

  reconcile(options = {}) {
    if (!options || typeof options !== 'object' || Array.isArray(options) ||
      (options.acceptCompletedCancellation !== undefined && typeof options.acceptCompletedCancellation !== 'boolean')) {
      throw new TypeError('Reconcile options require a boolean acceptCompletedCancellation.');
    }
    const pending = readJson(this.lockPath);
    if (!pending) return this.status();
    const request = readJson(this.requestPath);
    const result = readJson(this.resultPath);
    const cancelled = matchingCancellation(request, readJson(this.cancelPath)) ||
      (pending?.nonce === request?.nonce && pending?.mode === request?.mode && pending?.cancelRequested === true);
    // Explicit user RECHECK may accept a completed cancelled operation only
    // after a matching, fully verified terminal success proves its outcome.
    // Bootstrap/automatic reconciliation does not grant that acknowledgement.
    // No task is launched and request/result/cancellation records stay intact.
    if (pending.version === 1 && pending.nonce === request?.nonce && pending.mode === request?.mode && matchingResult(request, result) &&
      (((!cancelled || options.acceptCompletedCancellation === true) && successfulResult(result)) ||
        (['error', 'rejected'].includes(result.status) && result.safeToRetry === true))) {
      this.releaseLock(pending.nonce);
    }
    return this.status();
  }

  releaseLock(nonce) {
    const pending = readJson(this.lockPath);
    if (pending?.nonce === nonce) fs.unlinkSync(this.lockPath);
  }

  launch(mode, options = {}) {
    if (!['daemon', 'ui'].includes(mode)) return Promise.reject(new LaunchError('INVALID_MODE', 'Launch mode must be daemon or ui.'));
    if (!options || typeof options !== 'object' || Array.isArray(options) || (options.beforeTrigger !== undefined && typeof options.beforeTrigger !== 'function') ||
      (options.signal !== undefined && (!options.signal || typeof options.signal.aborted !== 'boolean' ||
        typeof options.signal.addEventListener !== 'function' || typeof options.signal.removeEventListener !== 'function'))) {
      return Promise.reject(new TypeError('Launch options require an AbortSignal and/or beforeTrigger function.'));
    }
    if (options.signal?.aborted) return Promise.reject(new LaunchError('LAUNCH_ABORTED', 'Launch was cancelled before a task request was created.'));
    const running = active.get(this.root);
    if (running) {
      // Only identical ownership/check contexts may share a pending request.
      // A second caller must not silently bypass its own safety callback.
      if (running.mode === mode && running.signal === options.signal && running.beforeTrigger === options.beforeTrigger) return running.promise;
      return Promise.reject(new LaunchError('LAUNCH_BUSY', 'Another Orchestrator launch is still being verified.'));
    }
    const promise = this.launchExclusive(mode, options).finally(() => {
      if (active.get(this.root)?.promise === promise) active.delete(this.root);
    });
    active.set(this.root, { mode, promise, signal: options.signal, beforeTrigger: options.beforeTrigger });
    return promise;
  }

  async launchExclusive(mode, options) {
    this.reconcile();
    const request = { version: 1, nonce: crypto.randomUUID(), mode, requestedAt: new Date(this.now()).toISOString() };
    if (!NONCE.test(request.nonce)) throw new LaunchError('INVALID_NONCE', 'Request identity generation failed.');
    try {
      fs.writeFileSync(this.lockPath, JSON.stringify(request), { flag: 'wx', mode: 0o600 });
    } catch (error) {
      if (error.code === 'EEXIST') throw new LaunchError('LAUNCH_UNCERTAIN', 'A previous launch has no verified final outcome. Inspect/reconcile it; no second task was started.', this.status());
      throw error;
    }
    try { writeAtomic(this.requestPath, request); } catch (error) { this.releaseLock(request.nonce); throw error; }

    const deadline = this.monotonic() + this.timeoutMs;
    const signal = options.signal;
    let triggerMayHaveOccurred = false;
    let cancellationWritten = false;
    let cancellationLockMarked = false;
    let cancellationWriteFailed = false;
    let triggerError = null;
    const publishCancellation = () => {
      const cancelledAt = new Date(this.now()).toISOString();
      if (!cancellationWritten) {
        try {
          writeAtomic(this.cancelPath, { version: 1, nonce: request.nonce, mode,
            cancelledAt, reason: 'REQUEST_ABORTED' });
          cancellationWritten = true;
        } catch { cancellationWriteFailed = true; }
      }
      // A separate cancellation-file failure must not let a late success
      // silently make the cancelled operation retryable after a restart.
      if (!cancellationLockMarked) {
        try {
          const pending = readJson(this.lockPath);
          if (pending?.nonce === request.nonce && pending.mode === mode) {
            writeAtomic(this.lockPath, { ...pending, cancelRequested: true, cancelledAt });
            cancellationLockMarked = true;
          }
        } catch { cancellationWriteFailed = true; }
      }
    };
    const checkCancelled = () => {
      if (!signal?.aborted) return;
      publishCancellation();
      throw new LaunchError('REQUEST_ABORTED', 'Launch cancellation requested.');
    };
    const uncertainDetails = () => ({ ...this.status(), state: 'uncertain', requiresReconciliation: true,
      cancelRequested: signal?.aborted === true || matchingCancellation(request, readJson(this.cancelPath)),
      ...(cancellationWriteFailed ? { cancellationError: 'CANCELLATION_WRITE_FAILED' } : {}) });
    const rejectBeforeTrigger = code => {
      const result = { version: 1, nonce: request.nonce, mode, status: 'rejected',
        completedAt: new Date(this.now()).toISOString(), safeToRetry: true,
        error: code, isElevated: null, integrityRid: null, pid: null };
      writeAtomic(this.resultPath, result); this.releaseLock(request.nonce);
      return result;
    };
    const awaitStage = async (operation, timeoutCode) => {
      checkCancelled();
      let timer, onAbort;
      try {
        const competitors = [Promise.resolve().then(() => { checkCancelled(); return operation(); }),
          new Promise((resolve, reject) => {
            timer = setTimeout(() => reject(new LaunchError(timeoutCode, 'Launch stage timed out.')),
              Math.max(1, deadline - this.monotonic()));
          })];
        if (signal) competitors.push(new Promise((resolve, reject) => {
          onAbort = () => reject(new LaunchError('REQUEST_ABORTED', 'Launch cancellation requested.'));
          signal.addEventListener('abort', onAbort, { once: true });
          if (signal.aborted) onAbort();
        }));
        const value = await Promise.race(competitors);
        checkCancelled();
        return value;
      } finally {
        clearTimeout(timer);
        if (onAbort) signal.removeEventListener('abort', onAbort);
      }
    };
    signal?.addEventListener('abort', publishCancellation, { once: true });
    try {
      try {
        checkCancelled();
        if (options.beforeTrigger) {
          const allowed = await awaitStage(() => options.beforeTrigger({ request, signal }), 'PRE_TRIGGER_CHECK_TIMEOUT');
          if (allowed === false) throw new LaunchError('PRE_TRIGGER_CHECK_FAILED', 'Pre-trigger safety check refused launch.');
        }
        checkCancelled();
      } catch (error) {
        const aborted = signal?.aborted === true || error.code === 'REQUEST_ABORTED';
        if (aborted) publishCancellation();
        const result = rejectBeforeTrigger(aborted ? 'REQUEST_ABORTED' : 'PRE_TRIGGER_CHECK_FAILED');
        throw new LaunchError(aborted ? 'LAUNCH_ABORTED' : 'LAUNCH_REJECTED',
          'The task was not triggered because cancellation or a pre-trigger safety check blocked it.', result);
      }

      try {
        // The adapter may start ONLY the fixed Limited/Interactive task.
        // Mark inside the operation, after its final cancellation check, so
        // cancellation before that point remains a proven no-trigger outcome.
        const acknowledgement = await awaitStage(() => {
          checkCancelled();
          triggerMayHaveOccurred = true;
          return this.runTask({ taskName: TASK_NAME, root: this.root, request, signal });
        }, 'TASK_ACK_UNCERTAIN');
        if (acknowledgement?.started === false) {
          const result = rejectBeforeTrigger(acknowledgement.code || 'TASK_NOT_STARTED');
          throw new LaunchError('TASK_NOT_STARTED', 'The standard-user task was not started: ' + result.error, result);
        }
        if (acknowledgement?.started !== true) triggerError = 'The task trigger outcome was ambiguous.';
      } catch (error) {
        if (error.code === 'TASK_NOT_STARTED') throw error;
        if (signal?.aborted || error.code === 'REQUEST_ABORTED') throw error;
        triggerError = 'The task trigger did not provide a verified outcome.';
      }

      while (this.monotonic() <= deadline) {
        checkCancelled();
        const result = readJson(this.resultPath);
        if (matchingResult(request, result)) {
          if (successfulResult(result)) { this.releaseLock(request.nonce); return { ...result, ok: true }; }
          if (['error', 'rejected', 'busy'].includes(result.status)) {
            if (result.safeToRetry === true && result.status !== 'busy') this.releaseLock(request.nonce);
            throw new LaunchError(result.safeToRetry === true ? 'LAUNCH_REJECTED' : 'LAUNCH_UNCERTAIN',
              'Orchestrator launch was not verified: ' + String(result.error || result.status).slice(0, 180), result);
          }
        }
        try {
          await awaitStage(() => this.sleep(Math.min(this.pollMs, Math.max(1, deadline - this.monotonic()))), 'LAUNCH_WAIT_TIMEOUT');
        } catch (error) {
          if (error.code === 'LAUNCH_WAIT_TIMEOUT') break;
          throw error;
        }
      }
      throw new LaunchError('LAUNCH_UNCERTAIN',
        (triggerError || 'No matching verified launch result arrived before the deadline.') +
        ' The pending request is preserved; no automatic launch retry will be attempted.', uncertainDetails());
    } catch (error) {
      if (error.code === 'LAUNCH_ABORTED' && !triggerMayHaveOccurred) throw error;
      if (signal?.aborted || error.code === 'REQUEST_ABORTED') {
        publishCancellation();
        if (!triggerMayHaveOccurred) {
          const result = rejectBeforeTrigger('REQUEST_ABORTED');
          throw new LaunchError('LAUNCH_ABORTED', 'Launch was cancelled before the task trigger.', result);
        }
        throw new LaunchError('LAUNCH_UNCERTAIN',
          'Cancellation was requested after the task trigger may have occurred. The nonce-bound cancellation and pending request are preserved; no retry was attempted.', uncertainDetails());
      }
      throw error;
    } finally { signal?.removeEventListener('abort', publishCancellation); }
  }
}

module.exports = { OrchestratorLauncher, LaunchError, TASK_NAME, writeAtomic, matchingResult, matchingCancellation, successfulResult, defaultRunTask };
