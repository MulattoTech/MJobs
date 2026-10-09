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
  source_refs: { facts: [{ fact_id: 'fact-7', source: 'USER_APPROVED', fact_fingerprint: 'fact-sha-7' }] },
  keyword_alignment: {
    matches: { skills: [{ term: 'Linux', fact_ids: ['fact-8'] }], responsibilities: [] },
    unmatched: { skills: [{ term: 'Terraform' }], responsibilities: [] }, analysis_only: true,
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
    expect(html).toContain('Linux');
    expect(html).toContain('SUPPORTED');
    expect(html).toContain('fact-8');
    expect(html).toContain('Terraform');
    expect(html).toContain('UNKNOWN');
    expect(html).toContain('analysis only');
    expect(html).toContain('aria-label="I reviewed this resume workspace preview" checked=""');
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
