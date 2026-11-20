import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  ApplicationRehearsal,
  ApplicationRehearsalView,
  createApplicationRehearsalClient,
  type ApplicationRehearsalField,
  type ApplicationRehearsalSnapshot,
} from './application-rehearsal';

const profileId = 'profile/a';
const applicationId = 'application&b';
const profileVersion = '19d5918fa4de2a66b301a612a8ab7564b1da2b64793b855cbc5c3458960cea08';
const field: ApplicationRehearsalField = {
  key: 'email', label: 'Email address', type: 'email', required: true,
  value: 'local.person@example.test', source: 'CONTACT', reason_code: null,
};
const unresolved: ApplicationRehearsalField = {
  key: 'work_auth', label: 'Work authorization', type: 'select', required: true,
  value: null, source: 'UNRESOLVED', reason_code: 'NO_APPROVED_ANSWER',
};
const snapshot: ApplicationRehearsalSnapshot = {
  profile_id: profileId,
  application_id: applicationId,
  profile_version: profileVersion,
  route_id: 'route-greenhouse',
  fingerprint: 'a'.repeat(64),
  fields: [field, unresolved],
  blockers: ['MISSING_REQUIRED_FIELD'],
  ready: false,
  submission_enabled: false,
  latest_review: null,
};
type TreeProps = { children?: ReactNode };
type ButtonProps = TreeProps & { type?: string; disabled?: boolean; onClick?: () => void };
type InputProps = TreeProps & { type?: string; checked?: boolean; disabled?: boolean; onChange?: (event: { currentTarget: { checked: boolean } }) => void };
function findElements<T extends TreeProps>(node: ReactNode, type: string): ReactElement<T>[] {
  return Children.toArray(node).flatMap((child) => {
    if (!isValidElement<T>(child)) return [];
    return [...(child.type === type ? [child] : []), ...findElements<T>(child.props.children, type)];
  });
}

describe('local application rehearsal', () => {
  it('loads a profile-scoped local rehearsal with GET and no cache', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(snapshot));
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);

    await expect(client.load()).resolves.toEqual(snapshot);
    expect(fetcher).toHaveBeenCalledWith(
      '/profiles/profile%2Fa/applications/application%26b/rehearsal',
      expect.objectContaining({ method: 'GET', cache: 'no-store', credentials: 'same-origin' }),
    );
  });

  it('accepts the API hash version on the snapshot and latest review receipt', async () => {
    const hydrated = {
      ...snapshot,
      latest_review: {
        request_id: 'receipt-1', fingerprint: snapshot.fingerprint,
        profile_version: profileVersion, reviewed_at: '2026-10-11T15:00:00Z', acknowledged: true as const,
      },
    };
    const client = createApplicationRehearsalClient(profileId, applicationId,
      vi.fn<typeof fetch>().mockResolvedValue(Response.json(hydrated)));

    await expect(client.load()).resolves.toEqual(hydrated);
  });

  it('renders values as read-only text, explicit sources, and safe missing-field reasons', () => {
    const html = renderToStaticMarkup(<ApplicationRehearsalView
      snapshot={snapshot} reviewChecked={false} reviewPending={false} notice={null}
      onReviewCheck={vi.fn()} onRecordReview={vi.fn()}
    />);

    expect(html).toContain('local.person@example.test');
    expect(html).toContain('Profile contact');
    expect(html).toContain('Work authorization');
    expect(html).toContain('No approved saved answer is available for this required field.');
    expect(html).toContain('required');
    expect(html).toContain('External submission is disabled');
    expect(html).not.toMatch(/<button[^>]*>[^<]*(submit|upload|autofill)[^<]*<\/button>/i);
    expect(html).not.toMatch(/<(input|textarea|select)[^>]*value=/i);
  });

  it('offers only explicit local review acknowledgement and keeps the receipt separate from submission', () => {
    const onRecordReview = vi.fn();
    const tree = ApplicationRehearsalView({
      snapshot, reviewChecked: true, reviewPending: false, notice: null,
      onReviewCheck: vi.fn(), onRecordReview,
    });
    const buttons = findElements<ButtonProps>(tree, 'button');
    const checkboxes = findElements<InputProps>(tree, 'input');
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('I have reviewed these rehearsal values locally');
    expect(html).toContain('Record local review receipt');
    expect(buttons).toHaveLength(1);
    expect(buttons[0].props.type).toBe('button');
    expect(checkboxes).toHaveLength(1);
    buttons[0].props.onClick?.();
    expect(onRecordReview).toHaveBeenCalledExactlyOnceWith();
    expect(html).not.toContain('Submitted');
  });

  it('shows a latest persisted receipt without implying that an application was submitted', () => {
    const receiptSnapshot = {
      ...snapshot,
      latest_review: {
        request_id: 'receipt-1', fingerprint: snapshot.fingerprint,
        profile_version: profileVersion, reviewed_at: '2026-10-11T15:00:00Z', acknowledged: true as const,
      },
    };
    const html = renderToStaticMarkup(<ApplicationRehearsalView
      snapshot={receiptSnapshot} reviewChecked={false} reviewPending={false} notice={null}
      onReviewCheck={vi.fn()} onRecordReview={vi.fn()}
    />);

    expect(html).toContain('Review receipt recorded locally');
    expect(html).not.toContain('Submitted');
    expect(html).not.toContain('receipt-1');
  });

  it('posts only the review receipt metadata and never sends rehearsal values', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response('', { status: 201 }));
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);
    const request = { request_id: 'request-1', fingerprint: snapshot.fingerprint, acknowledged: true as const };

    await client.recordReview(request);

    expect(fetcher).toHaveBeenCalledWith(
      '/profiles/profile%2Fa/applications/application%26b/rehearsal/reviews',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify(request),
      }),
    );
    expect(JSON.stringify(fetcher.mock.calls[0][1])).not.toContain(field.value);
  });

  it('classifies a stale 409 without exposing server response details', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      detail: { code: 'APPLICATION_REHEARSAL_STALE', secret: 'PRIVATE_VALUE' },
    }, { status: 409 }));
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);

    await expect(client.recordReview({
      request_id: 'request-1', fingerprint: snapshot.fingerprint, acknowledged: true,
    })).rejects.toMatchObject({ code: 'STALE' });
  });

  it('rejects malformed, cross-scope, or externally enabled rehearsal snapshots', async () => {
    const badSnapshots = [
      { ...snapshot, application_id: 'other-application' },
      { ...snapshot, profile_version: 4 },
      { ...snapshot, latest_review: {
        request_id: 'receipt-1', fingerprint: snapshot.fingerprint,
        profile_version: 4, reviewed_at: '2026-10-11T15:00:00Z', acknowledged: true,
      } },
      { ...snapshot, submission_enabled: true },
      { ...snapshot, fingerprint: '' },
    ];
    for (const body of badSnapshots) {
      const client = createApplicationRehearsalClient(profileId, applicationId,
        vi.fn<typeof fetch>().mockResolvedValue(Response.json(body)));
      await expect(client.load()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
    }
  });

  it('does not expose raw GET errors or server detail', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      detail: 'PRIVATE_PROFILE_VALUE',
    }, { status: 404 }));
    await expect(createApplicationRehearsalClient(profileId, applicationId, fetcher).load())
      .rejects.toMatchObject({ code: 'NOT_FOUND' });
  });

  it('renders loading during mount without triggering external or write actions', () => {
    const fetcher = vi.fn<typeof fetch>();
    const html = renderToStaticMarkup(<ApplicationRehearsal
      profileId={profileId} applicationId={applicationId} fetcher={fetcher}
    />);
    expect(html).toContain('Loading local rehearsal');
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('does not show absolute paths for owned document values', () => {
    const documentSnapshot = {
      ...snapshot,
      fields: [{ ...field, key: 'resume', label: 'Resume', type: 'file', source: 'DOCUMENT' as const,
        value: 'C:\\private\\owned\\resume.pdf' }],
    };
    const html = renderToStaticMarkup(<ApplicationRehearsalView
      snapshot={documentSnapshot} reviewChecked={false} reviewPending={false} notice={null}
      onReviewCheck={vi.fn()} onRecordReview={vi.fn()}
    />);

    expect(html).toContain('resume.pdf');
    expect(html).not.toContain('C:\\private');
    expect(html).not.toContain('owned\\resume');
  });
});
describe('rehearsal field-key contract', () => {
  it.each([
    { description: 'nested', key: 'candidate[first_name]' },
    { description: 'array', key: 'field[]' },
    { description: 'leading underscore', key: '_custom' },
    { description: '100-character', key: 'a'.repeat(100) },
  ])('retains a backend-safe $description field key without rewriting it', async ({ key }) => {
    const body = {
      ...snapshot,
      fields: [{ ...unresolved, key, label: key, reason_code: 'FORM_FIELD_REVIEW_REQUIRED' }],
      blockers: ['FORM_FIELD_REVIEW_REQUIRED', 'REQUIRED_FIELD_UNRESOLVED'],
    };
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json(body));
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);

    await expect(client.load()).resolves.toEqual(body);
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it('keeps backend-safe unresolved fields and blockers visible', async () => {
    const keys = ['candidate[first_name]', 'field[]', '_custom'];
    const body = {
      ...snapshot,
      fields: [field, ...keys.map((key) => ({
        ...unresolved, key, label: key, reason_code: 'FORM_FIELD_REVIEW_REQUIRED',
      }))],
      blockers: ['FORM_FIELD_REVIEW_REQUIRED', 'REQUIRED_FIELD_UNRESOLVED'],
    };
    const client = createApplicationRehearsalClient(profileId, applicationId,
      vi.fn<typeof fetch>().mockResolvedValue(Response.json(body)));
    const loaded = await client.load();
    const html = renderToStaticMarkup(<ApplicationRehearsalView
      snapshot={loaded} reviewChecked={false} reviewPending={false} notice={null}
      onReviewCheck={vi.fn()} onRecordReview={vi.fn()}
    />);

    expect(loaded).toEqual(body);
    for (const key of keys) {
      expect(html).toContain('aria-label="' + key + ' rehearsal value">No value populated</output>');
    }
    expect(html.match(/>Unresolved<\/dd>/g)).toHaveLength(3);
    expect(html).toContain('One or more form fields need manual review.');
    expect(html).toContain('One or more required fields do not have a verified value.');
    expect(html).toContain('local.person@example.test');
    expect(html).toContain('External submission is disabled');
    expect(html).not.toMatch(/<(input|textarea|select)[^>]*value=/i);
  });

  const invalidKeys: Array<{ description: string; key: unknown }> = [
    { description: '101 characters', key: 'a'.repeat(101) },
    { description: 'empty string', key: '' },
    { description: 'leading whitespace', key: ' email' },
    { description: 'embedded whitespace', key: 'first name' },
    { description: 'trailing whitespace', key: 'email ' },
    { description: 'embedded control', key: 'field\u0000name' },
    { description: 'tab', key: 'field\tname' },
    { description: 'trailing newline', key: 'email\n' },
    { description: 'slash', key: 'candidate/name' },
    { description: 'markup', key: '<input>' },
    { description: 'leading bracket', key: '[field]' },
    { description: 'leading dash', key: '-field' },
    { description: 'non-ASCII character', key: 'na\u00efve' },
    { description: 'number', key: 1 },
    { description: 'null', key: null },
    { description: 'array', key: ['name'] },
    { description: 'object', key: { key: 'name' } },
    { description: 'boolean', key: true },
  ];
  it.each(invalidKeys)('rejects a field key outside the backend contract: $description', async ({ key }) => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...snapshot, fields: [{ ...field, key }],
    }));
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);

    await expect(client.load()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });

  it.each(['candidate[first_name]', '_custom'])(
    'does not accept a form key as review request ID: %s', async (requestId) => {
      const fetcher = vi.fn<typeof fetch>();
      const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);

      await expect(client.recordReview({
        request_id: requestId, fingerprint: snapshot.fingerprint, acknowledged: true,
      })).rejects.toMatchObject({ code: 'INVALID_REQUEST' });
      expect(fetcher).not.toHaveBeenCalled();
    });

  it.each(['candidate[first_name]', '_custom'])(
    'does not accept a form key as persisted receipt ID: %s', async (requestId) => {
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
        ...snapshot,
        latest_review: {
          request_id: requestId, fingerprint: snapshot.fingerprint,
          profile_version: profileVersion, reviewed_at: '2026-10-11T15:00:00Z', acknowledged: true,
        },
      }));
      const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);

      await expect(client.load()).rejects.toMatchObject({ code: 'INVALID_RESPONSE' });
    });

  it('preserves the distinct 128-character review request and receipt ID limit', async () => {
    const requestId = 'r'.repeat(128);
    const body = {
      ...snapshot,
      latest_review: {
        request_id: requestId, fingerprint: snapshot.fingerprint,
        profile_version: profileVersion, reviewed_at: '2026-10-11T15:00:00Z', acknowledged: true,
      },
    };
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(Response.json(body))
      .mockResolvedValueOnce(new Response('', { status: 201 }));
    const client = createApplicationRehearsalClient(profileId, applicationId, fetcher);
    const request = { request_id: requestId, fingerprint: snapshot.fingerprint, acknowledged: true as const };

    await expect(client.load()).resolves.toEqual(body);
    await client.recordReview(request);
    expect(fetcher).toHaveBeenCalledTimes(2);
    expect(fetcher.mock.calls[1][1]).toMatchObject({ method: 'POST', body: JSON.stringify(request) });
  });
});
