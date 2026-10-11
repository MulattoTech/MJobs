import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import { DocumentGenerationFailure } from './document-generation-failure';

function findElement(node: React.ReactNode, type: string): React.ReactElement<any> | undefined {
  if (Array.isArray(node)) {
    for (const child of node) {
      const found = findElement(child, type);
      if (found) return found;
    }
    return undefined;
  }

  if (!React.isValidElement<any>(node)) return undefined;
  const element = node as React.ReactElement<{ children?: React.ReactNode }>;
  if (element.type === type) return element;
  return findElement(element.props.children, type);
}

describe('document generation failure', () => {
  it('announces the canonical failure and makes clear that no usable document was produced', () => {
    const html = renderToStaticMarkup(<DocumentGenerationFailure />);

    expect(html).toContain('<section class="panel" aria-labelledby="document-generation-failure-title">');
    expect(html).toContain('<h2 id="document-generation-failure-title">Document generation failed</h2>');
    expect(html).toContain('<div class="alert" role="alert">');
    expect(html).toContain('DOCUMENT_GENERATION_FAILED');
    expect(html).toContain('No complete document was produced. Do not use or submit an incomplete file.');
    expect(html).not.toContain('href=');
    expect(html).not.toContain('<button');
  });

  it('exposes retry only when the caller confirms it is supported', () => {
    const onRetry = vi.fn();
    const section = DocumentGenerationFailure({
      retry: {
        operationStatus: 'DEAD_LETTERED',
        recovery: 'RETRY',
        safeToRetry: true,
        onRetry,
      },
    });
    const button = findElement(section.props.children, 'button');

    expect(button?.props.type).toBe('button');
    expect(button?.props.children).toBe('Retry document generation');
    expect(onRetry).not.toHaveBeenCalled();
    button?.props.onClick?.();
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it('does not expose retry when no recovery callback is available', () => {
    const html = renderToStaticMarkup(<DocumentGenerationFailure />);

    expect(html).not.toContain('<button');
    expect(html).toContain('Review the source details before starting another generation.');
  });

  it('does not expose retry for regeneration recovery even when a callback exists', () => {
    const html = renderToStaticMarkup(
      <DocumentGenerationFailure retry={{
        operationStatus: 'DOCUMENT_GENERATION_FAILED',
        recovery: 'REGENERATE',
        safeToRetry: false,
        onRetry: vi.fn(),
      }} />,
    );

    expect(html).not.toContain('<button');
    expect(html).toContain('Review the source details before starting another generation.');
  });

  it('does not expose retry for a dead-lettered operation unless safe retry is explicit', () => {
    const html = renderToStaticMarkup(
      <DocumentGenerationFailure retry={{
        operationStatus: 'DEAD_LETTERED',
        recovery: 'RETRY',
        safeToRetry: false,
        onRetry: vi.fn(),
      }} />,
    );

    expect(html).not.toContain('<button');
  });
});
