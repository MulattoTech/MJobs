import React, { useEffect, useId, useState } from 'react';

export const MIN_SAVED_SEARCH_INTERVAL_SECONDS = 300;
export const MAX_SAVED_SEARCH_INTERVAL_SECONDS = 604800;

export type SavedSearchScheduleChange = {
  enabled: boolean;
  interval: number;
};

export type SavedSearchScheduleProps = {
  searchName: string;
  enabled: boolean;
  interval: number;
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
        onChange={event => onChange({enabled: event.currentTarget.checked, interval})}
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
          if (nextInterval !== null) onChange({enabled, interval: nextInterval});
        }}
      />
      <small id={intervalHelpId}>
        Choose a whole number from {MIN_SAVED_SEARCH_INTERVAL_SECONDS} to {MAX_SAVED_SEARCH_INTERVAL_SECONDS} seconds.
      </small>
      {!intervalIsValid && <p id={intervalErrorId} role="alert">
        Enter a whole number of seconds from {MIN_SAVED_SEARCH_INTERVAL_SECONDS} to {MAX_SAVED_SEARCH_INTERVAL_SECONDS}.
      </p>}
    </div>
  </section>;
}
