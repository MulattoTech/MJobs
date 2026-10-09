import React, {useEffect, useState} from 'react';
import {InterviewPreparation, type InterviewPrepStoryOption} from './interview-preparation';

type Fetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;
type StoryListRow = {
  story_id?: unknown;
  profile_id?: unknown;
  revision?: unknown;
  title?: unknown;
  fact_ids?: unknown;
  allowed_uses?: unknown;
  reusable?: unknown;
  is_latest?: unknown;
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function selectInterviewPrepStories(profileId: string, payload: unknown): InterviewPrepStoryOption[] {
  if (!isRecord(payload) || payload.profile_id !== profileId) {
    throw new Error('Career story response belongs to a different profile.');
  }
  if (!Array.isArray(payload.stories)) {
    throw new Error('Career story response did not contain a story list.');
  }

  const seen = new Set<string>();
  const stories: InterviewPrepStoryOption[] = [];
  for (const value of payload.stories) {
    if (!isRecord(value)) continue;
    const row = value as StoryListRow;
    if (row.profile_id !== profileId || row.reusable !== true || row.is_latest !== true ||
        !Array.isArray(row.allowed_uses) || !row.allowed_uses.includes('INTERVIEW_PREPARATION') ||
        typeof row.story_id !== 'string' || !row.story_id.trim() || seen.has(row.story_id) ||
        typeof row.title !== 'string' || !row.title.trim() ||
        typeof row.revision !== 'number' || !Number.isSafeInteger(row.revision) || row.revision < 1 ||
        !Array.isArray(row.fact_ids) || row.fact_ids.some(id => typeof id !== 'string' || !id.trim())) continue;
    seen.add(row.story_id);
    stories.push({
      revision_id: row.story_id,
      title: row.title,
      revision: row.revision,
      source_refs: [
        `career_story_revision:${row.story_id}`,
        ...row.fact_ids.map(id => `career_fact:${id}`),
      ],
    });
  }
  return stories;
}

export function InterviewPreparationConnected({
  profileId,
  jobId,
  jobTitle,
  companyName,
  fetcher = fetch,
}: {
  profileId: string;
  jobId: string;
  jobTitle: string;
  companyName: string;
  fetcher?: Fetcher;
}) {
  const scope = `${profileId}\0${jobId}`;
  const [attempt, setAttempt] = useState(0);
  const [state, setState] = useState<{scope: string; loading: boolean; ready: boolean; error: string; stories: InterviewPrepStoryOption[]}>(
    {scope, loading: true, ready: false, error: '', stories: []},
  );
  const loading = state.scope !== scope || state.loading;
  const error = state.scope === scope ? state.error : '';
  const stories = state.scope === scope ? state.stories : [];
  const ready = state.scope === scope && state.ready;

  useEffect(() => {
    let active = true;
    setState(previous => ({
      scope,
      loading: true,
      ready: previous.scope === scope && previous.ready,
      error: '',
      stories: previous.scope === scope ? previous.stories : [],
    }));
    void fetcher(`/profiles/${encodeURIComponent(profileId)}/career-stories`)
      .then(async response => {
        const payload = await response.json().catch(() => null);
        if (!response.ok) throw new Error(`Career story request failed (${response.status}).`);
        return selectInterviewPrepStories(profileId, payload);
      })
      .then(rows => {
        if (active) setState({scope, loading: false, ready: true, error: '', stories: rows});
      })
      .catch(failure => {
        if (active) setState({
          scope,
          loading: false,
          ready: true,
          error: failure instanceof Error ? failure.message : 'Career stories could not be loaded.',
          stories: [],
        });
      });
    return () => { active = false; };
  }, [attempt, fetcher, jobId, profileId, scope]);

  if (!ready) return <p role="status">Loading approved interview stories…</p>;

  return <>
    <InterviewPreparation
      key={scope}
      profileId={profileId}
      jobId={jobId}
      jobTitle={jobTitle}
      companyName={companyName}
      availableStories={stories}
      fetcher={fetcher}
    />
    {loading && <p role="status">Refreshing approved interview stories…</p>}
    {error && <section role="alert" aria-label="Interview story loading error">
      <p>Approved interview stories could not be loaded: {error}</p>
      <button type="button" onClick={() => setAttempt(value => value + 1)}>Retry story loading</button>
      <p>You can still prepare a pack from the selected job and current profile facts.</p>
    </section>}
  </>;
}
