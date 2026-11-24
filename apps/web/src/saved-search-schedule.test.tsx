import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  isValidSavedSearchScheduleWindow,
  savedSearchSchedulerPayload,
  SavedSearchSchedule,
} from './saved-search-schedule';

describe('saved search schedule', () => {
  it('renders an accessible, named local discovery schedule with bounded interval controls', () => {
    const html = renderToStaticMarkup(
      <SavedSearchSchedule searchName="Remote IT roles" enabled interval={3600} onChange={() => {}} />,
    );

    expect(html).toContain('Remote IT roles');
    expect(html).toContain('type="checkbox"');
    expect(html).toContain('type="number"');
    expect(html).toContain('min="300"');
    expect(html).toContain('max="604800"');
    expect(html).toContain('value="3600"');
    expect(html).toContain('queues local discovery');
    expect(html).toContain('turn it off at any time');
    expect(html).toContain('does not send a provider request');
    expect(html).toContain('Local time window');
    expect(html).toContain('Limit discovery to a local time window');
  });

  it.each([299, 604801, 300.5, Number.NaN])('explains invalid interval %s accessibly', interval => {
    const html = renderToStaticMarkup(
      <SavedSearchSchedule searchName="Saved search" enabled={false} interval={interval} onChange={() => {}} />,
    );

    expect(html).toContain('aria-invalid="true"');
    expect(html).toContain('role="alert"');
    expect(html).toContain('Enter a whole number of seconds from 300 to 604800.');
  });

  it('disables both controls when requested and never invokes the change handler while rendering', () => {
    const onChange = vi.fn();
    const html = renderToStaticMarkup(
      <SavedSearchSchedule searchName="Saved search" enabled={false} interval={300} disabled onChange={onChange} />,
    );

    expect(html).toContain('type="checkbox"');
    expect(html).toContain('type="number"');
    expect(html.match(/disabled=""/g)).toHaveLength(3);
    expect(onChange).not.toHaveBeenCalled();
  });

  it('accepts valid IANA windows including ones that cross midnight', () => {
    expect(isValidSavedSearchScheduleWindow({
      time_zone: 'America/Chicago', start_local: '22:00', end_local: '06:00',
    })).toBe(true);
    expect(isValidSavedSearchScheduleWindow({
      time_zone: 'Not/A_Time_Zone', start_local: '08:00', end_local: '17:00',
    })).toBe(false);
    expect(isValidSavedSearchScheduleWindow({
      time_zone: 'America/Chicago', start_local: '08:00', end_local: '08:00',
    })).toBe(false);
    expect(isValidSavedSearchScheduleWindow({
      time_zone: 'America/Chicago', start_local: '', end_local: '17:00',
    })).toBe(false);
  });

  it('preserves an opted-in window and omits a cleared window from scheduler replacement', () => {
    expect(savedSearchSchedulerPayload({
      enabled: true, interval: 3600,
      window: { time_zone: 'America/Chicago', start_local: '08:00', end_local: '17:00' },
    })).toEqual({
      enabled: true, interval_seconds: 3600,
      window: { time_zone: 'America/Chicago', start_local: '08:00', end_local: '17:00' },
    });
    expect(savedSearchSchedulerPayload({ enabled: false, interval: 3600, window: null }))
      .toEqual({ enabled: false, interval_seconds: 3600 });
  });
});
