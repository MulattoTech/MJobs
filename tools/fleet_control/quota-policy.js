'use strict';
// One configurable start/new-work limit, separate from emergency termination.
const DEFAULT_MINIMUM = 15;
function minimumRemaining(config = {}) {
  const stop = Number.isInteger(config.thresholdPercent) ? Math.max(7, config.thresholdPercent) : 7;
  if (config.minimumRemainingPercent === undefined) return Math.min(99, stop + 8);
  const value = config.minimumRemainingPercent;
  return Number.isInteger(value) && value >= stop && value <= 99 ? value : 100;
}
function policyStatus(config, sample) {
  const limit = minimumRemaining(config), stop = Number.isInteger(config.thresholdPercent) ? config.thresholdPercent : 7;
  const remaining = sample.selectedRemainingPercent;
  const fresh = sample.fresh === true && Number.isFinite(remaining) && remaining >= 0 && remaining <= 100;
  const blocked = !fresh || remaining <= limit || sample.ordinaryUsageAllowed === false;
  return { minimumRemainingPercent: limit, emergencyStopPercent: stop, remainingPercent: fresh ? remaining : null,
    fresh, blocked, configurable: true, comparison: 'remaining must be greater than the start/new-work limit',
    reason: !fresh ? 'Quota unavailable or stale; new work is blocked.' : sample.ordinaryUsageAllowed === false ? 'Provider usage is unavailable.' :
      remaining <= limit ? `QUOTA HOLD: ${remaining}% remaining; start/new work requires more than ${limit}%. Emergency stop remains ${stop}%.` :
        `${remaining}% remaining; start/new work allowed above ${limit}%. Emergency stop: ${stop}%.` };
}
module.exports = { DEFAULT_MINIMUM, minimumRemaining, policyStatus };
