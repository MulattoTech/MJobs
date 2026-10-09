import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  DynamicApplicationFormConnected,
  dynamicApplicationFormDraftStorageKey,
  parseDynamicApplicationFormDraft,
} from './dynamic-application-form-connected';

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

  it('shows loading state without starting a form action during render', () => {
    const html = renderToStaticMarkup(<DynamicApplicationFormConnected
      profileId="profile-a" applicationId="application-a" provider="mock-ats"
    />);
    expect(html).toContain('Loading current profile-scoped form fields');
    expect(html).not.toContain('submit');
  });
});
