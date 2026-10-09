import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { CompanySourceMonitoring, type Monitoring } from './company-source-monitoring';

const healthy: Monitoring = {revision:2, enabled:true, poll_interval:3600, status:'HEALTHY', failure_count:0, last_attempt:'2026-10-03T12:00:00Z', next_due:'2026-10-03T13:00:00Z', last_error:null, operation_id:null, rediscovery_operation_id:null, authorization:'USER_REVIEW'};
const props = {sourceId:'source-1', sourceVersion:'v2', sourceEnabled:true, supported:true, busy:false, profileSelected:true, onMonitor:vi.fn(), onRun:vi.fn()};

describe('company source monitoring', () => {
  it('renders durable monitoring state and update controls without identity claims', () => {
    const html = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={healthy} />);
    expect(html).toContain('Source monitoring');
    expect(html).toContain('Healthy');
    expect(html).toContain('Update monitoring');
    expect(html).toContain('Run now');
    expect(html).toContain('Bounded public read checks');
    expect(html).toContain('does not verify employer identity');
    expect(html).not.toContain('USER_REVIEW');
  });

  it('offers initial review monitoring and preserves HTTPS-independent interval bounds', () => {
    const html = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={null} />);
    expect(html).toContain('Monitor this reviewed source');
    expect(html).toContain('min="5"');
    expect(html).toContain('max="10080"');
    expect(html).not.toContain('Run now');
  });

  it('blocks controls for unsupported or disabled sources', () => {
    const unsupported = renderToStaticMarkup(<CompanySourceMonitoring {...props} supported={false} monitoring={healthy} />);
    expect(unsupported).toContain('Monitoring is not supported for this source');
    expect(unsupported).not.toContain('Update monitoring');
    const disabled = renderToStaticMarkup(<CompanySourceMonitoring {...props} sourceEnabled={false} monitoring={healthy} />);
    expect(disabled).toContain('Enable the existing source before monitoring can run');
    expect(disabled).not.toContain('Run now');
  });

  it('uses idle and failed as reviewable monitoring states but gates run now', () => {
    const idle = {...healthy, status:'IDLE'};
    const idleHtml = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={idle} />);
    expect(idleHtml).toContain('Idle');
    expect(idleHtml).toContain('Update monitoring');
    expect(idleHtml).toContain('Run now');
    const failedHtml = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{...healthy, status:'FAILED'}} />);
    expect(failedHtml).toContain('Failed');
    expect(failedHtml).toContain('Update monitoring');
    expect(failedHtml).toContain('Run now');
    const queuedHtml = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{...healthy, status:'QUEUED'}} />);
    expect(queuedHtml).toContain('Queued');
    expect(queuedHtml).not.toContain('Run now');
    const blockedHtml = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{...healthy, status:'BLOCKED'}} />);
    expect(blockedHtml).toContain('Monitor this reviewed source');
    expect(blockedHtml).not.toContain('Run now');
  });

  it('explains degraded health and stopped automatic retries while keeping manual recovery available', () => {
    const degraded = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{...healthy, status:'IDLE', failure_count:2, last_error:'HTTP_STATUS_ERROR'}} />);
    expect(degraded).toContain('Degraded');
    expect(degraded).toContain('Consecutive failures');
    expect(degraded).toContain('2 (retry scheduled)');
    expect(degraded).toContain('Run now');
    const retrying = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{...healthy, status:'SCHEDULED', failure_count:1}} />);
    expect(retrying).toContain('Degraded');

    const stopped = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{...healthy, status:'FAILED', failure_count:3, last_error:'HTTP_STATUS_ERROR'}} />);
    expect(stopped).toContain('Failed');
    expect(stopped).toContain('3 (automatic retries stopped)');
    expect(stopped).toContain('Run now');
  });

  it('explains that failed-source rediscovery is queued without changing the source', () => {
    const html = renderToStaticMarkup(<CompanySourceMonitoring {...props} monitoring={{
      ...healthy, status:'FAILED', failure_count:3,
      rediscovery_operation_id:'rediscovery-1', last_error:'HTTP_STATUS_ERROR',
    }} />);
    expect(html).toContain('Repeated failures queued an employer-source rediscovery');
    expect(html).toContain('existing failed source and its settings remain unchanged');
    expect(html).toContain('review any findings');
  });
});
