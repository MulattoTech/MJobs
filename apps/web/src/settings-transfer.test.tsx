import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { SettingsTransfer, getSettingsImportPreview, MAX_SETTINGS_IMPORT_BYTES } from './settings-transfer';

describe('settings transfer component', () => {
  it('explains the safe transfer scope without rendering setting values', () => {
    const tree = React.createElement(SettingsTransfer, {
      profileId: 'profile-1',
      onExport: vi.fn(),
      onImport: vi.fn(),
    });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Export settings');
    expect(html).toContain('Import settings');
    expect(html).toContain('Search profiles');
    expect(html).toContain('Automation rules');
    expect(html).toContain('Source configuration');
    expect(html).toContain('Automation rules for scheduled searches');
    expect(html).toContain('public collection policies');
    expect(html).toContain('Secrets, career facts, and documents are never included.');
    expect(html).not.toContain('password');
    expect(html).not.toContain('resume');
  });

  it('offers an accessible export action and does not export before activation', () => {
    const onExport = vi.fn();
    const tree = React.createElement(SettingsTransfer, { profileId: 'profile-1', onExport, onImport: vi.fn() });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('<button type="button"');
    expect(onExport).not.toHaveBeenCalled();
  });

  it('previews legacy v1 settings as migration-required without exposing payload values', () => {
    const preview = getSettingsImportPreview(JSON.stringify({
      schema_version: 1,
      settings: {
        search_profiles: [{ name: 'private profile name' }],
        automation_rules: [{ secret: 'do-not-display' }, {}],
        source_configuration: [{ token: 'do-not-display' }],
      },
      career_facts: [{ text: 'private career detail' }],
      documents: [{ filename: 'private-resume.pdf' }],
    }));

    expect(preview).toEqual({
      ok: true,
      schemaVersion: 1,
      counts: { searchProfiles: 1, automationRules: 2, sourceConfigurations: 1 },
    });
    expect(JSON.stringify(preview)).not.toMatch(/private|secret|token|resume/i);
  });

  it('previews current v2 settings with counts only', () => {
    const preview = getSettingsImportPreview(JSON.stringify({
      schema_version: 2,
      settings: {
        search_profiles: [{ name: 'private profile name' }],
        automation_rules: [{ id: 'rule-1' }],
        source_configuration: [],
      },
    }));

    expect(preview).toEqual({
      ok: true,
      schemaVersion: 2,
      counts: { searchProfiles: 1, automationRules: 1, sourceConfigurations: 0 },
    });
    expect(JSON.stringify(preview)).not.toContain('private profile name');
  });

  it('rejects unsupported and non-integer schema versions', () => {
    for (const schemaVersion of [0, 3, -1, 1.5, true, '1', null]) {
      expect(getSettingsImportPreview(JSON.stringify({
        schema_version: schemaVersion,
        settings: { search_profiles: [], automation_rules: [], source_configuration: [] },
      }))).toEqual({ ok: false, errorCode: 'unsupported_format' });
    }
  });

  it('rejects malformed JSON and oversized files before import can begin', () => {
    expect(getSettingsImportPreview('{broken').ok).toBe(false);
    const oversized = getSettingsImportPreview(' '.repeat(MAX_SETTINGS_IMPORT_BYTES + 1));
    expect(!oversized.ok && oversized.errorCode).toBe('file_too_large');
  });
});
