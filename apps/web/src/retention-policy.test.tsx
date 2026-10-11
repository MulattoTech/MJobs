import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  RetentionPolicyControls,
  RetentionPolicyEditor,
  loadRetentionPolicy,
  saveRetentionPolicy,
  type RetentionPolicy,
} from './retention-policy';

const policy: RetentionPolicy = {
  revision: 4,
  config_fingerprint: 'a'.repeat(64),
  enabled: false,
  pruning_allowed: true,
  rules: [
    { id: 'debug-images', category: 'DEBUG_SCREENSHOT', max_age_days: 21 },
    { id: 'temporary-html', category: 'TEMP_HTML', max_age_days: 14 },
  ],
  requires_review: true,
};

function response(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

function findElements<T extends React.ReactElement>(node: React.ReactNode, predicate: (element: React.ReactElement) => boolean): T[] {
  const found: T[] = [];
  const visit = (value: React.ReactNode) => {
    if (Array.isArray(value)) {
      value.forEach(visit);
      return;
    }
    if (!React.isValidElement(value)) return;
    if (predicate(value)) found.push(value as T);
    visit((value.props as { children?: React.ReactNode }).children);
  };
  visit(node);
  return found;
}

describe('retention policy controls', () => {
  it('loads the saved path-free policy from the settings API', async () => {
    const fetcher = vi.fn(async () => response(policy));

    await expect(loadRetentionPolicy(fetcher)).resolves.toEqual(policy);
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/retention/policy', {
      method: 'GET',
      headers: { Accept: 'application/json' },
    });
  });

  it('saves only explicit bounded ages and scheduled-cleanup consent', async () => {
    const fetcher = vi.fn(async () => response({ ...policy, revision: 5, enabled: true, requires_review: false }));
    const values = {
      expected_revision: 4,
      expected_config_fingerprint: policy.config_fingerprint,
      enabled: true,
      max_age_days_by_rule: { 'debug-images': 21, 'temporary-html': 14 },
      confirmed: true as const,
    };

    const saved = await saveRetentionPolicy(fetcher, values);

    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/retention/policy', {
      method: 'PUT',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify(values),
    });
    expect(saved.enabled).toBe(true);
    expect(saved.revision).toBe(5);
  });

  it('maps stale revision errors to a safe reload message without exposing server detail', async () => {
    const fetcher = vi.fn(async () => response({ detail: { code: 'RETENTION_POLICY_CONFIG_CHANGED', path: 'C:\\private\\data' } }, 409));
    const onReload = vi.fn();
    const onSave = vi.fn();

    await expect(saveRetentionPolicy(fetcher, {
      expected_revision: 4,
      expected_config_fingerprint: policy.config_fingerprint,
      enabled: true,
      max_age_days_by_rule: { 'debug-images': 21, 'temporary-html': 14 },
      confirmed: true,
    })).rejects.toMatchObject({ code: 'RETENTION_POLICY_CONFIG_CHANGED' });
    const html = renderToStaticMarkup(<RetentionPolicyEditor
      policy={policy}
      ages={{ 'debug-images': 21, 'temporary-html': 10 }}
      enabled={false}
      confirmed={false}
      errorCode="RETENTION_POLICY_CONFIG_CHANGED"
      saving={false}
      onAgeChange={vi.fn()}
      onEnabledChange={vi.fn()}
      onConfirmedChange={vi.fn()}
      onSave={onSave}
      onReload={onReload}
    />);

    expect(html).toContain('Server retention configuration changed. Reload and review the new settings before saving again.');
    expect(html).toContain('Reload settings');
    expect(html).not.toContain('C:\\private');
    const reload = findElements<React.ReactElement<{ onClick?: () => void }>>(RetentionPolicyEditor({
      policy,
      ages: { 'debug-images': 21, 'temporary-html': 10 },
      enabled: false,
      confirmed: false,
      errorCode: 'RETENTION_POLICY_CONFIG_CHANGED',
      saving: false,
      onAgeChange: vi.fn(),
      onEnabledChange: vi.fn(),
      onConfirmedChange: vi.fn(),
      onSave,
      onReload,
    }), element => element.type === 'button' && (element.props as { children?: React.ReactNode }).children === 'Reload settings')[0];
    reload?.props.onClick?.();
    expect(onReload).toHaveBeenCalledTimes(1);
    expect(onSave).not.toHaveBeenCalled();
  });

  it('renders configured categories with bounded age controls and requires acknowledgement to enable cleanup', () => {
    const onEnabledChange = vi.fn();
    const onAgeChange = vi.fn();
    const onConfirmedChange = vi.fn();
    const onSave = vi.fn();
    const view = RetentionPolicyEditor({
      policy,
      ages: { 'debug-images': 21, 'temporary-html': 14 },
      enabled: true,
      confirmed: false,
      errorCode: null,
      saving: false,
      onAgeChange,
      onEnabledChange,
      onConfirmedChange,
      onSave,
      onReload: vi.fn(),
    });
    const inputs = findElements<React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>>(view, element => element.type === 'input');
    const age = inputs.find(element => element.props['aria-label'] === 'Maximum age for Debug screenshots');
    const enabled = inputs.find(element => element.props['aria-label'] === 'Enable scheduled cleanup');
    const confirmation = inputs.find(element => element.props['aria-label'] === 'I understand cleanup applies only to configured transient data and never to application history.');
    const save = findElements<React.ReactElement<{ onClick?: () => void }>>(view, element => element.type === 'button' && (element.props as { children?: React.ReactNode }).children === 'Save retention settings')[0];

    expect(age?.props.min).toBe(1);
    expect(age?.props.max).toBe(365);
    age?.props.onChange?.({ currentTarget: { value: '9' } } as React.ChangeEvent<HTMLInputElement>);
    enabled?.props.onChange?.({ currentTarget: { checked: true } } as React.ChangeEvent<HTMLInputElement>);
    confirmation?.props.onChange?.({ currentTarget: { checked: true } } as React.ChangeEvent<HTMLInputElement>);
    save?.props.onClick?.();

    expect(onAgeChange).toHaveBeenCalledWith('debug-images', 9);
    expect(onEnabledChange).toHaveBeenCalledWith(true);
    expect(onConfirmedChange).toHaveBeenCalledWith(true);
    expect(onSave).toHaveBeenCalledTimes(1);

    const html = renderToStaticMarkup(view);
    expect(html).toContain('role="status"');
    expect(html).toContain('Core application history is never eligible for cleanup.');
    expect(html).not.toMatch(/run now|delete now|cleanup now/i);
    expect(html).not.toContain('debug-images');
  });

  it('keeps cleanup disabled when the server admin gate is off and renders initial loading state', () => {
    const locked = { ...policy, pruning_allowed: false };
    const view = RetentionPolicyEditor({
      policy: locked,
      ages: { 'debug-images': 21, 'temporary-html': 14 },
      enabled: false,
      confirmed: false,
      errorCode: null,
      saving: false,
      onAgeChange: vi.fn(),
      onEnabledChange: vi.fn(),
      onConfirmedChange: vi.fn(),
      onSave: vi.fn(),
      onReload: vi.fn(),
    });
    const checkbox = findElements<React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>>(view, element => element.type === 'input')
      .find(element => element.props['aria-label'] === 'Enable scheduled cleanup');
    const confirmation = findElements<React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>>(view, element => element.type === 'input')
      .find(element => element.props['aria-label'] === 'I understand cleanup applies only to configured transient data and never to application history.');
    const html = renderToStaticMarkup(<RetentionPolicyControls />);

    expect(checkbox?.props.disabled).toBe(true);
    expect(confirmation?.props.disabled).toBe(false);
    expect(renderToStaticMarkup(view)).toContain('Scheduled cleanup is unavailable because server configuration does not permit pruning (pruning disabled or dry-run mode active).');
    expect(html).toContain('role="status"');
    expect(html).toContain('Loading retention settings');
  });

  it('rejects unsafe or malformed policy responses without echoing server data', async () => {
    const malformed = { ...policy, rules: [{ ...policy.rules[0], path: 'C:\\private\\data' }] };
    const fetcher = vi.fn(async () => response(malformed));

    await expect(loadRetentionPolicy(fetcher)).rejects.toMatchObject({ code: 'RETENTION_POLICY_INVALID_RESULT' });
    await expect(loadRetentionPolicy(fetcher)).rejects.toMatchObject({ message: 'The server returned settings that could not be safely displayed.' });
  });

  it('does not offer scheduled cleanup when the server has no configured managed categories', () => {
    const noRules = { ...policy, rules: [] };
    const view = RetentionPolicyEditor({
      policy: noRules,
      ages: {},
      enabled: false,
      confirmed: true,
      errorCode: null,
      saving: false,
      onAgeChange: vi.fn(),
      onEnabledChange: vi.fn(),
      onConfirmedChange: vi.fn(),
      onSave: vi.fn(),
      onReload: vi.fn(),
    });
    const enabled = findElements<React.ReactElement<React.InputHTMLAttributes<HTMLInputElement>>>(view, element => element.type === 'input')
      .find(element => element.props['aria-label'] === 'Enable scheduled cleanup');

    expect(enabled?.props.disabled).toBe(true);
    expect(renderToStaticMarkup(view)).toContain('No managed transient categories are configured for cleanup.');
  });
});
