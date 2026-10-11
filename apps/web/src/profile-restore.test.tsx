import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  parseRestoreBackup,
  ProfileRestore,
  ProfileRestorePreviewCard,
  requestRestorePreview,
  submitProfileRestore,
  MAX_PROFILE_RESTORE_BYTES,
} from './profile-restore';

const backup = {
  schema_version: 1,
  export_type: 'profile_backup',
  profile_id: 'source-1',
  profile: { id: 'source-1', name: 'PRIVATE_SOURCE_NAME', email: 'private@example.test' },
  facts: [{ text: 'PRIVATE_FACT' }],
};
const preview = {
  fingerprint: 'a'.repeat(64),
  source_profile: { name: 'Source Person', email: 'source@example.test' },
  counts: { facts: 2, answers: 1, uploaded_files: 1, search_profiles: 1 },
  warnings: ['IMPORTED_FACTS_REQUIRE_REVIEW', 'SEARCH_PROFILES_DISABLED', 'UPLOADED_FILES_NOT_INCLUDED'],
  omitted_sections: ['job_history', 'application_history', 'ai_evaluations'],
  imported_private_payload: 'DO_NOT_DISPLAY',
};

const api = vi.fn();

describe('profile restore', () => {
  it('accepts only bounded supported JSON backups and never puts invalid contents in errors', async () => {
    const valid = await parseRestoreBackup(new Blob([JSON.stringify(backup)]));
    expect(valid.ok).toBe(true);

    const malformed = await parseRestoreBackup(new Blob(['PRIVATE_BROKEN_BACKUP']));
    expect(malformed).toEqual({ ok: false, reason: 'invalid_json' });
    const tooLarge = await parseRestoreBackup(new Blob([' '.repeat(MAX_PROFILE_RESTORE_BYTES + 1)]));
    expect(tooLarge).toEqual({ ok: false, reason: 'file_too_large' });
    const unsupported = await parseRestoreBackup(new Blob([JSON.stringify({ schema_version: 2, payload: 'PRIVATE' })]));
    expect(unsupported).toEqual({ ok: false, reason: 'unsupported_format' });
    expect(JSON.stringify([malformed, tooLarge, unsupported])).not.toContain('PRIVATE');
  });

  it('previews only fixed summary fields and posts the backup to the preview route', async () => {
    const request = vi.fn(async () => preview);

    const result = await requestRestorePreview(request, backup);

    expect(request).toHaveBeenCalledExactlyOnceWith('/profile-restores/preview', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ backup }),
    });
    expect(result).toEqual({
      fingerprint: 'a'.repeat(64),
      sourceProfile: { name: 'Source Person', email: 'source@example.test' },
      counts: { facts: 2, answers: 1, uploaded_files: 1, search_profiles: 1 },
      warnings: ['IMPORTED_FACTS_REQUIRE_REVIEW', 'SEARCH_PROFILES_DISABLED', 'UPLOADED_FILES_NOT_INCLUDED'],
      omittedSections: ['job_history', 'application_history', 'ai_evaluations'],
    });
    expect(JSON.stringify(result)).not.toContain('DO_NOT_DISPLAY');
    expect(JSON.stringify(result)).not.toContain('PRIVATE_FACT');
  });

  it('rejects unknown preview codes and hides provider error details', async () => {
    const invalidPreview = await requestRestorePreview(vi.fn(async () => ({
      ...preview,
      warnings: ['PRIVATE_SERVER_DETAIL'],
    })), backup);
    const failedRequest = await requestRestorePreview(vi.fn(async () => {
      throw new Error('PRIVATE_SERVER_DETAIL');
    }), backup);

    expect(invalidPreview).toBeNull();
    expect(failedRequest).toBeNull();
    expect(JSON.stringify([invalidPreview, failedRequest])).not.toContain('PRIVATE_SERVER_DETAIL');
  });

  it('requires explicit confirmation and retries with the same request ID', async () => {
    const request = vi.fn(async () => ({ profile_id: 'new-profile', request_id: 'restore-request-1' }));

    const first = await submitProfileRestore(request, backup, 'restore-request-1', 'a'.repeat(64), true);
    const retry = await submitProfileRestore(request, backup, 'restore-request-1', 'a'.repeat(64), true);
    const unconfirmed = await submitProfileRestore(request, backup, 'restore-request-1', 'a'.repeat(64), false);

    expect(first).toEqual({ ok: true, profileId: 'new-profile' });
    expect(retry).toEqual(first);
    expect(unconfirmed).toEqual({ ok: false, reason: 'confirmation_required' });
    expect(request).toHaveBeenCalledTimes(2);
    expect(request).toHaveBeenNthCalledWith(1, '/profile-restores', {
      headers: { 'Content-Type': 'application/json' },
      method: 'POST', body: JSON.stringify({
        backup, request_id: 'restore-request-1', expected_fingerprint: 'a'.repeat(64), confirmed: true,
      }),
    });
  });

  it('shows omissions, verification reset, a new-profile warning, and no imported values before preview', () => {
    const markup = renderToStaticMarkup(<ProfileRestore api={api} />);
    expect(markup).toContain('Restore a profile backup');
    expect(markup).toContain('creates a new profile');
    expect(markup).toContain('Imported facts and answers will require review and verification.');
    expect(markup).toContain('Choose a JSON backup file');
    expect(markup).not.toContain('PRIVATE_SOURCE_NAME');
    expect(markup).not.toContain('PRIVATE_FACT');
  });

  it('renders a fixed-label preview with omissions and an explicit confirm control only', () => {
    const markup = renderToStaticMarkup(<ProfileRestorePreviewCard preview={{
      fingerprint: preview.fingerprint,
      sourceProfile: preview.source_profile,
      counts: preview.counts,
      warnings: preview.warnings,
      omittedSections: preview.omitted_sections,
    }} confirmed={false} onConfirmedChange={() => {}} onRestore={() => {}} restoring={false} />);
    expect(markup).toContain('2 facts');
    expect(markup).toContain('1 answer');
    expect(markup).toContain('Employment history and job records');
    expect(markup).toContain('Application history');
    expect(markup).toContain('Original uploaded files are not included');
    expect(markup).toContain('I understand this creates a new profile');
    expect(markup).not.toContain('DO_NOT_DISPLAY');
    expect(markup).not.toContain('PRIVATE_FACT');
  });
});
