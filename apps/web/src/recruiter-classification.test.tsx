import React, { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  RecruiterClassificationCardView,
  RecruiterClassificationCard,
  RecruiterClassificationController,
  type RecruiterClassification,
  type RecruiterClassificationClient,
  type RecruiterClassificationCardState,
} from './recruiter-classification';

function findButtons(node: ReactNode): ReactElement<{
  children?: ReactNode;
  onClick?: () => void;
  'aria-label'?: string;
}>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<{
      children?: ReactNode;
      onClick?: () => void;
      'aria-label'?: string;
    }>(child)) return [];
    const current = child.type === 'button' ? [child] : [];
    return [...current, ...findButtons(child.props.children)];
  });
}

const makeClassification = (
  classification: RecruiterClassification['classification'],
): RecruiterClassification => ({
  job_id: 'job-1',
  classification,
  explanation: `Evidence currently supports ${classification.toLowerCase().replaceAll('_', ' ')} classification.`,
  evidence: [{ signal: 'Company relationship', detail: 'Posting names the hiring employer.', source: 'Job description' }],
});

const idle: RecruiterClassificationCardState = {
  jobId: 'job-1', result: null, loading: false, error: null,
};

describe('recruiter classification card', () => {
  it('uses the injected client without requesting classification automatically', () => {
    const client: RecruiterClassificationClient = { classify: vi.fn() };
    const markup = renderToStaticMarkup(<RecruiterClassificationCard jobId="job-1" client={client} />);

    expect(markup).toContain('No recruiter classification is available yet.');
    expect(markup).toContain('Classify employer relationship');
    expect(client.classify).not.toHaveBeenCalled();
  });

  it('renders all supported recruiter classifications as advisory labels', () => {
    for (const label of ['DIRECT_EMPLOYER', 'STAFFING_AGENCY', 'RECRUITER', 'UNKNOWN'] as const) {
      const result = makeClassification(label);
      const markup = renderToStaticMarkup(
        <RecruiterClassificationCardView
          state={{ ...idle, result }}
          onClassify={vi.fn()}
          onRetry={vi.fn()}
        />,
      );
      expect(markup).toContain(label.replaceAll('_', ' '));
      expect(markup).toContain('advisory');
      expect(markup).toContain('does not automatically reject staffing firms');
      expect(markup).toContain(result.explanation);
      expect(markup).toContain('Company relationship');
      expect(markup).toContain('Posting names the hiring employer.');
      expect(markup).toContain('Job description');
      expect(markup).not.toMatch(/<button[^>]*>[^<]*(block|reject)/i);
      expect(markup).not.toMatch(/aria-label="[^"]*(block|reject)/i);
    }
  });

  it('shows an explicit unknown result when evidence is absent', () => {
    const result = {
      ...makeClassification('UNKNOWN'),
      explanation: 'There is not enough evidence to classify the employer relationship.',
      evidence: [],
    };
    const markup = renderToStaticMarkup(
      <RecruiterClassificationCardView state={{ ...idle, result }} onClassify={vi.fn()} onRetry={vi.fn()} />,
    );

    expect(markup).toContain('UNKNOWN');
    expect(markup).toContain(result.explanation);
    expect(markup).toContain('No classification evidence is available.');
    expect(markup).not.toContain('DIRECT EMPLOYER');
  });

  it('shows loading, empty, and retryable error states accessibly', () => {
    const onClassify = vi.fn();
    const onRetry = vi.fn();
    const loading = renderToStaticMarkup(
      <RecruiterClassificationCardView state={{ ...idle, loading: true }} onClassify={onClassify} onRetry={onRetry} />,
    );
    expect(loading).toContain('role="status"');
    expect(loading).toContain('Loading employer classification');

    const empty = renderToStaticMarkup(
      <RecruiterClassificationCardView state={idle} onClassify={onClassify} onRetry={onRetry} />,
    );
    expect(empty).toContain('No recruiter classification is available yet.');
    expect(empty).toContain('Classify employer relationship');

    const errorState = { ...idle, error: 'Employer classification is temporarily unavailable.' };
    const tree = RecruiterClassificationCardView({ state: errorState, onClassify, onRetry });
    const markup = renderToStaticMarkup(tree);
    expect(markup).toContain('role="alert"');
    expect(markup).toContain(errorState.error);
    expect(markup).toContain('Retry classification');
    const retry = findButtons(tree).find(button => button.props['aria-label'] === 'Retry classification');
    retry?.props.onClick?.();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('requests classification only after an explicit action and retries failures through the injected client', async () => {
    let finish!: (value: RecruiterClassification) => void;
    const client: RecruiterClassificationClient = {
      classify: vi.fn().mockImplementation(() => new Promise(resolve => { finish = resolve; })),
    };
    const controller = new RecruiterClassificationController(client);
    expect(client.classify).not.toHaveBeenCalled();
    expect(controller.getSnapshot()).toMatchObject({ loading: false, result: null, error: null });

    const firstAttempt = controller.classify('job-1');
    expect(client.classify).toHaveBeenCalledExactlyOnceWith('job-1');
    expect(controller.getSnapshot()).toMatchObject({ jobId: 'job-1', loading: true, result: null, error: null });
    finish(makeClassification('STAFFING_AGENCY'));
    await firstAttempt;
    expect(controller.getSnapshot()).toMatchObject({ loading: false, result: makeClassification('STAFFING_AGENCY'), error: null });

    const retryClient: RecruiterClassificationClient = {
      classify: vi.fn().mockRejectedValueOnce(new Error('temporary')).mockResolvedValue(makeClassification('UNKNOWN')),
    };
    const retryController = new RecruiterClassificationController(retryClient);
    await retryController.classify('job-1');
    expect(retryController.getSnapshot()).toMatchObject({ loading: false, error: 'Employer classification could not be loaded. Try again.' });
    await retryController.retry();
    expect(retryClient.classify).toHaveBeenCalledTimes(2);
    expect(retryController.getSnapshot()).toMatchObject({ loading: false, result: makeClassification('UNKNOWN'), error: null });
  });

  it('fails closed when the injected client returns a result for another job', async () => {
    const client: RecruiterClassificationClient = {
      classify: vi.fn().mockResolvedValue({ ...makeClassification('DIRECT_EMPLOYER'), job_id: 'other-job' }),
    };
    const controller = new RecruiterClassificationController(client);
    await controller.classify('job-1');

    expect(controller.getSnapshot()).toMatchObject({
      jobId: 'job-1', result: null, loading: false,
      error: 'Employer classification could not be loaded. Try again.',
    });
  });
});
