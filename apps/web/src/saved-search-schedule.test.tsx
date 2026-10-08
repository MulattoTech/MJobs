import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SavedSearchSchedule } from './saved-search-schedule';

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
    expect(html.match(/disabled=""/g)).toHaveLength(2);
    expect(onChange).not.toHaveBeenCalled();
  });
});
