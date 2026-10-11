import { Children, isValidElement, type ReactElement, type ReactNode } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { ApplicationFormReview, type ApplicationFormReviewField } from './application-form-review';

type ButtonProps = { children?: ReactNode; type?: string; disabled?: boolean; onClick?: () => void };

function findButtons(node: ReactNode): ReactElement<ButtonProps>[] {
  return Children.toArray(node).flatMap(child => {
    if (!isValidElement<ButtonProps>(child)) return [];
    return [...(child.type === 'button' ? [child] : []), ...findButtons(child.props.children)];
  });
}

const fields: ApplicationFormReviewField[] = [
  { name: 'Email address', type: 'email', required: true, reviewRequired: true },
  { name: 'Preferred name', type: 'text', required: false, reviewRequired: false },
];

const render = (props: Partial<Parameters<typeof ApplicationFormReview>[0]> = {}) =>
  ApplicationFormReview({ status: 'idle', fields: [], onInspect: vi.fn(), ...props });

describe('application form read-only review', () => {
  it('offers an explicit read-only inspect action and no form submission controls', () => {
    const onInspect = vi.fn();
    const tree = render({ onInspect });
    const html = renderToStaticMarkup(tree);
    const buttons = findButtons(tree);

    expect(html).toContain('Inspect form fields');
    expect(html).toContain('read-only');
    expect(buttons).toHaveLength(1);
    expect(buttons[0].props.type).toBe('button');
    expect(html).not.toMatch(/<button[^>]*>[^<]*(submit|upload|autofill)[^<]*<\/button>/i);
    buttons[0].props.onClick?.();
    expect(onInspect).toHaveBeenCalledExactlyOnceWith();
  });

  it('announces pending inspection and prevents duplicate inspect actions', () => {
    const tree = render({ status: 'pending', onInspect: vi.fn() });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('role="status"');
    expect(html).toContain('Inspecting the application form');
    expect(findButtons(tree)[0].props.disabled).toBe(true);
  });

  it('shows only sanitized field metadata after a completed inspection', () => {
    const fieldWithPrivateValue = { ...fields[0], value: 'private-applicant@example.test' };
    const tree = render({ status: 'completed', fields: [fieldWithPrivateValue] });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Email address');
    expect(html).toContain('email');
    expect(html).toContain('Required');
    expect(html).toContain('Review required');
    expect(html).not.toContain('private-applicant@example.test');
    expect(html).not.toContain('value=');
  });

  it('sanitizes unsafe field labels and limits field metadata to safe text', () => {
    const unsafeField: ApplicationFormReviewField = {
      name: 'Contact person@example.test <script> alert(1)',
      type: 'email; access_token=hidden',
      required: true,
      reviewRequired: true,
    };
    const html = renderToStaticMarkup(render({ status: 'completed', fields: [unsafeField] }));

    expect(html).not.toContain('person@example.test');
    expect(html).not.toContain('<script>');
    expect(html).not.toContain('access_token');
    expect(html).toContain('Contact');
    expect(html).toContain('Other');
  });

  it('shows fixed unavailable guidance without enabling inspection', () => {
    const tree = render({ status: 'unavailable' });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Form inspection is unavailable');
    expect(html).toContain('role="status"');
    expect(findButtons(tree)[0].props.disabled).toBe(true);
  });

  it('shows fixed error guidance and does not expose supplied provider error content', () => {
    const privateError = 'PRIVATE_RESPONSE applicant@example.test API_KEY=secret';
    const tree = ApplicationFormReview({
      status: 'error', fields, onInspect: vi.fn(), error: privateError,
    } as Parameters<typeof ApplicationFormReview>[0] & { error: string });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Inspection could not be completed');
    expect(html).toContain('Try again');
    expect(html).not.toContain(privateError);
    expect(html).not.toContain('applicant@example.test');
    expect(html).not.toContain('API_KEY');
  });

  it('falls back safely for unknown statuses and ignores supplied field values', () => {
    const tree = ApplicationFormReview({
      status: 'unknown', fields, onInspect: vi.fn(), error: 'raw provider detail',
    } as Parameters<typeof ApplicationFormReview>[0] & { error: string });
    const html = renderToStaticMarkup(tree);

    expect(html).toContain('Inspection could not be completed');
    expect(html).not.toContain('raw provider detail');
    expect(html).not.toContain('Email address');
  });
});
