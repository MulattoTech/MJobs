import { describe, expect, it, vi } from 'vitest';
import {
  createResumeWorkspaceClient,
  ResumeWorkspaceClientError,
  type ResumeWorkspacePreviewInput,
  type ResumeWorkspaceSaveInput,
} from './resume-workspace-client';

const section = { section_id: 'experience', visible: true, position: 0, fact_ids: ['fact-7'] };
const previewInput: ResumeWorkspacePreviewInput = {
  profileId: 'profile-1', jobId: 'job-1', templateId: 'verified-headings-v1',
  profileVersion: 4, jobFingerprint: 'job-sha-1', templateVersion: '1', sections: [section],
};
const preview = {
  profile_id: 'profile-1', profile_version: 4, job_id: 'job-1', job_fingerprint: 'job-sha-1',
  template_id: 'verified-headings-v1', template_version: '1', preview_fingerprint: 'preview-sha-1',
  rendered_text: 'Systems administrator\nMaintained approved systems.',
  source_refs: { facts: [{ fact_id: 'fact-7', source: 'USER_APPROVED', fact_fingerprint: 'fact-sha-7' }] },
  keyword_alignment: {
    matches: { skills: [{ term: 'Linux', fact_ids: ['fact-7'] }], responsibilities: [] },
    unmatched: { skills: [{ term: 'Kubernetes' }], responsibilities: [] },
    analysis_only: true,
  },
};
const revision = {
  id: 'workspace-row-1', profile_id: 'profile-1', job_id: 'job-1', version: 1,
  profile_version: 4, job_fingerprint: 'job-sha-1', template_id: 'verified-headings-v1',
  sections: [section], preview_fingerprint: 'preview-sha-1',
  rendered_text: 'Systems administrator\nMaintained approved systems.',
  source_refs: preview.source_refs, created_at: '2026-10-09T10:00:00Z',
};

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });
}

describe('resume workspace client', () => {
  it('loads only the requested profile/job history and returns the current immutable version', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({
      profile_id: 'profile-1', job_id: 'job-1', current: revision, versions: [revision],
    }));
    const client = createResumeWorkspaceClient({ baseUrl: 'http://127.0.0.1:8000/', fetchImpl });

    const result = await client.load('profile-1', 'job-1');

    expect(fetchImpl).toHaveBeenCalledTimes(1);
    expect(fetchImpl.mock.calls[0][0]).toBe('http://127.0.0.1:8000/profiles/profile-1/jobs/job-1/resume-workspace');
    expect((fetchImpl.mock.calls[0][1] as RequestInit).method).toBe('GET');
    expect(result.current?.version).toBe(1);
    expect(result.versions.map(row => row.id)).toEqual(['workspace-row-1']);
  });

  it('posts an exact draft layout and rejects a preview from another snapshot', async () => {
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse(preview));
    const client = createResumeWorkspaceClient({ fetchImpl });

    const result = await client.preview(previewInput);

    expect(result.rendered_text).toBe('Systems administrator\nMaintained approved systems.');
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/profiles/profile-1/jobs/job-1/resume-workspace/preview');
    expect(init.method).toBe('POST');
    expect(JSON.parse(String(init.body))).toEqual({
      template_id: 'verified-headings-v1', sections: [section], expected_profile_version: 4,
      expected_job_fingerprint: 'job-sha-1', expected_template_version: '1',
    });

    const mismatched = { ...preview, job_id: 'other-job' };
    const otherClient = createResumeWorkspaceClient({ fetchImpl: vi.fn().mockResolvedValue(jsonResponse(mismatched)) });
    await expect(otherClient.preview(previewInput)).rejects.toThrow(/scope/i);
  });

  it('puts only a reviewed, idempotent snapshot and accepts the exact durable readback', async () => {
    const input: ResumeWorkspaceSaveInput = {
      profileId: 'profile-1', jobId: 'job-1', expectedVersion: null, profileVersion: 4,
      jobFingerprint: 'job-sha-1', templateId: 'verified-headings-v1', sections: [section],
      previewFingerprint: 'preview-sha-1', reviewed: true, requestId: 'save-request-1',
    };
    const fetchImpl = vi.fn().mockResolvedValue(jsonResponse({ workspace_revision: revision, replayed: false }));
    const client = createResumeWorkspaceClient({ fetchImpl });

    const result = await client.save(input);

    expect(result.workspace_revision.version).toBe(1);
    expect(result.replayed).toBe(false);
    const [url, init] = fetchImpl.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/profiles/profile-1/jobs/job-1/resume-workspace');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(String(init.body))).toEqual({
      expected_version: null, profile_version: 4, job_fingerprint: 'job-sha-1',
      template_id: 'verified-headings-v1', sections: [section], preview_fingerprint: 'preview-sha-1',
      reviewed: true, request_id: 'save-request-1',
    });
  });

  it('does not accept a successful response whose durable revision differs from the reviewed draft', async () => {
    const wrongRevision = { ...revision, template_id: 'approved-skill-order-headings-v1' };
    const client = createResumeWorkspaceClient({
      fetchImpl: vi.fn().mockResolvedValue(jsonResponse({ workspace_revision: wrongRevision, replayed: false })),
    });
    await expect(client.save({
      profileId: 'profile-1', jobId: 'job-1', expectedVersion: null, profileVersion: 4,
      jobFingerprint: 'job-sha-1', templateId: 'verified-headings-v1', sections: [section],
      previewFingerprint: 'preview-sha-1', reviewed: true, requestId: 'save-request-1',
    })).rejects.toThrow(/readback/i);
  });

  it('returns safe stable errors without echoing server details', async () => {
    const client = createResumeWorkspaceClient({
      fetchImpl: vi.fn().mockResolvedValue(jsonResponse({ detail: 'token=private' }, 409)),
    });
    try {
      await client.load('profile-1', 'job-1');
      throw new Error('expected stale load to fail');
    } catch (error) {
      expect(error).toBeInstanceOf(ResumeWorkspaceClientError);
      expect((error as Error).message).toMatch(/409|stale/i);
      expect((error as Error).message).not.toContain('private');
    }
  });
});
