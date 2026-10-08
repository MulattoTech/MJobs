import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { RoleLensPicker, type RoleLensPickerProps, type RoleLensSelectionApiResponse, type RoleLensSelectionPreview } from './role-lens-picker';

function findElements(node: ReactNode, typeName: string): ReactElement<{
  children?: ReactNode;
  value?: string;
  onChange?: (event: {target: {value: string}}) => void;
  onClick?: () => void;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{children?: ReactNode;value?: string;onChange?: (event: {target: {value: string}}) => void;onClick?: () => void}>(child)) return [];
    const current = child.type === typeName ? [child] : [];
    return [...current, ...findElements(child.props.children, typeName)];
  });
}

const options = [
  {id: 'support', version: 1, display_name: 'Support', description: 'Prioritize approved evidence about support and issue resolution.'},
  {id: 'systems', version: 1, display_name: 'Systems', description: 'Prioritize approved evidence about systems administration and infrastructure.'},
];

const explicitPreview: RoleLensSelectionPreview = {
  schema_version: 1,
  profile_id: 'profile-1',
  profile_version: 'profile-version-1',
  role_text: 'Systems Administrator',
  lens: options[1],
  selection_reason: 'EXPLICIT',
  matched_role_terms: ['systems administrator'],
  fact_ids: ['verified-fact-1'],
  emphasized_fact_ids: ['verified-fact-1'],
  story_ids: ['approved-story-revision-2'],
  emphasized_story_ids: ['approved-story-revision-2'],
  source_refs: {
    facts: [{fact_id: 'verified-fact-1', fingerprint: 'verified-fingerprint'}],
    stories: [{story_id: 'approved-story-revision-2', story_key: 'story-key-1', revision: 2, profile_version: 'profile-version-1', fact_ids: ['verified-fact-1'], fact_fingerprints: [{fact_id: 'verified-fact-1', fingerprint: 'verified-fingerprint'}]}],
  },
};

const explicitResponse: RoleLensSelectionApiResponse = {
  schema_version: 1,
  profile_id: 'profile-1',
  profile_version: 'profile-version-1',
  job_id: 'job-1',
  job_fingerprint: 'job-fingerprint-1',
  role_lenses: options,
  selection: explicitPreview,
};

const suggestedResponse: RoleLensSelectionApiResponse = {
  ...explicitResponse,
  selection: {...explicitPreview, lens: options[0], selection_reason: 'ROLE_MATCH'},
};

const baseProps: RoleLensPickerProps = {
  profileId: 'profile-1',
  jobId: 'job-1',
  roleLenses: options,
  suggestedResponse,
  selectedResponse: null,
  requestedLens: null,
  loading: false,
  loadingError: null,
  selecting: false,
  selectionError: null,
  onRetry: () => {},
  onRetrySelection: () => {},
  onSelectLens: () => {},
};

describe('role lens picker', () => {
  it('starts with no explicitly selected lens while identifying the server suggestion as unselected', () => {
    const html = renderToStaticMarkup(<RoleLensPicker {...baseProps} />);
    const tree = RoleLensPicker(baseProps);
    const selector = findElements(tree, 'select')[0];

    expect(html).toContain('Suggested lens: Support (version 1). This suggestion is not selected.');
    expect(html).toContain('aria-label="Role lens for this job"');
    expect(html).toContain('Support · version 1');
    expect(html).toContain('Systems · version 1');
    expect(selector.props.value).toBe('');
  });

  it('requests the exact selected lens version and displays only the confirmed readback as selected', () => {
    const onSelectLens = vi.fn();
    const props = {...baseProps, onSelectLens};
    const tree = RoleLensPicker(props);
    const selector = findElements(tree, 'select')[0];

    selector.props.onChange?.({target: {value: 'systems@1'}});
    expect(onSelectLens).toHaveBeenCalledExactlyOnceWith({id: 'systems', version: 1});

    const html = renderToStaticMarkup(<RoleLensPicker
      {...props}
      requestedLens={{id: 'systems', version: 1}}
      selecting
    />);
    expect(html).toContain('Confirming the selected lens version with a job-scoped readback');
    expect(html).not.toContain('Selected lens: Systems (version 1) is confirmed for this job.');
  });

  it('shows the confirmed lens explanation and evidence references separately from verified facts', () => {
    const html = renderToStaticMarkup(<RoleLensPicker
      {...baseProps}
      requestedLens={{id: 'systems', version: 1}}
      selectedResponse={explicitResponse}
    />);

    expect(html).toContain('Selected lens: Systems (version 1) is confirmed for this job.');
    expect(html).toContain(options[1].description);
    expect(html).toContain('Verified facts remain the source evidence');
    expect(html).toContain('it cannot create or change facts.');
    expect(html).toContain('verified-fact-1');
    expect(html).toContain('verified-fingerprint');
    expect(html).toContain('approved-story-revision-2');
    expect(html).toContain('Story revision 2');
  });

  it('shows loading and retryable catalog errors accessibly', () => {
    const loading = renderToStaticMarkup(<RoleLensPicker {...baseProps} loading />);
    expect(loading).toContain('Loading supported role lenses');
    expect(loading).toContain('role="status"');

    const onRetry = vi.fn();
    const errorProps = {...baseProps, roleLenses: [], suggestedResponse: null, loadingError: 'The role-lens list could not be loaded.', onRetry};
    const html = renderToStaticMarkup(<RoleLensPicker {...errorProps} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('The role-lens list could not be loaded.');
    const retry = findElements(RoleLensPicker(errorProps), 'button')[0];
    retry.props.onClick?.();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('shows a retry for a rejected lens readback without treating the requested version as confirmed', () => {
    const onRetrySelection = vi.fn();
    const props = {
      ...baseProps,
      requestedLens: {id: 'systems', version: 1},
      selecting: false,
      selectionError: 'The selected role lens changed; reload the supported lens list.',
      onRetrySelection,
    };
    const html = renderToStaticMarkup(<RoleLensPicker {...props} />);
    expect(html).toContain('The selected role lens changed; reload the supported lens list.');
    expect(html).not.toContain('Selected lens: Systems (version 1) is confirmed for this job.');
    findElements(RoleLensPicker(props), 'button')[0].props.onClick?.();
    expect(onRetrySelection).toHaveBeenCalledOnce();
  });

  it('does not report an old-version readback as confirmation of the requested version', () => {
    const html = renderToStaticMarkup(<RoleLensPicker
      {...baseProps}
      requestedLens={{id: 'systems', version: 2}}
      selectedResponse={explicitResponse}
    />);

    expect(html).not.toContain('Selected lens: Systems (version 1) is confirmed for this job.');
    expect(html).not.toContain('Selected lens explanation');
  });

  it('rejects a readback returned for a different job or profile', () => {
    const foreignJobHtml = renderToStaticMarkup(<RoleLensPicker
      {...baseProps}
      requestedLens={{id: 'systems', version: 1}}
      selectedResponse={{...explicitResponse, job_id: 'job-foreign'}}
    />);
    const foreignProfileHtml = renderToStaticMarkup(<RoleLensPicker
      {...baseProps}
      requestedLens={{id: 'systems', version: 1}}
      selectedResponse={{...explicitResponse, profile_id: 'profile-foreign'}}
    />);

    for (const html of [foreignJobHtml, foreignProfileHtml]) {
      expect(html).not.toContain('Selected lens: Systems (version 1) is confirmed for this job.');
      expect(html).not.toContain('Selected lens explanation');
    }
  });

  it('rejects a readback if profile or job evidence changed since the suggestion preview', () => {
    const changedProfileVersion = {...explicitResponse, profile_version: 'new-profile-version', selection: {...explicitPreview, profile_version: 'new-profile-version'}};
    const changedJobFingerprint = {...explicitResponse, job_fingerprint: 'new-job-fingerprint'};
    for (const selectedResponse of [changedProfileVersion, changedJobFingerprint]) {
      const html = renderToStaticMarkup(<RoleLensPicker
        {...baseProps}
        requestedLens={{id: 'systems', version: 1}}
        selectedResponse={selectedResponse}
      />);
      expect(html).not.toContain('Selected lens: Systems (version 1) is confirmed for this job.');
    }
  });
});
