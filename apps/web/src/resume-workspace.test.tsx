import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { describe, expect, it, vi } from 'vitest';
import {
  createResumeWorkspaceController,
  ResumeWorkspace,
  type ResumeWorkspaceClient,
  type ResumeWorkspaceContext,
  type ResumeWorkspaceSectionSource,
} from './resume-workspace';

const context: ResumeWorkspaceContext = {
  profileId: 'profile-1', jobId: 'job-1', profileVersion: 4, jobFingerprint: 'job-sha-1',
};
const sections: ResumeWorkspaceSectionSource[] = [
  { sectionId: 'experience', label: 'Experience', factIds: ['fact-7'], items: [{ factId: 'fact-7', text: 'Maintained approved systems.' }] },
  { sectionId: 'skills', label: 'Skills', factIds: ['fact-8'], items: [{ factId: 'fact-8', text: 'Linux administration.' }] },
];
const preview = {
  profile_id: 'profile-1', profile_version: 4, job_id: 'job-1', job_fingerprint: 'job-sha-1',
  template_id: 'verified-headings-v1', template_version: '1', preview_fingerprint: 'preview-sha-1',
  rendered_text: 'Experience\nMaintained approved systems.',
  source_refs: { facts: [
    { fact_id: 'fact-7', source: 'Synthetic approved experience', fact_fingerprint: 'fact-sha-7' },
    { fact_id: 'fact-8', source: 'Synthetic approved skill', fact_fingerprint: 'fact-sha-8' },
  ] },
  keyword_alignment: {
    matches: {
      skills: [{ term: 'Linux', fact_ids: ['fact-8'] }, { term: 'Terraform', fact_ids: [] }],
      responsibilities: [{ term: 'Maintain systems', fact_ids: ['fact-7'] }, { term: 'Manage infrastructure', fact_ids: [] }],
    },
    unmatched: { skills: ['Terraform'], responsibilities: ['Manage infrastructure'] },
    selected_fact_ids: ['fact-7', 'fact-8'], analysis_only: true,
  },
};
const saved = {
  workspace_revision: {
    id: 'workspace-row-1', profile_id: 'profile-1', job_id: 'job-1', version: 1,
    profile_version: 4, job_fingerprint: 'job-sha-1', template_id: 'verified-headings-v1',
    sections: [
      { section_id: 'experience', visible: true, position: 0, fact_ids: ['fact-7'] },
      { section_id: 'skills', visible: true, position: 1, fact_ids: ['fact-8'] },
    ], preview_fingerprint: 'preview-sha-1', rendered_text: preview.rendered_text,
    source_refs: preview.source_refs, created_at: '2026-10-09T10:00:00Z',
  }, replayed: false,
};

const client: ResumeWorkspaceClient = {
  load: vi.fn().mockResolvedValue({ profile_id: 'profile-1', job_id: 'job-1', current: null, versions: [] }),
  preview: vi.fn().mockResolvedValue(preview),
  save: vi.fn().mockResolvedValue(saved),
};

async function renderPreview(overrides: Record<string, unknown> = {}) {
  const previewClient: ResumeWorkspaceClient = { ...client, preview: vi.fn().mockResolvedValue({ ...preview, ...overrides }), save: vi.fn().mockResolvedValue(saved) };
  const controller = createResumeWorkspaceController({ ...context, sections, client: previewClient });
  await controller.preview();
  return { html: renderToStaticMarkup(<ResumeWorkspace controller={controller} />), controller, previewClient };
}

describe('resume workspace', () => {
  it('requires a fresh explicit preview review before save and shows exact keyword support provenance', async () => {
    const controller = createResumeWorkspaceController({ ...context, sections, client });
    await controller.load();
    expect(controller.getSnapshot().reviewed).toBe(false);
    expect(controller.getSnapshot().canSave).toBe(false);

    await controller.preview();
    expect(controller.getSnapshot().canSave).toBe(false);
    controller.setReviewed(true);
    expect(controller.getSnapshot().canSave).toBe(true);

    const html = renderToStaticMarkup(<ResumeWorkspace controller={controller} />);
    expect(html).toContain('Resume workspace');
    expect(html).toContain('Keyword coverage analysis');
    expect(html).toContain('Linux: SUPPORTED</strong> · approved fact fact-8');
    expect(html).toContain('Maintain systems: SUPPORTED</strong> · approved fact fact-7');
    expect(html.match(/Terraform: UNKNOWN/g)).toHaveLength(1);
    expect(html.match(/Manage infrastructure: UNKNOWN/g)).toHaveLength(1);
    expect(html).not.toContain('Terraform: SUPPORTED');
    expect(html).not.toContain('Manage infrastructure: SUPPORTED');
    expect(html).not.toContain('undefined');
    expect(html).toContain('analysis only');
    expect(html).toContain('aria-label="I reviewed this resume workspace preview" checked=""');
  });

  it('names every unsupported term when the preview has no supporting facts', async () => {
    const { html, controller } = await renderPreview({
      source_refs: { facts: [] },
      keyword_alignment: {
        matches: { skills: [{ term: 'Terraform', fact_ids: [] }], responsibilities: [{ term: 'Manage infrastructure', fact_ids: [] }] },
        unmatched: { skills: ['Terraform'], responsibilities: ['Manage infrastructure'] },
        selected_fact_ids: [], analysis_only: true,
      },
    });
    expect(html).toContain('Terraform: UNKNOWN');
    expect(html).toContain('Manage infrastructure: UNKNOWN');
    expect(html).not.toContain(': SUPPORTED');
    expect(html).not.toContain('<p>: UNKNOWN</p>');
    expect(controller.getSnapshot().canSave).toBe(false);
    expect(controller.getSnapshot().preview.rendered_text).toBe(preview.rendered_text);
  });

  it('keeps all-supported analysis informational and requires explicit review', async () => {
    const { html, controller, previewClient } = await renderPreview({
      keyword_alignment: {
        matches: { skills: [{ term: 'Linux', fact_ids: ['fact-8'] }], responsibilities: [{ term: 'Maintain systems', fact_ids: ['fact-7'] }] },
        unmatched: { skills: [], responsibilities: [] },
        selected_fact_ids: ['fact-7', 'fact-8'], analysis_only: true,
      },
    });
    expect(html).toContain('Linux: SUPPORTED</strong> · approved fact fact-8');
    expect(html).toContain('Maintain systems: SUPPORTED</strong> · approved fact fact-7');
    expect(html).not.toContain(': UNKNOWN');
    expect(html).toContain('no keywords are inserted automatically');
    expect(html).toContain(`<pre>${preview.rendered_text}</pre>`);
    expect(controller.getSnapshot().reviewed).toBe(false);
    await controller.save();
    expect(previewClient.save).not.toHaveBeenCalled();
  });

  it.each([
    ['empty references', []],
    ['missing references', undefined],
    ['null references', null],
    ['scalar references', 'fact-8'],
    ['blank reference', [' ']],
    ['non-string reference', [8]],
    ['mixed invalid references', ['fact-8', null]],
    ['foreign reference', ['fact-other']],
    ['mixed foreign references', ['fact-8', 'fact-other']],
  ])('does not claim support from %s', async (_description, factIds) => {
    const { html } = await renderPreview({
      keyword_alignment: {
        ...preview.keyword_alignment,
        matches: { skills: [{ term: 'Linux', fact_ids: factIds }], responsibilities: [] },
        unmatched: { skills: [], responsibilities: [] },
      },
    });
    expect(html).toContain('Linux: UNKNOWN');
    expect(html).not.toContain(': SUPPORTED');
    expect(html).not.toContain(' · approved fact ');
  });

  it.each([
    ['absent from selection', { keyword_alignment: { ...preview.keyword_alignment, selected_fact_ids: ['fact-7'] } }],
    ['absent from provenance', { source_refs: { facts: [preview.source_refs.facts[0]] } }],
    ['missing provenance', { source_refs: null }],
  ])('does not claim support when a fact is %s', async (_description, overrides) => {
    const { html } = await renderPreview(overrides);
    expect(html).toContain('Linux: UNKNOWN');
    expect(html).not.toContain('Linux: SUPPORTED');
  });

  it('lets explicit unknown evidence override a contradictory supported match', async () => {
    const { html } = await renderPreview({
      keyword_alignment: {
        ...preview.keyword_alignment,
        matches: { skills: [{ term: 'Linux', fact_ids: ['fact-8'] }], responsibilities: [] },
        unmatched: { skills: ['Linux'], responsibilities: [] },
      },
    });
    expect(html.match(/Linux: UNKNOWN/g)).toHaveLength(1);
    expect(html).not.toContain('Linux: SUPPORTED');
  });

  it.each([
    ['missing alignment', undefined],
    ['null alignment', null],
    ['missing groups', {}],
    ['invalid groups', { ...preview.keyword_alignment, matches: { skills: null, responsibilities: {} }, unmatched: null }],
    ['invalid rows', { ...preview.keyword_alignment, matches: { skills: [null, { term: 8, fact_ids: ['fact-8'] }], responsibilities: [] }, unmatched: { skills: [null, { term: 'Terraform' }], responsibilities: [] } }],
    ['missing selection', { ...preview.keyword_alignment, selected_fact_ids: undefined }],
    ['invalid selection', { ...preview.keyword_alignment, selected_fact_ids: ['fact-8', null] }],
    ['non-analysis payload', { ...preview.keyword_alignment, analysis_only: false }],
  ])('renders an honest unavailable state for %s', async (_description, keywordAlignment) => {
    const { html } = await renderPreview({ keyword_alignment: keywordAlignment });
    expect(html).toContain('Some keyword coverage could not be verified.');
    expect(html).not.toContain(': SUPPORTED');
    expect(html).not.toContain('undefined');
    expect(html).not.toContain('[object Object]');
    expect(html).not.toContain('<p>: UNKNOWN</p>');
  });

  it('clears preview acknowledgement when layout changes and confirms only durable readback', async () => {
    const readbackClient: ResumeWorkspaceClient = { ...client,
      load: vi.fn().mockResolvedValueOnce({ profile_id: 'profile-1', job_id: 'job-1', current: null, versions: [] })
        .mockResolvedValue({ profile_id: 'profile-1', job_id: 'job-1', current: saved.workspace_revision, versions: [saved.workspace_revision] }),
    };
    const controller = createResumeWorkspaceController({ ...context, sections, client: readbackClient });
    await controller.load();
    await controller.preview();
    controller.setReviewed(true);
    controller.toggleSection('skills', false);
    expect(controller.getSnapshot().preview).toBeNull();
    expect(controller.getSnapshot().reviewed).toBe(false);
    expect(controller.getSnapshot().canSave).toBe(false);

    await controller.preview();
    controller.setReviewed(true);
    await controller.save();
    expect(controller.getSnapshot().savedVersion).toBe(1);
    expect(controller.getSnapshot().error).toBeNull();
    expect(readbackClient.save).toHaveBeenCalledWith(expect.objectContaining({ reviewed: true, requestId: expect.any(String) }));
  });

  it('does not let a late profile response restore preview or approval in the new scope', async () => {
    let resolveOld!: (value: Awaited<ReturnType<ResumeWorkspaceClient['preview']>>) => void;
    const delayedClient: ResumeWorkspaceClient = {
      ...client,
      preview: vi.fn(() => new Promise(resolve => { resolveOld = resolve; })),
    };
    const controller = createResumeWorkspaceController({ ...context, sections, client: delayedClient });
    await controller.load();
    const pending = controller.preview();
    controller.setContext({ ...context, profileId: 'profile-2' });
    resolveOld({ ...preview, profile_id: 'profile-1' });
    await pending;
    expect(controller.getSnapshot().context.profileId).toBe('profile-2');
    expect(controller.getSnapshot().preview).toBeNull();
    expect(controller.getSnapshot().reviewed).toBe(false);
  });

  it('renders stale errors as a blocker and offers a reload action', async () => {
    const staleClient: ResumeWorkspaceClient = {
      ...client,
      load: vi.fn().mockRejectedValue(new Error('The profile/job snapshot is stale.')),
    };
    const controller = createResumeWorkspaceController({ ...context, sections, client: staleClient });
    await controller.load();
    const html = renderToStaticMarkup(<ResumeWorkspace controller={controller} />);
    expect(html).toContain('role="alert"');
    expect(html).toContain('Reload workspace');
    expect(controller.getSnapshot().canSave).toBe(false);
  });
});
