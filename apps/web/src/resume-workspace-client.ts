export type ResumeWorkspaceSection = { section_id: string; visible: boolean; position: number; fact_ids: string[] };
export type ResumeWorkspaceRevision = {
  id: string; profile_id: string; job_id: string; version: number; profile_version: string | number;
  job_fingerprint: string; template_id: string; sections: ResumeWorkspaceSection[];
  preview_fingerprint?: string; rendered_text?: string; source_refs?: unknown; created_at?: string;
};
export type ResumeWorkspacePreviewInput = {
  profileId: string; jobId: string; templateId: string; profileVersion: string | number;
  jobFingerprint: string; templateVersion: string; sections: ResumeWorkspaceSection[];
};
export type ResumeWorkspaceSaveInput = Omit<ResumeWorkspacePreviewInput, 'templateVersion'> & {
  expectedVersion: number | null; previewFingerprint: string; reviewed: boolean; requestId: string;
};
export class ResumeWorkspaceClientError extends Error {
  constructor(message: string, readonly status: number) { super(message); this.name = 'ResumeWorkspaceClientError'; }
}
export type ResumeWorkspaceClient = ReturnType<typeof createResumeWorkspaceClient>;

export function createResumeWorkspaceClient(options: { baseUrl?: string; fetchImpl?: typeof fetch } = {}) {
  const base = (options.baseUrl || '').replace(/\/$/, '');
  const fetcher = options.fetchImpl || fetch;
  async function request(path: string, init?: RequestInit) {
    const response = await fetcher(base + path, { ...init, headers: { ...(init?.body ? { 'content-type': 'application/json' } : {}), ...init?.headers } });
    if (!response.ok) {
      const message = response.status === 404 ? 'Workspace not found (404).' : response.status === 409 ? 'Workspace snapshot is stale (409).' : `Resume workspace request failed (${response.status}).`;
      throw new ResumeWorkspaceClientError(message, response.status);
    }
    return response;
  }
  return {
    async load(profileId: string, jobId: string) {
      const body = await (await request(`/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/resume-workspace`, { method: 'GET' })).json();
      if (body.profile_id !== profileId || body.job_id !== jobId || !Array.isArray(body.versions) ||
          (body.current && (body.current.profile_id !== profileId || body.current.job_id !== jobId))) throw new Error('Resume workspace scope readback mismatch.');
      return body as { profile_id: string; job_id: string; current: ResumeWorkspaceRevision | null; versions: ResumeWorkspaceRevision[] };
    },
    async preview(input: ResumeWorkspacePreviewInput) {
      const path = `/profiles/${encodeURIComponent(input.profileId)}/jobs/${encodeURIComponent(input.jobId)}/resume-workspace/preview`;
      const result = await (await request(path, { method: 'POST', body: JSON.stringify({
        template_id: input.templateId, sections: input.sections, expected_profile_version: input.profileVersion,
        expected_job_fingerprint: input.jobFingerprint, expected_template_version: input.templateVersion,
      }) })).json();
      if (result.profile_id !== input.profileId || result.job_id !== input.jobId || result.profile_version !== input.profileVersion || result.job_fingerprint !== input.jobFingerprint) throw new Error('Resume workspace preview scope mismatch.');
      return { ...result, rendered_text: result.rendered_text ?? result.preview_text };
    },
    async save(input: ResumeWorkspaceSaveInput) {
      if (!input.reviewed) throw new Error('Review the current resume workspace preview before saving.');
      const path = `/profiles/${encodeURIComponent(input.profileId)}/jobs/${encodeURIComponent(input.jobId)}/resume-workspace`;
      const result = await (await request(path, { method: 'PUT', body: JSON.stringify({
        expected_version: input.expectedVersion, profile_version: input.profileVersion, job_fingerprint: input.jobFingerprint,
        template_id: input.templateId, sections: input.sections, preview_fingerprint: input.previewFingerprint,
        reviewed: input.reviewed, request_id: input.requestId,
      }) })).json();
      const row = result.workspace_revision;
      if (!row || row.profile_id !== input.profileId || row.job_id !== input.jobId || row.profile_version !== input.profileVersion || row.job_fingerprint !== input.jobFingerprint || row.template_id !== input.templateId || JSON.stringify(row.sections) !== JSON.stringify(input.sections) || row.preview_fingerprint !== input.previewFingerprint) throw new Error('Resume workspace durable readback mismatch.');
      return result as { workspace_revision: ResumeWorkspaceRevision; replayed: boolean };
    },
  };
}
