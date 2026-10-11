import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { TransientRetention, type RetentionSnapshot } from './transient-retention';

const snapshot: RetentionSnapshot = {
  policies: [
    { category: 'debug_screenshots', enabled: true, maxAgeDays: 7 },
    { category: 'temporary_html', enabled: true, maxAgeDays: 2 },
    { category: 'old_raw_payloads', enabled: true, maxAgeDays: 30 },
    { category: 'temporary_browser_artifacts', enabled: true, maxAgeDays: 7 },
    { category: 'temporary_generated_files', enabled: false, maxAgeDays: 14 },
    { category: 'rotated_logs', enabled: true, maxAgeDays: 30 },
  ],
  preview: [
    { category: 'debug_screenshots', eligibleCount: 2, eligibleBytes: 1024 },
    { category: 'temporary_html', eligibleCount: 1, eligibleBytes: 512 },
    { category: 'old_raw_payloads', eligibleCount: 0, eligibleBytes: 0 },
    { category: 'temporary_browser_artifacts', eligibleCount: 3, eligibleBytes: 2048 },
    { category: 'temporary_generated_files', eligibleCount: 0, eligibleBytes: 0 },
    { category: 'rotated_logs', eligibleCount: 1, eligibleBytes: 4096 },
  ],
  lastSweep: { status: 'SUCCEEDED', completedAt: '2026-10-10T16:00:00Z', removedCount: 5, removedBytes: 7680 },
};

describe('transient retention panel', () => {
  it('shows bounded managed categories, age policies, eligible counts and bytes, and last sweep status', () => {
    const html = renderToStaticMarkup(<TransientRetention status="ready" snapshot={snapshot} />);

    expect(html).toContain('<section class="panel" aria-labelledby="transient-retention-title" aria-busy="false">');
    expect(html).toContain('<h2 id="transient-retention-title">Transient data retention</h2>');
    expect(html).toContain('Only these managed transient artifacts are eligible for cleanup.');
    expect(html).toContain('Debug screenshots');
    expect(html).toContain('Temporary HTML');
    expect(html).toContain('Old raw payloads');
    expect(html).toContain('Temporary browser artifacts');
    expect(html).toContain('Temporary generated files');
    expect(html).toContain('Rotated logs');
    expect(html).toContain('7 days');
    expect(html).toContain('2 items');
    expect(html).toContain('1 KB');
    expect(html).toContain('Last sweep: Succeeded');
    expect(html).toContain('5 items');
    expect(html).not.toContain('<button');
    expect(html).not.toContain('Core application history');
  });

  it('announces loading and clear status-read errors accessibly', () => {
    const loading = renderToStaticMarkup(<TransientRetention status="loading" />);
    const error = renderToStaticMarkup(<TransientRetention status="error" />);

    expect(loading).toContain('<p role="status">Loading retention status and cleanup preview…</p>');
    expect(error).toContain('<div class="alert" role="alert">Retention status and preview could not be loaded.</div>');
  });

  it('offers a refresh when a ready response is missing its required retention snapshot', () => {
    const onRefresh = vi.fn();
    const section = TransientRetention({ status: 'ready', onRefresh });
    const retry = findElements(section, 'button').find(button => button.props.children === 'Retry status load');

    expect(findElements(section, 'div').some(element => element.props.role === 'alert')).toBe(true);
    expect(retry).toBeDefined();
    retry?.props.onClick?.();
    expect(onRefresh).toHaveBeenCalledOnce();
  });

  it('does not offer cleanup unless a bounded preview action is explicitly supported', () => {
    const html = renderToStaticMarkup(<TransientRetention status="ready" snapshot={snapshot} />);

    expect(html).not.toContain('Delete listed temporary data');
    expect(html).toContain('Application history and submitted application records are never included.');
  });

  it('applies only the explicit bounded preview token supplied by the API', () => {
    const onApply = vi.fn();
    const section = TransientRetention({
      status: 'ready',
      snapshot,
      cleanup: {
        supported: true,
        previewId: 'preview-42',
        maxAgeDays: 30,
        maxItems: 10,
        maxBytes: 10240,
        eligibleCount: 7,
        eligibleBytes: 7680,
        onApply,
      },
    });
    const buttons = findElements(section, 'button');
    const apply = buttons.find(button => button.props.children === 'Delete listed temporary data');

    expect(apply).toBeDefined();
    expect(onApply).not.toHaveBeenCalled();
    apply?.props.onClick?.();
    expect(onApply).toHaveBeenCalledOnce();
    expect(onApply).toHaveBeenCalledWith('preview-42');
  });

  it('hides cleanup when the supported preview has no eligible data or invalid bounds', () => {
    for (const cleanup of [
      { supported: true, previewId: 'empty', maxAgeDays: 30, maxItems: 10, maxBytes: 1024, eligibleCount: 0, eligibleBytes: 0, onApply: vi.fn() },
      { supported: true, previewId: '', maxAgeDays: 30, maxItems: 10, maxBytes: 1024, eligibleCount: 1, eligibleBytes: 1, onApply: vi.fn() },
      { supported: true, previewId: 'unbounded', maxAgeDays: 30, maxItems: 0, maxBytes: 1024, eligibleCount: 1, eligibleBytes: 1, onApply: vi.fn() },
      { supported: true, previewId: 'stale', maxAgeDays: 30, maxItems: 10, maxBytes: 10240, eligibleCount: 6, eligibleBytes: 7680, onApply: vi.fn() },
    ]) {
      const html = renderToStaticMarkup(
        <TransientRetention status="ready" snapshot={snapshot} cleanup={cleanup} />,
      );
      expect(html).not.toContain('Delete listed temporary data');
    }
  });

  it('hides cleanup when the last sweep failed, even if an action is offered', () => {
    const failed: RetentionSnapshot = {
      ...snapshot,
      lastSweep: { status: 'FAILED', removedCount: 0, removedBytes: 0 },
    };
    const html = renderToStaticMarkup(
      <TransientRetention status="ready" snapshot={failed} cleanup={{
        supported: true,
        previewId: 'preview-42',
        maxAgeDays: 30,
        maxItems: 10,
        maxBytes: 10240,
        eligibleCount: 7,
        eligibleBytes: 7680,
        onApply: vi.fn(),
      }} />,
    );

    expect(html).not.toContain('Delete listed temporary data');
  });

  it('hides cleanup if policy data is outside the configured age bounds', () => {
    const invalidPolicy: RetentionSnapshot = {
      ...snapshot,
      policies: snapshot.policies.map(policy => policy.category === 'debug_screenshots'
        ? { ...policy, maxAgeDays: 0 }
        : policy),
    };
    const html = renderToStaticMarkup(
      <TransientRetention status="ready" snapshot={invalidPolicy} cleanup={{
        supported: true,
        previewId: 'preview-42',
        maxAgeDays: 30,
        maxItems: 10,
        maxBytes: 10240,
        eligibleCount: 7,
        eligibleBytes: 7680,
        onApply: vi.fn(),
      }} />,
    );

    expect(html).toContain('Policy unavailable');
    expect(html).not.toContain('Delete listed temporary data');
  });

  it('keeps failures visible without exposing raw error text', () => {
    const failed: RetentionSnapshot = {
      ...snapshot,
      lastSweep: {
        status: 'FAILED', completedAt: '2026-10-10T16:00:00Z', removedCount: 0, removedBytes: 0,
      },
    };
    const html = renderToStaticMarkup(<TransientRetention status="ready" snapshot={failed} />);

    expect(html).toContain('Last sweep: Failed');
    expect(html).toContain('The last cleanup reported a failure. Refresh the status before applying cleanup.');
    expect(html).not.toContain('private');
  });
});

function findElements(node: React.ReactNode, type: string): React.ReactElement<any>[] {
  if (Array.isArray(node)) return node.flatMap(child => findElements(child, type));
  if (!React.isValidElement<any>(node)) return [];
  const element = node as React.ReactElement<{ children?: React.ReactNode }>;
  const found = element.type === type ? [element] : [];
  return [...found, ...findElements(element.props.children, type)];
}
