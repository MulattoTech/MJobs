import React, { useEffect, useRef, useState } from 'react';
import { CareerStorySelection, type CareerStorySelectionCandidate } from './career-story-selection';

type StoryRecord = {
  story_id: string;
  revision: number;
  profile_version: string;
  title: string;
  text: string;
  review_status: string;
  reviewed_at: string | null;
  is_latest: boolean;
  reusable: boolean;
  allowed_uses: string[];
  fact_ids: string[];
  fact_fingerprints: {fact_id: string; fingerprint: string}[];
  stale_fact_ids: string[];
};

type Props = {
  profileId: string;
  profileVersion: string;
  jobId: string;
  jobTitle: string;
  approvedFactIds: string[];
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
  onSelectionChange: (storyIds: string[]) => void;
};

export function ResumeStorySelection({
  profileId, profileVersion, jobId, jobTitle, approvedFactIds, api, onSelectionChange,
}: Props) {
  const [candidates, setCandidates] = useState<CareerStorySelectionCandidate[]>([]);
  const [status, setStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const [requestKey, setRequestKey] = useState('');
  const contextKey = JSON.stringify([profileId, profileVersion, jobId]);
  const selectionChanged = useRef(onSelectionChange);
  selectionChanged.current = onSelectionChange;

  useEffect(() => {
    let active = true;
    setCandidates([]);
    setStatus('loading');
    setRequestKey(contextKey);
    selectionChanged.current([]);
    void api<{stories: StoryRecord[]}>(`/profiles/${encodeURIComponent(profileId)}/career-stories`)
      .then(result => {
        if (!active) return;
        setCandidates(result.stories.map(story => ({
          storyId: story.story_id,
          revisionId: story.story_id,
          revision: story.revision,
          title: story.title,
          text: story.text,
          reviewStatus: story.review_status,
          reviewedAt: story.reviewed_at,
          isLatest: story.is_latest,
          reusable: story.reusable && story.profile_version === profileVersion,
          allowedUses: story.allowed_uses,
          factIds: story.fact_ids,
          factFingerprints: story.fact_fingerprints.map(item => ({
            factId: item.fact_id, fingerprint: item.fingerprint,
          })),
          staleFactIds: story.stale_fact_ids,
        })));
        setStatus('ready');
      })
      .catch(() => { if (active) setStatus('error'); });
    return () => { active = false; };
  }, [profileId, profileVersion, jobId, api, contextKey]);

  if (requestKey !== contextKey || status === 'loading') {
    return <section className="panel" aria-label={`Loading resume career stories for ${jobTitle}`}>
      <h2>Job-specific resume stories</h2><p role="status">Loading current reviewed stories…</p>
    </section>;
  }
  if (status === 'error') {
    return <section className="panel" aria-label={`Resume career story selection unavailable for ${jobTitle}`}>
      <h2>Job-specific resume stories</h2><p role="alert">Career stories could not be loaded. Reload before selecting a story.</p>
    </section>;
  }
  return <>
    <CareerStorySelection
      profileId={profileId}
      jobId={jobId}
      jobTitle={jobTitle}
      ariaLabel={`Resume career story selection for ${jobTitle}`}
      allowedUse="RESUME"
      candidates={candidates}
      approvedFactIds={approvedFactIds}
      onSelectionChange={selection => {
        const ids = selection.map(item => item.revisionId);
        onSelectionChange(ids);
      }}
    />
    <p className="muted">Selected story wording appears exactly as you wrote it in the resume, with its fact provenance retained. Review the complete resume before use; this does not change your approved profile facts or send anything to an employer.</p>
  </>;
}
