import React, { useRef, useState } from 'react';

export const MAX_SETTINGS_IMPORT_BYTES = 1024 * 1024;

export type SettingsImportCounts = {
  searchProfiles: number;
  automationRules: number;
  sourceConfigurations: number;
};

export type SettingsImportPreview =
  | { ok: true; counts: SettingsImportCounts }
  | { ok: false; errorCode: 'file_too_large' | 'invalid_json' | 'unsupported_format' };

export type SettingsImportResult = {
  ok: boolean;
  importedCount?: number;
  errorCode?: string;
};

export type SettingsTransferProps = {
  profileId?: string;
  busy?: boolean;
  error?: string | null;
  success?: string | null;
  onExport: () => Promise<Blob | string>;
  onImport: (fileText: string, options: { confirmReplace: true }) => Promise<SettingsImportResult>;
};

type RecordValue = Record<string, unknown>;

function isRecord(value: unknown): value is RecordValue {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function countCategory(value: unknown): number | null {
  if (Array.isArray(value)) return value.length;
  if (isRecord(value)) return Object.keys(value).length;
  return null;
}

/** Return category counts only. Never return imported setting values to the UI. */
export function getSettingsImportPreview(text: string): SettingsImportPreview {
  if (new TextEncoder().encode(text).byteLength > MAX_SETTINGS_IMPORT_BYTES) {
    return { ok: false, errorCode: 'file_too_large' };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return { ok: false, errorCode: 'invalid_json' };
  }

  if (!isRecord(parsed) || parsed.schema_version !== 1 || !isRecord(parsed.settings)) {
    return { ok: false, errorCode: 'unsupported_format' };
  }

  const searchProfiles = countCategory(parsed.settings.search_profiles);
  const automationRules = countCategory(parsed.settings.automation_rules);
  const sourceConfigurations = countCategory(parsed.settings.source_configuration);
  if (searchProfiles === null || automationRules === null || sourceConfigurations === null) {
    return { ok: false, errorCode: 'unsupported_format' };
  }

  return {
    ok: true,
    counts: { searchProfiles, automationRules, sourceConfigurations },
  };
}

function safeFilenamePart(value: string | undefined): string {
  const normalized = (value || 'settings').replace(/[^a-zA-Z0-9_-]/g, '-').replace(/-+/g, '-').slice(0, 48);
  return normalized || 'settings';
}

function downloadExport(data: Blob | string, profileId?: string): void {
  const blob = data instanceof Blob ? data : new Blob([data], { type: 'application/json' });
  const href = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = href;
  link.download = `mjobs-settings-${safeFilenamePart(profileId)}.json`;
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 0);
}

function importErrorMessage(errorCode: string): string {
  switch (errorCode) {
    case 'file_too_large': return 'This file is larger than the 1 MiB import limit.';
    case 'invalid_json': return 'This file is not valid JSON.';
    case 'unsupported_format': return 'This file does not use a supported settings format.';
    default: return 'Settings could not be imported. No settings were changed.';
  }
}

export function SettingsTransfer({ profileId, busy = false, error, success, onExport, onImport }: SettingsTransferProps) {
  const fileInput = useRef<HTMLInputElement>(null);
  const [exporting, setExporting] = useState(false);
  const [selectedFileName, setSelectedFileName] = useState('');
  const [fileText, setFileText] = useState('');
  const [preview, setPreview] = useState<SettingsImportPreview | null>(null);
  const [confirmReplace, setConfirmReplace] = useState(false);
  const [importing, setImporting] = useState(false);
  const [feedback, setFeedback] = useState<{ kind: 'error' | 'success'; text: string } | null>(null);
  const locked = busy || exporting || importing;

  const clearSelection = () => {
    setSelectedFileName('');
    setFileText('');
    setPreview(null);
    setConfirmReplace(false);
    setFeedback(null);
    if (fileInput.current) fileInput.current.value = '';
  };

  const selectFile = async (file?: File) => {
    setSelectedFileName(file?.name || '');
    setFileText('');
    setPreview(null);
    setConfirmReplace(false);
    setFeedback(null);
    if (!file) return;
    if (file.size > MAX_SETTINGS_IMPORT_BYTES) {
      setPreview({ ok: false, errorCode: 'file_too_large' });
      return;
    }

    try {
      const text = await file.text();
      const nextPreview = getSettingsImportPreview(text);
      if (!nextPreview.ok) {
        setPreview(nextPreview);
        return;
      }
      setFileText(text);
      setPreview(nextPreview);
    } catch {
      setPreview({ ok: false, errorCode: 'invalid_json' });
    }
  };

  const exportSettings = async () => {
    setExporting(true);
    setFeedback(null);
    try {
      const exported = await onExport();
      downloadExport(exported, profileId);
      setFeedback({ kind: 'success', text: 'Settings export downloaded.' });
    } catch {
      setFeedback({ kind: 'error', text: 'Settings could not be exported. No file was downloaded.' });
    } finally {
      setExporting(false);
    }
  };

  const confirmImport = async () => {
    if (!fileText || !preview?.ok || !confirmReplace || locked) return;
    setImporting(true);
    setFeedback(null);
    try {
      const result = await onImport(fileText, { confirmReplace: true });
      if (!result.ok) {
        setFeedback({ kind: 'error', text: importErrorMessage(result.errorCode || '') });
        return;
      }
      const count = Number.isSafeInteger(result.importedCount) && (result.importedCount || 0) >= 0
        ? result.importedCount || 0
        : 0;
      setFeedback({ kind: 'success', text: `Settings imported successfully (${count} items).` });
      setFileText('');
      setPreview(null);
      setConfirmReplace(false);
      setSelectedFileName('');
      if (fileInput.current) fileInput.current.value = '';
    } catch {
      setFeedback({ kind: 'error', text: 'Settings could not be imported. No settings were changed.' });
    } finally {
      setImporting(false);
    }
  };

  return <section className="panel" aria-labelledby="settings-transfer-title">
    <h2 id="settings-transfer-title">Settings transfer</h2>
    <p>Move validated search profiles, automation rules, and source configuration between MJobs installations.</p>
    <ul aria-label="Supported settings">
      <li>Search profiles</li>
      <li>Automation rules for scheduled searches</li>
      <li>Source configuration and public collection policies</li>
    </ul>
    <p>Secrets, career facts, and documents are never included.</p>

    <section aria-labelledby="settings-export-title">
      <h3 id="settings-export-title">Export settings</h3>
      <p>Download a JSON file containing supported settings for this profile.</p>
      <button type="button" disabled={locked} onClick={() => void exportSettings()}>
        {exporting ? 'Preparing export…' : 'Download settings JSON'}
      </button>
    </section>

    <section aria-labelledby="settings-import-title">
      <h3 id="settings-import-title">Import settings</h3>
      <label htmlFor="settings-import-file">Choose a settings JSON file (maximum 1 MiB)</label>
      <input
        ref={fileInput}
        id="settings-import-file"
        type="file"
        accept="application/json,.json"
        disabled={locked}
        onChange={event => void selectFile(event.currentTarget.files?.[0])}
      />
      {selectedFileName && <p aria-live="polite">Selected file: {selectedFileName}</p>}
      {selectedFileName && <button type="button" disabled={locked} onClick={clearSelection}>Clear file selection</button>}

      {preview && !preview.ok && <p role="alert">{importErrorMessage(preview.errorCode)}</p>}
      {preview?.ok && <div aria-live="polite" aria-label="Settings import preview">
        <p>Preview: {preview.counts.searchProfiles} search profiles, {preview.counts.automationRules} automation rules, and {preview.counts.sourceConfigurations} source configurations.</p>
        <p>Import replaces the supported settings in the selected profile. Nothing changes until you confirm.</p>
        <p>Missing public sources are created with the imported collection policy. Existing shared source policies must match; an import cannot overwrite another profile's source settings. Automation rules schedule discovery only.</p>
        <label>
          <input type="checkbox" checked={confirmReplace} disabled={locked} onChange={event => setConfirmReplace(event.currentTarget.checked)} />
          I understand this will replace the selected profile's supported settings.
        </label>
        <button type="button" disabled={locked || !confirmReplace} onClick={() => void confirmImport()}>
          {importing ? 'Importing…' : 'Confirm import'}
        </button>
      </div>}
    </section>

    {(feedback || error) && <p role="alert">{feedback?.kind === 'error' ? feedback.text : error ? 'Settings transfer failed. Existing settings were preserved.' : ''}</p>}
    {feedback?.kind === 'success' && <p role="status">{feedback.text}</p>}
    {!feedback && success && <p role="status">Settings transfer completed.</p>}
  </section>;
}
