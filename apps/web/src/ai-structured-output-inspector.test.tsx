import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AIStructuredOutputInspector,
  CANONICAL_SCHEMA_DEFINITIONS,
} from './ai-structured-output-inspector';

describe('AIStructuredOutputInspector Component', () => {
  it('renders section title and strict validation indicator', () => {
    const html = renderToStaticMarkup(<AIStructuredOutputInspector />);

    expect(html).toContain('Strict AI Output Schemas');
    expect(html).toContain('Strict Validation (Forbid Extra)');
    expect(html).toContain('additionalProperties: false');
  });

  it('renders schema selector tabs for all canonical schemas', () => {
    const html = renderToStaticMarkup(<AIStructuredOutputInspector />);

    expect(html).toContain('JobScoreResult');
    expect(html).toContain('RequirementClassificationResult');
    expect(html).toContain('CompanySummaryResult');
  });

  it('renders declared properties and required labels', () => {
    const html = renderToStaticMarkup(<AIStructuredOutputInspector />);

    expect(html).toContain('score');
    expect(html).toContain('category');
    expect(html).toContain('rationale');
    expect(html).toContain('REQUIRED');
  });

  it('renders sample loader buttons and validation action', () => {
    const html = renderToStaticMarkup(<AIStructuredOutputInspector />);

    expect(html).toContain('Load Valid');
    expect(html).toContain('Load Invalid');
    expect(html).toContain('Validate Untrusted Payload');
  });

  it('supports custom schema definitions', () => {
    const customSchemas = {
      custom_task: {
        title: 'CustomTaskResult',
        type: 'object',
        properties: {
          custom_id: { type: 'string', title: 'Custom ID' },
        },
        required: ['custom_id'],
        additionalProperties: false,
      },
    };
    const html = renderToStaticMarkup(<AIStructuredOutputInspector schemas={customSchemas} />);

    expect(html).toContain('CustomTaskResult');
    expect(html).toContain('custom_id');
  });
});
