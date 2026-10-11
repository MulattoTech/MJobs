import React, { useState } from 'react';

export type DataExportKind = 'jobs-csv' | 'applications-csv' | 'profile-json' | 'backup-json';
export type DataExportResult =
  | { status: 'downloaded' }
  | { status: 'empty' }
  | { status: 'error' };
export type DataExportDownload = (blob: Blob, filename: string) => void;
export type DataExportFetch = (profileId: string, kind: DataExportKind) => Promise<Blob>;
export type DataExportRequest = (input: string, init?: RequestInit) => Promise<Response>;

const EXPORT_ROUTES: Record<DataExportKind, string> = {
  'jobs-csv': 'jobs.csv',
  'applications-csv': 'applications.csv',
  'profile-json': 'profile.json',
  'backup-json': 'backup.json',
};

function filenamePart(profileId: string): string {
  return profileId.replace(/[^a-zA-Z0-9_-]/g, '-').replace(/-+/g, '-').slice(0, 48) || 'profile';
}

function filename(kind: DataExportKind, profileId: string): string {
  const profile = filenamePart(profileId);
  switch (kind) {
    case 'jobs-csv': return `mjobs-jobs-${profile}.csv`;
    case 'applications-csv': return `mjobs-applications-${profile}.csv`;
    case 'profile-json': return `mjobs-profile-${profile}.json`;
    case 'backup-json': return `mjobs-backup-${profile}.json`;
  }
}

function isCsv(kind: DataExportKind): boolean {
  return kind === 'jobs-csv' || kind === 'applications-csv';
}

/** Map stable export capabilities to the profile-scoped attachment routes. */
export async function fetchDataExport(
  profileId: string,
  kind: DataExportKind,
  request: DataExportRequest = fetch,
): Promise<Blob> {
  const path = `/profiles/${encodeURIComponent(profileId)}/exports/${EXPORT_ROUTES[kind]}`;
  let response: Response;
  try {
    response = await request(path, {
      method: 'GET', headers: { Accept: isCsv(kind) ? 'text/csv' : 'application/json' },
    });
  } catch {
    throw new Error('DATA_EXPORT_FAILED');
  }
  if (!response.ok) throw new Error('DATA_EXPORT_FAILED');
  try {
    return await response.blob();
  } catch {
    throw new Error('DATA_EXPORT_FAILED');
  }
}

function downloadInBrowser(blob: Blob, filename: string): void {
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = filename;
  anchor.style.display = 'none';
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(href), 0);
}

function csvRecordCount(text: string): number {
  let records = 0;
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const character = text[index];
    if (character === '"') {
      if (quoted && text[index + 1] === '"') index += 1;
      else quoted = !quoted;
    } else if (character === '\n' && !quoted) {
      records += 1;
    }
  }
  if (text.length > 0 && !text.endsWith('\n')) records += 1;
  return Math.max(0, records - 1); // Exclude the CSV header row.
}

/** Download an attachment without returning or rendering its private contents. */
export async function runDataExport(
  kind: DataExportKind,
  profileId: string,
  fetchExport: DataExportFetch = fetchDataExport,
  download: DataExportDownload = downloadInBrowser,
): Promise<DataExportResult> {
  if (!profileId.trim()) return { status: 'error' };
  try {
    const blob = await fetchExport(profileId, kind);
    if (!(blob instanceof Blob) || blob.size === 0) return { status: 'error' };
    if (isCsv(kind) && csvRecordCount(await blob.text()) === 0) {
      return { status: 'empty' };
    }
    download(blob, filename(kind, profileId));
    return { status: 'downloaded' };
  } catch {
    // Fetch errors can contain server data; expose only a fixed result status.
    return { status: 'error' };
  }
}

function resultMessage(result: DataExportResult): string {
  if (result.status === 'empty') return 'There is no data in this category to export. No file was downloaded.';
  if (result.status === 'error') return 'Export failed. No file was downloaded. Try again later.';
  return 'Export downloaded.';
}

export type DataExportProps = {
  profileId?: string;
  fetchExport?: DataExportFetch;
  download?: DataExportDownload;
  busy?: boolean;
};

export function DataExport({ profileId, fetchExport = fetchDataExport, download, busy = false }: DataExportProps) {
  const [exporting, setExporting] = useState<DataExportKind | null>(null);
  const [feedback, setFeedback] = useState<{ kind: 'status' | 'alert'; message: string } | null>(null);
  const locked = busy || exporting !== null || !profileId;

  const startExport = async (kind: DataExportKind) => {
    if (!profileId || locked) return;
    setExporting(kind);
    setFeedback({ kind: 'status', message: 'Preparing export…' });
    const result = await runDataExport(kind, profileId, fetchExport, download);
    setFeedback({
      kind: result.status === 'error' ? 'alert' : 'status',
      message: resultMessage(result),
    });
    setExporting(null);
  };

  const button = (kind: DataExportKind, label: string) => <button
    type="button"
    disabled={locked}
    onClick={() => void startExport(kind)}
  >{exporting === kind ? 'Preparing export…' : label}</button>;

  return <section className="panel" aria-labelledby="data-export-title" aria-busy={exporting !== null}>
    <h2 id="data-export-title">Data export</h2>
    <p>Download profile-scoped data to files on this device. Export files may contain private information; store them carefully.</p>
    <p className="muted">Export contents are never shown here.</p>
    {!profileId && <p role="status">Select a profile to export its data.</p>}
    {busy && <p role="status">Loading export options…</p>}
    <div className="toolbar" aria-label="Export choices">
      {button('jobs-csv', 'Download jobs CSV')}
      {button('applications-csv', 'Download applications CSV')}
      {button('profile-json', 'Download profile JSON')}
      {button('backup-json', 'Download complete backup')}
    </div>
    {feedback && <p role={feedback.kind} aria-live="polite">{feedback.message}</p>}
  </section>;
}
