import React from 'react';
import { describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  CompanyPreferences,
  CompanyPreferenceView,
  confirmCompanyPreference,
  type CompanyPreferenceClient,
  type CompanyPreferenceReadback,
  type CompanyPreferenceStatus,
  type CompanyPreferenceViewProps,
} from './company-preferences';

const saved: CompanyPreferenceReadback = {
  profile_id: 'profile-1', company_id: 'company-1', status: 'BLACKLISTED', updated_at: '2026-10-08T12:30:00Z',
};
const noop = () => undefined;
const baseView: CompanyPreferenceViewProps = {
  profileId: 'profile-1', companyId: 'company-1', companyName: 'Northwind Systems',
  saved, selectedStatus: 'BLACKLISTED', loading: false, saving: false, error: null,
  errorOperation: null, saveConfirmed: false, onStatusChange: noop, onSave: noop, onRetry: noop,
};
const render = (props: Partial<CompanyPreferenceViewProps> = {}) => renderToStaticMarkup(
  <CompanyPreferenceView {...baseView} {...props} />,
);

describe('CompanyPreferences', () => {
  it('shows durable discovery review separately from a saved preference', () => {
    const html = render({ saved: { ...saved, status: 'PREFERRED', discovery: {
      request_id: 'request-1', status: 'NEEDS_ORIGIN_REVIEW', reason: 'EMPLOYER_ORIGIN_UNVERIFIED',
    } } });
    expect(html).toContain('Employer discovery:');
    expect(html).toContain('NEEDS_ORIGIN_REVIEW');
    expect(html).toContain('Sources');
    expect(html).not.toContain('sorting only');
  });
  it('shows the persisted enum and accessible explicit choice for each preference state', () => {
    const labels: Record<CompanyPreferenceStatus, string> = {
      BLACKLISTED: 'Avoid this company', PREFERRED: 'Prefer this company', NEUTRAL: 'No preference',
    };
    for (const status of Object.keys(labels) as CompanyPreferenceStatus[]) {
      const record = { ...saved, status };
      const html = render({ saved: record, selectedStatus: status });
      expect(html).toContain(`<p>Saved preference: <strong>${status}</strong> — ${labels[status]}.</p>`);
      expect(html).toContain(labels[status]);
      expect(html).toContain(`checked="" value="${status}"`);
      expect(html).toContain('Blacklisted companies are omitted from job search');
    }
  });

  it('renders an accessible loading state before the selected company preference is read', () => {
    const html = render({ saved: null, selectedStatus: null, loading: true });
    expect(html).toContain('aria-label="Company preference"');
    expect(html).toContain('role="status"');
    expect(html).toContain('Loading saved company preference');
    expect(html).toContain('disabled=""');
    expect(html).not.toContain('Saved preference:');
  });

  it('labels an unsaved initial state as neutral default without inventing a save time', () => {
    const html = render({ saved: { ...saved, status: 'NEUTRAL', updated_at: null }, selectedStatus: 'NEUTRAL' });
    expect(html).toContain('No explicit choice has been saved; the current default is neutral.');
    expect(html).not.toContain('Last confirmed at:');
  });

  it('keeps the last confirmed value and offers a read retry after a GET failure', () => {
    const html = render({ error: 'API unavailable', errorOperation: 'get' });
    expect(html).toContain('role="alert"');
    expect(html).toContain('API unavailable');
    expect(html).toContain('Last confirmed preference remains shown');
    expect(html).toContain('Retry company preference');
    expect(html).toContain('<p>Last confirmed preference: <strong>BLACKLISTED</strong> — Avoid this company.</p>');
  });

  it('keeps the last confirmed value and offers an explicit save retry after a PUT failure', () => {
    const html = render({ selectedStatus: 'PREFERRED', error: 'Readback unavailable', errorOperation: 'set' });
    expect(html).toContain('Could not confirm the saved preference');
    expect(html).toContain('<p>Last confirmed preference: <strong>BLACKLISTED</strong> — Avoid this company.</p>');
    expect(html).toContain('Retry save preference');
    expect(html).not.toContain('Saved preference: PREFERRED');
  });

  it('uses the set response as persisted readback and does not save on initial render', async () => {
    const readback = { ...saved, status: 'PREFERRED' as const, updated_at: '2026-10-09T09:00:00Z' };
    const client: CompanyPreferenceClient = {
      get: vi.fn(async () => saved),
      set: vi.fn(async (_profile, _company, _status) => readback),
    };
    const html = renderToStaticMarkup(<CompanyPreferences profileId="profile-1" companyId="company-1" companyName="Northwind Systems" client={client} />);
    expect(client.set).not.toHaveBeenCalled();
    expect(html).toContain('Loading saved company preference');

    const result = await confirmCompanyPreference(client, 'profile-1', 'company-1', 'PREFERRED');
    expect(client.set).toHaveBeenCalledExactlyOnceWith('profile-1', 'company-1', 'PREFERRED');
    expect(result).toEqual(readback);
    expect(result.status).toBe('PREFERRED');
  });

  it('rejects a save response for another company or status instead of displaying it as saved', async () => {
    const client: CompanyPreferenceClient = {
      get: vi.fn(async () => saved),
      set: vi.fn(async () => ({ ...saved, company_id: 'company-other', status: 'BLACKLISTED' as const })),
    };
    await expect(confirmCompanyPreference(client, 'profile-1', 'company-1', 'PREFERRED'))
      .rejects.toThrow('did not confirm the selected profile, company, status, and update time');
  });
});
