import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DataExport, fetchDataExport, runDataExport } from './data-export';

const noDownload = vi.fn();

async function downloadedText(download: ReturnType<typeof vi.fn>) {
  const [blob] = download.mock.calls.at(-1) as [Blob, string];
  return await blob.text();
}

describe('DataExport', () => {
  it('fetches the profile jobs CSV attachment and never returns its contents to the UI', async () => {
    const fetchExport = vi.fn(async (_profileId: string, _kind: string) =>
      new Blob(['id,title\njob-1,Northwind Engineer'], { type: 'text/csv' }));
    const download = vi.fn();

    const result = await runDataExport('jobs-csv', 'profile 1', fetchExport, download);

    expect(fetchExport).toHaveBeenCalledExactlyOnceWith('profile 1', 'jobs-csv');
    expect(await downloadedText(download)).toContain('Northwind Engineer');
    expect(download.mock.calls[0][1]).toBe('mjobs-jobs-profile-1.csv');
    expect(JSON.stringify(result)).not.toContain('Northwind');
    expect(result).toEqual({ status: 'downloaded' });
  });

  it('uses the attachment routes for applications CSV, profile JSON, and complete backup', async () => {
    const fetchExport = vi.fn(async (_profileId: string, kind: string) =>
      new Blob([kind === 'applications-csv' ? 'id,state\na,PREPARED' : '{"profile_id":"p"}']));
    const download = vi.fn();

    await expect(runDataExport('applications-csv', 'p', fetchExport, download))
      .resolves.toEqual({ status: 'downloaded' });
    await expect(runDataExport('profile-json', 'p', fetchExport, download))
      .resolves.toEqual({ status: 'downloaded' });
    await expect(runDataExport('backup-json', 'p', fetchExport, download))
      .resolves.toEqual({ status: 'downloaded' });

    expect(fetchExport.mock.calls).toEqual([
      ['p', 'applications-csv'], ['p', 'profile-json'], ['p', 'backup-json'],
    ]);
    expect(download.mock.calls.map(([, filename]) => filename)).toEqual([
      'mjobs-applications-p.csv', 'mjobs-profile-p.json', 'mjobs-backup-p.json',
    ]);
  });

  it('maps capability identifiers to profile-scoped download routes and checks HTTP errors safely', async () => {
    const request = vi.fn(async (path: string) => new Response('synthetic-file', { status: 200 }));
    const blob = await fetchDataExport('profile /1', 'backup-json', request);
    expect(request).toHaveBeenCalledExactlyOnceWith('/profiles/profile%20%2F1/exports/backup.json', {
      method: 'GET', headers: { Accept: 'application/json' },
    });
    expect(await blob.text()).toBe('synthetic-file');

    const failed = vi.fn(async () => new Response('PRIVATE_PROVIDER_ERROR', { status: 500 }));
    await expect(fetchDataExport('p', 'jobs-csv', failed)).rejects.toThrow('DATA_EXPORT_FAILED');
  });

  it('keeps private backup contents in the downloaded file and out of the rendered UI', async () => {
    const privateContent = 'PRIVATE_PROFILE_FACT';
    const fetchExport = async () => new Blob([JSON.stringify({ fact: privateContent })]);
    const download = vi.fn();

    const result = await runDataExport('backup-json', 'p', fetchExport, download);
    const rendered = renderToStaticMarkup(
      <DataExport profileId="p" fetchExport={fetchExport} download={download} />,
    );

    expect(result).toEqual({ status: 'downloaded' });
    expect(await downloadedText(download)).toContain(privateContent);
    expect(JSON.stringify(result)).not.toContain(privateContent);
    expect(rendered).not.toContain(privateContent);
  });

  it('reports empty and failed exports without downloading or exposing provider errors', async () => {
    const empty = vi.fn(async () => new Blob(['id,title\n'], { type: 'text/csv' }));
    const failed = vi.fn(async () => { throw new Error('PRIVATE_PROVIDER_ERROR'); });
    const download = vi.fn();

    await expect(runDataExport('jobs-csv', 'p', empty, download))
      .resolves.toEqual({ status: 'empty' });
    await expect(runDataExport('jobs-csv', 'p', failed, download))
      .resolves.toEqual({ status: 'error' });
    expect(download).not.toHaveBeenCalled();
    expect(renderToStaticMarkup(<DataExport profileId="p" fetchExport={failed} />))
      .not.toContain('PRIVATE_PROVIDER_ERROR');
  });

  it('shows loading and missing-profile states and all four export choices', () => {
    const fetchExport = async () => new Blob();
    const loading = renderToStaticMarkup(<DataExport profileId="p" fetchExport={fetchExport} busy />);
    const noProfile = renderToStaticMarkup(<DataExport fetchExport={fetchExport} />);
    expect(loading).toContain('Loading export options');
    expect(loading).toContain('disabled=""');
    expect(noProfile).toContain('Select a profile to export its data.');
    expect(noProfile).toContain('Download jobs CSV');
    expect(noProfile).toContain('Download applications CSV');
    expect(noProfile).toContain('Download profile JSON');
    expect(noProfile).toContain('Download complete backup');
  });
});
