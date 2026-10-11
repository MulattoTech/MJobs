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
  profile_version: 4,
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
        profile_version: 4, reviewed_at: '2026-10-11T15:00:00Z', acknowledged: true as const,
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
