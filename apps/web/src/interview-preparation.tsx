import React, {useEffect, useState} from 'react';

export type InterviewPrepSource = string;

export type InterviewPrepQuestion = {
  id: string;
  prompt: string;
  source_refs: InterviewPrepSource[];
};

export type InterviewPrepTalkingPoint = {
  text: string;
  source_refs: InterviewPrepSource[];
};

export type InterviewPrepStarPrompt = {
  question: string;
  situation_prompt: string;
  task_prompt: string;
  action_prompt: string;
  result_prompt: string;
  source_refs: InterviewPrepSource[];
};

export type InterviewPrepPack = {
  schema_version: 1;
  pack_id: string;
  pack_version: number;
  profile_id: string;
  profile_version: string;
  job_id: string;
  job_fingerprint: string;
  created_at: string;
  questions: InterviewPrepQuestion[];
  talking_points: InterviewPrepTalkingPoint[];
  star_prompts: InterviewPrepStarPrompt[];
  missing_evidence: Array<{topic: string; reason: string}>;
  selected_story_revision_ids: string[];
  export_text: string;
};

/** Current, approved story revisions supplied by the existing story selector. */
export type InterviewPrepStoryOption = {
  revision_id: string;
  title: string;
  revision: number;
  source_refs: InterviewPrepSource[];
};

export type InterviewPreparationProps = {
  profileId: string;
  jobId: string;
  availableStories?: readonly InterviewPrepStoryOption[];
  /** Optional preloaded values for a containing view or deterministic rendering. */
  packs?: readonly InterviewPrepPack[];
  fetcher?: typeof fetch;
};

type PackCollection = {packs: InterviewPrepPack[]};
type JsonFetcher = (input: RequestInfo | URL, init?: RequestInit) => Promise<Response>;

function collectionPath(profileId: string, jobId: string) {
  return `/profiles/${encodeURIComponent(profileId)}/jobs/${encodeURIComponent(jobId)}/interview-prep-packs`;
}

async function requestJson<T>(url: string, init: RequestInit, fetcher: JsonFetcher): Promise<T> {
  const response = await fetcher(url, {
    ...init,
    headers: {'Content-Type': 'application/json', ...init.headers},
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(typeof data.detail === 'string' ? data.detail : `Interview prep request rejected (${response.status}).`);
  }
  return data as T;
}

export async function loadInterviewPrepPacks(
  profileId: string,
  jobId: string,
  fetcher: JsonFetcher = fetch,
): Promise<InterviewPrepPack[]> {
  const result = await requestJson<PackCollection>(collectionPath(profileId, jobId), {}, fetcher);
  return Array.isArray(result.packs) ? result.packs : [];
}

export async function loadInterviewPrepPack(
  profileId: string,
  jobId: string,
  packId: string,
  fetcher: JsonFetcher = fetch,
): Promise<InterviewPrepPack> {
  return requestJson<InterviewPrepPack>(`${collectionPath(profileId, jobId)}/${encodeURIComponent(packId)}`, {}, fetcher);
}

export async function createInterviewPrepPack(
  profileId: string,
  jobId: string,
  selectedStoryRevisionIds: readonly string[],
  fetcher: JsonFetcher = fetch,
): Promise<InterviewPrepPack> {
  return requestJson<InterviewPrepPack>(collectionPath(profileId, jobId), {
    method: 'POST',
    body: JSON.stringify({selected_story_revision_ids: [...new Set(selectedStoryRevisionIds)]}),
  }, fetcher);
}

export function interviewPrepExportFilename(pack: InterviewPrepPack): string {
  return `interview-preparation-v${pack.pack_version}.txt`;
}

export function downloadInterviewPrepExport(pack: InterviewPrepPack): void {
  const blob = new Blob([pack.export_text], {type: 'text/plain;charset=utf-8'});
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  link.href = url;
  link.download = interviewPrepExportFilename(pack);
  link.click();
  URL.revokeObjectURL(url);
}

function SourceRefs({refs}: {refs: readonly string[]}) {
  return <p><strong>Sources:</strong> {refs.length ? refs.join(' · ') : 'No source references supplied.'}</p>;
}

function shortDate(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : date.toLocaleString();
}

export function InterviewPreparation({profileId, jobId, availableStories = [], packs: initialPacks, fetcher = fetch}: InterviewPreparationProps) {
  const [packs, setPacks] = useState<InterviewPrepPack[]>([...(initialPacks || [])]);
  const [activePack, setActivePack] = useState<InterviewPrepPack | null>(initialPacks?.[0] || null);
  const [selectedStoryIds, setSelectedStoryIds] = useState<string[]>([]);
  const [loading, setLoading] = useState(!initialPacks);
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError('');
    void loadInterviewPrepPacks(profileId, jobId, fetcher).then(async history => {
      if (cancelled) return;
      setPacks(history);
      const latest = history[0];
      if (latest) {
        const exact = await loadInterviewPrepPack(profileId, jobId, latest.pack_id, fetcher);
        if (!cancelled) setActivePack(exact);
      } else if (!cancelled) {
        setActivePack(null);
      }
    }).catch(failure => {
      if (!cancelled) setError(failure instanceof Error ? failure.message : String(failure));
    }).finally(() => {
      if (!cancelled) setLoading(false);
    });
    return () => {cancelled = true;};
  }, [profileId, jobId, fetcher]);

  async function openVersion(packId: string) {
    setError('');
    try {
      setActivePack(await loadInterviewPrepPack(profileId, jobId, packId, fetcher));
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    }
  }

  async function createPack() {
    setCreating(true);
    setError('');
    try {
      const created = await createInterviewPrepPack(profileId, jobId, selectedStoryIds, fetcher);
      // Confirm the immutable row through its exact-readback route before showing it as saved.
      const saved = await loadInterviewPrepPack(profileId, jobId, created.pack_id, fetcher);
      setActivePack(saved);
      setPacks(await loadInterviewPrepPacks(profileId, jobId, fetcher));
      setSelectedStoryIds([]);
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : String(failure));
    } finally {
      setCreating(false);
    }
  }

  function toggleStory(revisionId: string, checked: boolean) {
    setSelectedStoryIds(current => checked
      ? [...new Set([...current, revisionId])]
      : current.filter(id => id !== revisionId));
  }

  return <section aria-label="Interview preparation">
    <h2>Interview preparation</h2>
    <p>Review a saved prep pack grounded in approved career evidence. Prompts help you prepare; they do not write answers for you.</p>

    <section aria-label="Create an interview prep pack">
      <h3>Create a version</h3>
      <p>No stories are selected automatically. Choose current approved story revisions to include; the pack will keep their source references.</p>
      {availableStories.length
        ? <fieldset disabled={creating || loading}>
          <legend>Approved story revisions</legend>
          {availableStories.map(story => <label key={story.revision_id}>
            <input type="checkbox" checked={selectedStoryIds.includes(story.revision_id)}
              onChange={event => toggleStory(story.revision_id, event.target.checked)} />
            {story.title} · revision {story.revision}
            <SourceRefs refs={story.source_refs} />
          </label>)}
        </fieldset>
        : <p>No approved interview-preparation story revisions are available. You can still create a pack from verified career facts.</p>}
      <button type="button" disabled={creating || loading} onClick={() => void createPack()}>
        {creating ? 'Saving prep pack…' : 'Create interview prep pack'}
      </button>
    </section>

    <section aria-label="Saved interview prep versions">
      <h3>Saved versions</h3>
      {loading && <p role="status">Loading saved prep packs…</p>}
      {!loading && packs.length === 0 && <p>No interview prep packs have been saved for this job.</p>}
      {packs.length > 0 && <ul>{packs.map(pack => <li key={pack.pack_id}>
        <button type="button" aria-pressed={activePack?.pack_id === pack.pack_id}
          onClick={() => void openVersion(pack.pack_id)}>
          Version {pack.pack_version} · {shortDate(pack.created_at)}
        </button>
      </li>)}</ul>}
    </section>

    {error && <p role="alert">Could not load or save the interview prep pack: {error}</p>}
    {activePack && <article aria-label={`Interview prep pack version ${activePack.pack_version}`}>
      <h3>Interview prep pack · Version {activePack.pack_version}</h3>
      <dl>
        <dt>Pack ID</dt><dd>{activePack.pack_id}</dd>
        <dt>Profile version</dt><dd>{activePack.profile_version}</dd>
        <dt>Job</dt><dd>{activePack.job_id} · fingerprint {activePack.job_fingerprint}</dd>
        <dt>Saved</dt><dd>{shortDate(activePack.created_at)}</dd>
      </dl>
      <section aria-label="Likely interview questions">
        <h4>Likely questions</h4>
        {activePack.questions.length
          ? <ol>{activePack.questions.map(question => <li key={question.id}>
            <p>{question.prompt}</p><SourceRefs refs={question.source_refs} />
          </li>)}</ol>
          : <p>No likely questions were produced from the current evidence.</p>}
      </section>
      <section aria-label="Fact-linked talking points">
        <h4>Fact-linked talking points</h4>
        {activePack.talking_points.length
          ? <ul>{activePack.talking_points.map((point, index) => <li key={`${index}-${point.text}`}>
            <p>{point.text}</p><SourceRefs refs={point.source_refs} />
          </li>)}</ul>
          : <p>No supported talking points are available.</p>}
      </section>
      <section aria-label="STAR prompts">
        <h4>STAR prompts</h4>
        {activePack.star_prompts.length
          ? <ol>{activePack.star_prompts.map((prompt, index) => <li key={`${index}-${prompt.question}`}>
            <p><strong>{prompt.question}</strong></p>
            <dl>
              <dt>Situation</dt><dd>{prompt.situation_prompt}</dd>
              <dt>Task</dt><dd>{prompt.task_prompt}</dd>
              <dt>Action</dt><dd>{prompt.action_prompt}</dd>
              <dt>Result</dt><dd>{prompt.result_prompt}</dd>
            </dl>
            <SourceRefs refs={prompt.source_refs} />
          </li>)}</ol>
          : <p>No story-based STAR prompts are available.</p>}
      </section>
      <section aria-label="Missing evidence">
        <h4>Missing evidence</h4>
        {activePack.missing_evidence.length
          ? <ul>{activePack.missing_evidence.map((item, index) => <li key={`${index}-${item.topic}`}>
            <strong>{item.topic}:</strong> {item.reason}
          </li>)}</ul>
          : <p>No missing evidence was reported for this pack.</p>}
      </section>
      <section aria-label="Deterministic export preview">
        <h4>Deterministic export preview</h4>
        <p>The export is the saved structured pack text. It contains no generated answers.</p>
        <pre>{activePack.export_text}</pre>
        <button type="button" onClick={() => downloadInterviewPrepExport(activePack)}>Download deterministic export</button>
      </section>
    </article>}
  </section>;
}
