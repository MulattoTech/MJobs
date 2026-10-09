import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  DynamicApplicationFormConnected,
  clearDynamicApplicationFormDeviceDraft,
  loadDynamicApplicationFormDeviceDraft,
  dynamicApplicationFormDraftStorageKey,
  dynamicApplicationFormDeviceDraftStorageKey,
  saveDynamicApplicationFormDeviceDraft,
  parseDynamicApplicationFormDraft,
} from './dynamic-application-form-connected';

function memoryStorage(): Storage {
  const values = new Map<string, string>();
  return {
    get length() { return values.size; },
    clear: () => values.clear(),
    getItem: key => values.get(key) ?? null,
    key: index => [...values.keys()][index] ?? null,
    removeItem: key => { values.delete(key); },
    setItem: (key, value) => { values.set(key, String(value)); },
  };
}

describe('connected dynamic application review scope and recovery', () => {
  it('restores only the exact profile, application, and form fingerprint', () => {
    const draft = {
      profile_id: 'profile-a', application_id: 'application-a', fingerprint: 'f'.repeat(64),
      answers: { country: 'United States' },
    };

    expect(parseDynamicApplicationFormDraft(draft, 'profile-a', 'application-a', 'f'.repeat(64)))
      .toEqual({ profile_id: 'profile-a', application_id: 'application-a', answers: draft.answers });
    expect(parseDynamicApplicationFormDraft(draft, 'profile-b', 'application-a', 'f'.repeat(64)))
      .toBeUndefined();
    expect(parseDynamicApplicationFormDraft(draft, 'profile-a', 'application-a', 'e'.repeat(64)))
      .toBeUndefined();
  });

  it('uses distinct local recovery keys for profiles, applications, and metadata revisions', () => {
    const key = dynamicApplicationFormDraftStorageKey('profile/a', 'application-a', 'f'.repeat(64));
    expect(key).toContain('profile%2Fa');
    expect(key).not.toBe(dynamicApplicationFormDraftStorageKey('profile-b', 'application-a', 'f'.repeat(64)));
    expect(key).not.toBe(dynamicApplicationFormDraftStorageKey('profile/a', 'application-a', 'e'.repeat(64)));
  });

  it('saves an explicitly selected device draft without storing review approval', () => {
    const storage = memoryStorage();
    const fingerprint = 'f'.repeat(64);
    const draft = { answers: { country: 'United States' }, enabled_optional_sections: ['benefits'] };
    const key = saveDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-a', fingerprint, draft);

    expect(key).toBe(dynamicApplicationFormDeviceDraftStorageKey('profile-a', 'application-a', fingerprint));
    expect(loadDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-a', fingerprint))
      .toEqual({ profile_id: 'profile-a', application_id: 'application-a', ...draft });
    expect(storage.getItem(key)).not.toContain('reviewed');
  });

  it('does not restore device answers across profile, application, or form revisions', () => {
    const storage = memoryStorage();
    const fingerprint = 'f'.repeat(64);
    saveDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-a', fingerprint,
      { answers: { country: 'United States' } });

    expect(loadDynamicApplicationFormDeviceDraft(storage, 'profile-b', 'application-a', fingerprint)).toBeUndefined();
    expect(loadDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-b', fingerprint)).toBeUndefined();
    expect(loadDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-a', 'e'.repeat(64))).toBeUndefined();
  });

  it('clears only the selected profile, application, and form device draft', () => {
    const storage = memoryStorage();
    const fingerprint = 'f'.repeat(64);
    const key = saveDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-a', fingerprint,
      { answers: { country: 'United States' } });
    saveDynamicApplicationFormDeviceDraft(storage, 'profile-b', 'application-a', fingerprint,
      { answers: { country: 'Canada' } });

    clearDynamicApplicationFormDeviceDraft(storage, 'profile-a', 'application-a', fingerprint);

    expect(storage.getItem(key)).toBeNull();
    expect(loadDynamicApplicationFormDeviceDraft(storage, 'profile-b', 'application-a', fingerprint)?.answers.country)
      .toBe('Canada');
  });

  it('rejects reserved answer keys and oversized restored payloads', () => {
    const fingerprint = 'f'.repeat(64);
    expect(parseDynamicApplicationFormDraft({profile_id: 'profile-a', application_id: 'application-a', fingerprint,
      answers: JSON.parse('{"__proto__":"unsafe"}')}, 'profile-a', 'application-a', fingerprint)).toBeUndefined();
    expect(parseDynamicApplicationFormDraft({profile_id: 'profile-a', application_id: 'application-a', fingerprint,
      answers: {country: 'x'.repeat(256_001)}}, 'profile-a', 'application-a', fingerprint)).toBeUndefined();
  });

  it('shows loading state without starting a form action during render', () => {
    const html = renderToStaticMarkup(<DynamicApplicationFormConnected
      profileId="profile-a" applicationId="application-a" provider="mock-ats"
    />);
    expect(html).toContain('Loading current profile-scoped form fields');
    expect(html).not.toContain('submit');
  });
});
