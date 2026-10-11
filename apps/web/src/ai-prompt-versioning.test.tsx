import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it } from 'vitest';
import {
  AIPromptVersioningView,
  CANONICAL_TARGET_LABELS,
  DEFAULT_PROMPT_VERSIONS,
} from './ai-prompt-versioning';

describe('AIPromptVersioningView Component', () => {
  it('renders section heading and all 4 target tabs', () => {
    const html = renderToStaticMarkup(<AIPromptVersioningView />);

    expect(html).toContain('AI Prompt &amp; Template Versioning');
    expect(html).toContain('Reproducibility Gate Active');

    expect(html).toContain(CANONICAL_TARGET_LABELS.resume.title);
    expect(html).toContain(CANONICAL_TARGET_LABELS.cover_letter.title);
    expect(html).toContain(CANONICAL_TARGET_LABELS.screening_answers.title);
    expect(html).toContain(CANONICAL_TARGET_LABELS.job_parsing.title);
  });

  it('renders active version details for default target (resume)', () => {
    const html = renderToStaticMarkup(<AIPromptVersioningView />);

    expect(html).toContain('Version 1.0.0');
    expect(html).toContain('Active in Production');
    expect(html).toContain('Initial canonical resume tailoring prompt template v1.0.0');
  });

  it('renders template variables and fingerprint hash', () => {
    const html = renderToStaticMarkup(<AIPromptVersioningView />);

    expect(html).toContain('{job_title}');
    expect(html).toContain('{company_name}');
    expect(html).toContain('{career_facts}');
    expect(html).toContain('SHA-256 Fingerprint:');
  });

  it('renders history table with active and archived status', () => {
    const customTargets = {
      ...DEFAULT_PROMPT_VERSIONS,
      resume: [
        {
          version: '1.0.0',
          template: 'Template v1',
          description: 'v1 description',
          variables: ['job_title'],
          sha256: 'a1b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef',
          isActive: false,
        },
        {
          version: '2.0.0',
          template: 'Template v2',
          description: 'v2 description',
          variables: ['job_title', 'skills'],
          sha256: 'b2c3d4e5f60718293a4b5c6d7e8f901234567890abcdef1234567890abcdef01',
          isActive: true,
        },
      ],
    };

    const html = renderToStaticMarkup(<AIPromptVersioningView targets={customTargets} />);

    expect(html).toContain('Template Version History (2)');
    expect(html).toContain('1.0.0');
    expect(html).toContain('2.0.0');
    expect(html).toContain('ACTIVE');
    expect(html).toContain('Archived');
    expect(html).toContain('v2 description');
  });
});
