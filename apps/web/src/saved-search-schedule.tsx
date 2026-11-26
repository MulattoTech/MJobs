import React, { useEffect, useId, useState } from 'react';

export const MIN_SAVED_SEARCH_INTERVAL_SECONDS = 300;
export const MAX_SAVED_SEARCH_INTERVAL_SECONDS = 604800;

export type SavedSearchScheduleWindow = {
  time_zone: string;
  start_local: string;
  end_local: string;
};

export type SavedSearchScheduleValue = {
  enabled: boolean;
  interval: number;
  window: SavedSearchScheduleWindow | null;
};

export type SavedSearchScheduleChange = SavedSearchScheduleValue;

export type SavedSearchSchedulerPayload = {
  enabled: boolean;
  interval_seconds: number;
  window?: SavedSearchScheduleWindow;
};

export function isValidSavedSearchScheduleWindow(window: SavedSearchScheduleWindow | null): boolean {
  if (!window || !window.time_zone.trim() || !/^([01]\d|2[0-3]):[0-5]\d$/.test(window.start_local)
      || !/^([01]\d|2[0-3]):[0-5]\d$/.test(window.end_local)
      || window.start_local === window.end_local) return false;
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: window.time_zone }).format();
    return true;
  } catch {
    return false;
  }
}

export function savedSearchSchedulerPayload(value: SavedSearchScheduleValue): SavedSearchSchedulerPayload {
  return {
    enabled: value.enabled,
    interval_seconds: value.interval,
    ...(value.window ? { window: value.window } : {}),
  };
}

export type SavedSearchScheduleProps = {
  searchName: string;
  enabled: boolean;
  interval: number;
  window?: SavedSearchScheduleWindow | null;
  disabled?: boolean;
  onChange: (value: SavedSearchScheduleChange) => void;
};

function isValidInterval(value: number): boolean {
  return Number.isSafeInteger(value)
    && value >= MIN_SAVED_SEARCH_INTERVAL_SECONDS
    && value <= MAX_SAVED_SEARCH_INTERVAL_SECONDS;
}

function parseInterval(value: string): number | null {
  if (!/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return isValidInterval(parsed) ? parsed : null;
}

export function SavedSearchSchedule({
  searchName,
  enabled,
  interval,
  window = null,
  disabled = false,
  onChange,
}: SavedSearchScheduleProps) {
  const id = useId();
  const [intervalDraft, setIntervalDraft] = useState(() => String(interval));

  useEffect(() => {
    setIntervalDraft(String(interval));
  }, [interval]);

  const parsedInterval = parseInterval(intervalDraft);
  const intervalIsValid = parsedInterval !== null;
  const headingId = `${id}-heading`;
  const descriptionId = `${id}-description`;
  const intervalId = `${id}-interval`;
  const intervalHelpId = `${id}-interval-help`;
  const intervalErrorId = `${id}-interval-error`;
  const windowEnabledId = `${id}-window-enabled`;
  const windowTimeZoneId = `${id}-window-time-zone`;
  const windowStartId = `${id}-window-start`;
  const windowEndId = `${id}-window-end`;
  const windowErrorId = `${id}-window-error`;
  const windowValid = !window || isValidSavedSearchScheduleWindow(window);
  const value = (nextWindow: SavedSearchScheduleWindow | null, nextEnabled = enabled, nextInterval = interval) =>
    onChange({ enabled: nextEnabled, interval: nextInterval, window: nextWindow });

  return <section className="saved-search-schedule" aria-labelledby={headingId}>
    <h3 id={headingId}>Schedule “{searchName}”</h3>
    <p id={descriptionId}>
      This schedule queues local discovery for this saved search. You can turn it off at any time.
      Editing this schedule does not send a provider request.
    </p>
    <label>
      <input
        type="checkbox"
        checked={enabled}
        disabled={disabled}
        aria-describedby={descriptionId}
        onChange={event => value(window, event.currentTarget.checked)}
      />
      Enable scheduled discovery
    </label>
    <div>
      <label htmlFor={intervalId}>Interval (seconds)</label>
      <input
        id={intervalId}
        type="number"
        inputMode="numeric"
        min={MIN_SAVED_SEARCH_INTERVAL_SECONDS}
        max={MAX_SAVED_SEARCH_INTERVAL_SECONDS}
        step={1}
        required
        value={intervalDraft}
        disabled={disabled}
        aria-invalid={!intervalIsValid}
        aria-describedby={`${intervalHelpId}${intervalIsValid ? '' : ` ${intervalErrorId}`}`}
        onChange={event => {
          const nextDraft = event.currentTarget.value;
          setIntervalDraft(nextDraft);
          const nextInterval = parseInterval(nextDraft);
          if (nextInterval !== null) value(window, enabled, nextInterval);
        }}
      />
      <small id={intervalHelpId}>
        Choose a whole number from {MIN_SAVED_SEARCH_INTERVAL_SECONDS} to {MAX_SAVED_SEARCH_INTERVAL_SECONDS} seconds.
      </small>
      {!intervalIsValid && <p id={intervalErrorId} role="alert">
        Enter a whole number of seconds from {MIN_SAVED_SEARCH_INTERVAL_SECONDS} to {MAX_SAVED_SEARCH_INTERVAL_SECONDS}.
      </p>}
    </div>
    <fieldset disabled={disabled}>
      <legend>Local time window</legend>
      <label htmlFor={windowEnabledId}>
        <input
          id={windowEnabledId}
          type="checkbox"
          checked={!!window}
          aria-describedby={descriptionId}
          onChange={event => value(event.currentTarget.checked
            ? window || {
              time_zone: Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC',
              start_local: '08:00',
              end_local: '17:00',
            }
            : null)}
        />
        Limit discovery to a local time window
      </label>
      {window&&<div>
        <label htmlFor={windowTimeZoneId}>IANA time zone</label>
        <input id={windowTimeZoneId} type="text" required value={window.time_zone} placeholder="America/Chicago"
          aria-invalid={!window.time_zone.trim()||!isValidSavedSearchScheduleWindow({...window,start_local:'08:00',end_local:'17:00'})}
          onChange={event=>value({...window,time_zone:event.currentTarget.value})}/>
        <div>
          <label htmlFor={windowStartId}>Starts at (inclusive)</label>
          <input id={windowStartId} type="time" required value={window.start_local}
            aria-invalid={!/^([01]\d|2[0-3]):[0-5]\d$/.test(window.start_local)}
            onChange={event=>value({...window,start_local:event.currentTarget.value})}/>
          <label htmlFor={windowEndId}>Ends at (exclusive)</label>
          <input id={windowEndId} type="time" required value={window.end_local}
            aria-invalid={!/^([01]\d|2[0-3]):[0-5]\d$/.test(window.end_local)||window.start_local===window.end_local}
            onChange={event=>value({...window,end_local:event.currentTarget.value})}/>
        </div>
        {!windowValid&&<p id={windowErrorId} role="alert">Enter a valid IANA time zone, complete local times, and different start and end times. The window may cross midnight.</p>}
        <small>Start is inclusive and end is exclusive. Different times may cross midnight.</small>
      </div>}
    </fieldset>
  </section>;
}
