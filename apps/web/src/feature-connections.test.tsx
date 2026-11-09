// @vitest-environment jsdom
import React, { act } from 'react';
import { createRoot, type Root } from 'react-dom/client';
import { fireEvent, getByRole } from '@testing-library/dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { renderToStaticMarkup } from 'react-dom/server';
import { TransientRetention } from './transient-retention';
import { RetentionConnected, retentionSnapshotFromPreview, type RetentionPreviewPayload } from './feature-connections';

function configuredPreview(control?: unknown): RetentionPreviewPayload {
  return {
    policy: [{id:'screens',category:'DEBUG_SCREENSHOT',pattern:'*.png',max_age_days:30}],
    preview: {status:'OK',dry_run:true,by_category:{DEBUG_SCREENSHOT:{candidate_count:2,candidate_bytes:72}}},
    last_sweep: null,
    ...(control === undefined ? {} : {cleanup_control: control}),
  } as RetentionPreviewPayload;
}

function markup(payload: RetentionPreviewPayload) {
  return renderToStaticMarkup(<TransientRetention status="ready" snapshot={retentionSnapshotFromPreview(payload)} />);
}

describe('RetentionConnected', () => {
  it('turns a fixed configured preview into the six-category user view', async () => {
    const payload: RetentionPreviewPayload = {
      policy: [{id:'screens',category:'DEBUG_SCREENSHOT',pattern:'*.png',max_age_days:30}],
      preview: {status:'OK',dry_run:true,by_category:{DEBUG_SCREENSHOT:{candidate_count:2,candidate_bytes:72}}},
      last_sweep: null,
    };
    const snapshot = retentionSnapshotFromPreview(payload);
    const html = renderToStaticMarkup(<TransientRetention status="ready" snapshot={snapshot} />);
    expect(html).toContain('2 items');
    expect(html).toContain('72 B');
    expect(html).toContain('Debug screenshots');
    expect(html).toContain('Last sweep: Not run yet');
    expect(html).not.toContain('Delete listed temporary data');
  });

  it('fails closed for malformed server summaries', () => {
    expect(() => retentionSnapshotFromPreview({policy:[],preview:{status:'PRIVATE',by_category:{}},last_sweep:null} as unknown as RetentionPreviewPayload)).toThrow();
  });

  it('does not turn a configured category into cleanup permission on a legacy server', () => {
    const payload = configuredPreview();
    const snapshot = retentionSnapshotFromPreview(payload);
    expect(snapshot.policies[0].enabled).toBe(false);
    const html = markup(payload);
    expect(html).toContain('Configured · 30 days');
    expect(html).toContain('Cleanup permission: Unknown');
    expect(html).not.toContain('Enabled ·');
    expect(html).toContain('This preview does not delete files');
    expect(html).not.toContain('Delete listed temporary data');
  });

  it.each([
    ['ALLOWED', 4, 'Allowed by saved settings', true],
    ['DISABLED', 3, 'Disabled by saved settings', false],
    ['REVIEW_REQUIRED', 0, 'Review required', false],
    ['REVIEW_REQUIRED', 3, 'Review required', false],
    ['SERVER_DISABLED', 2, 'Disabled by server configuration', false],
  ] as const)('separates persisted %s permission from configured categories', (state, revision, message, enabled) => {
    const payload = configuredPreview({source:'PERSISTED_POLICY',state,policy_revision:revision});
    const snapshot = retentionSnapshotFromPreview(payload);
    expect(snapshot.policies[0].enabled).toBe(enabled);
    const html = markup(payload);
    expect(html).toContain(`Cleanup permission: ${message}`);
    if (revision === 0) {
      expect(html).toContain('No reviewed cleanup policy is available');
      expect(html).not.toContain('Saved policy revision:');
    } else expect(html).toContain(`Saved policy revision: ${revision}`);
    expect(html).toContain('Configured · 30 days');
    expect(html).toContain('Cleanup permission does not confirm that scheduled cleanup is running');
    expect(html).not.toContain('Enabled ·');
    expect(html).not.toContain('Delete listed temporary data');
  });

  it.each(['ALLOWED','SERVER_DISABLED'] as const)('identifies %s server-only control without inventing a saved policy', state => {
    const html = markup(configuredPreview({source:'SERVER_CONFIGURATION',state,policy_revision:null}));
    expect(html).toContain(state === 'ALLOWED' ? 'Allowed by server configuration' : 'Disabled by server configuration');
    expect(html).toContain('No saved policy revision was used');
    expect(html).not.toContain('Saved policy revision:');
  });

  it.each([
    null, [], {},
    {source:'PRIVATE_SECRET_CANARY',state:'ALLOWED',policy_revision:1},
    {source:'PERSISTED_POLICY',state:'UNKNOWN',policy_revision:1},
    {source:'PERSISTED_POLICY',state:'DISABLED',policy_revision:-1},
    {source:'PERSISTED_POLICY',state:'DISABLED',policy_revision:1.5},
    {source:'PERSISTED_POLICY',state:'ALLOWED',policy_revision:Number.MAX_SAFE_INTEGER+1},
    {source:'PERSISTED_POLICY',state:'ALLOWED',policy_revision:null},
    {source:'PERSISTED_POLICY',state:'ALLOWED',policy_revision:0},
    {source:'PERSISTED_POLICY',state:'DISABLED',policy_revision:0},
    {source:'SERVER_CONFIGURATION',state:'ALLOWED',policy_revision:1},
    {source:'SERVER_CONFIGURATION',state:'REVIEW_REQUIRED',policy_revision:null},
    {source:'SERVER_CONFIGURATION',state:'DISABLED',policy_revision:null},
  ])('fails closed for malformed or contradictory cleanup control %#', control => {
    expect(() => retentionSnapshotFromPreview(configuredPreview(control))).toThrow('RETENTION_CONTROL_INVALID');
  });

  it('does not render a blocked preview as a successful zero-item scan', () => {
    const payload = configuredPreview({source:'PERSISTED_POLICY',state:'DISABLED',policy_revision:1});
    payload.preview = {status:'BLOCKED',dry_run:true,by_category:{}};
    const html = markup(payload);
    expect(html).toContain('Preview blocked');
    expect(html).toContain('Unavailable');
    expect(html).not.toContain('0 items');
  });

  it('shows partial observed counts without inventing zero counts for unreported categories', () => {
    const payload = configuredPreview({source:'PERSISTED_POLICY',state:'ALLOWED',policy_revision:1});
    payload.policy.push({id:'html',category:'TEMP_HTML',pattern:'*.html',max_age_days:7});
    payload.preview.status = 'PARTIAL';
    const html = markup(payload);
    expect(html).toContain('Preview incomplete');
    expect(html).toContain('Observed: 2 items');
    expect(html).toContain('Unavailable');
    expect(html).not.toContain('0 items');
    expect(html).not.toContain('Delete listed temporary data');
  });
});

describe('mounted retention disclosure', () => {
  let container: HTMLDivElement;
  let root: Root;
  beforeEach(() => {
    vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true);
    container = document.createElement('div');
    document.body.append(container);
    root = createRoot(container);
  });
  afterEach(async () => {
    await act(async () => root.unmount());
    container.remove();
    vi.unstubAllGlobals();
  });

  it('loads disabled control and refreshes the recorded permission without starting cleanup', async () => {
    let finish!: (value: unknown) => void;
    const request = vi.fn<(path:string)=>Promise<unknown>>()
      .mockReturnValueOnce(new Promise(resolve => {finish=resolve;}))
      .mockResolvedValueOnce(configuredPreview({source:'PERSISTED_POLICY',state:'ALLOWED',policy_revision:2}));
    await act(async () => root.render(<RetentionConnected request={request}/>));
    expect(container.textContent).toContain('Loading retention status');
    await act(async () => finish(configuredPreview({source:'PERSISTED_POLICY',state:'DISABLED',policy_revision:1})));
    expect(container.textContent).toContain('Cleanup permission: Disabled by saved settings');
    await act(async () => fireEvent.click(getByRole(container, 'button', {name:'Refresh status and preview'})));
    expect(container.textContent).toContain('Cleanup permission: Allowed by saved settings');
    expect(container.textContent).toContain('Saved policy revision: 2');
    expect(container.textContent).not.toContain('Delete listed temporary data');
    expect(request.mock.calls).toEqual([['/retention/preview'],['/retention/preview']]);
  });

  it('hides stale permission after malformed refresh and renders only the safe error', async () => {
    const request = vi.fn<(path:string)=>Promise<unknown>>()
      .mockResolvedValueOnce(configuredPreview({source:'PERSISTED_POLICY',state:'ALLOWED',policy_revision:1}))
      .mockResolvedValueOnce(configuredPreview({source:'PRIVATE_SECRET_CANARY',state:'ALLOWED',policy_revision:2}));
    await act(async () => root.render(<RetentionConnected request={request}/>));
    expect(container.textContent).toContain('Allowed by saved settings');
    await act(async () => fireEvent.click(getByRole(container, 'button', {name:'Refresh status and preview'})));
    expect(getByRole(container, 'alert').textContent).toBe('Retention status and preview could not be loaded.');
    expect(container.textContent).not.toContain('Allowed by saved settings');
    expect(container.textContent).not.toContain('PRIVATE_SECRET_CANARY');
  });
});
