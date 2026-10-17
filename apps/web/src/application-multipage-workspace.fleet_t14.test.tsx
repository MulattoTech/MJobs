import { describe, expect, it, vi } from 'vitest';
import {
  createApplicationMultipageClient,
  type ApplicationMultipageState,
} from './application-multipage-workspace';
import type { ApplicationMultipagePage } from './application-multipage-review';

const pages: ApplicationMultipagePage[] = [
  { id: 'contact', title: 'Contact information', fields: [{ id: 'email', label: 'Email', required: true }] },
  { id: 'experience', title: 'Experience', fields: [{ id: 'summary', label: 'Experience summary' }] },
];

const saved: ApplicationMultipageState = {
  profile_id: 'profile-fixture', application_id: 'application-fixture', current_page_id: 'contact',
  page_order: ['contact', 'experience'], status: 'IN_PROGRESS',
  progress: { contact: { email: 'old@example.test' } }, version: 2,
  updated_at: '2026-10-16T12:00:00Z', manual_intervention_required: true,
};

describe('fleet T14 multipage persisted recovery', () => {
  it('recovers server-accepted user progress when the transition response is interrupted', async () => {
    let persisted = saved;
    const fetcher = vi.fn<typeof fetch>(async (_input, init) => {
      if (init?.method === 'POST') {
        const body = JSON.parse(String(init.body)) as { progress: ApplicationMultipageState['progress'] };
        persisted = {
          ...persisted,
          progress: body.progress,
          version: persisted.version + 1,
          updated_at: '2026-10-16T12:01:00Z',
        };
        // The server committed, but the client lost the response during interruption.
        throw new TypeError('connection interrupted after write');
      }
      return Response.json(persisted);
    });
    const client = createApplicationMultipageClient(
      saved.profile_id, saved.application_id, pages, fetcher,
    );
    const initial = await client.load(saved.profile_id, saved.application_id);

    await expect(client.transition(saved.profile_id, saved.application_id, {
      request_id: 't14-interrupted-save', action: 'SAVE_PROGRESS', expected_version: initial.version,
      page_id: 'contact', progress: { ...initial.progress, contact: { email: 'new@example.test' } },
    })).rejects.toMatchObject({ code: 'NETWORK_ERROR' });

    const recovered = await client.load(saved.profile_id, saved.application_id);
    expect(recovered.progress.contact).toEqual({ email: 'new@example.test' });
    expect(recovered.version).toBe(3);
    expect(fetcher.mock.calls.map(([, init]) => init?.method)).toEqual(['GET', 'POST', 'GET']);
  });

  it('rejects recovered state for a different selected application without exposing it', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(Response.json({
      ...saved, application_id: 'another-application',
      progress: { contact: { email: 'other@example.test' } },
    }));
    const client = createApplicationMultipageClient(
      saved.profile_id, saved.application_id, pages, fetcher,
    );

    await expect(client.load(saved.profile_id, saved.application_id))
      .rejects.toMatchObject({ code: 'SCOPE_MISMATCH' });
    expect(fetcher).toHaveBeenCalledTimes(1);
  });
});
