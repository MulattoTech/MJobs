import React, { useEffect, useMemo } from 'react';
import { createResumeWorkspaceClient } from './resume-workspace-client';
import { createResumeWorkspaceController, ResumeWorkspace, type ResumeWorkspaceContext, type ResumeWorkspaceSectionSource } from './resume-workspace';

type Fact = { id: string; fact_id?: string; text: string; kind: string; verified?: boolean; user_approved?: boolean };
export function ResumeWorkspaceConnected(props: ResumeWorkspaceContext & { facts: Fact[] }) {
  const sections = useMemo(() => {
    const approved = props.facts.filter(f => f.verified && f.user_approved && f.text.trim());
    const groups = [
      { id: 'summary', label: 'Summary', kinds: ['summary'] },
      { id: 'experience', label: 'Experience', kinds: ['experience', 'responsibility', 'accomplishment', 'technology_experience'] },
      { id: 'skill', label: 'Skills', kinds: ['skill'] },
      { id: 'education', label: 'Education', kinds: ['education'] },
      { id: 'certification', label: 'Certifications', kinds: ['certification'] },
      { id: 'project', label: 'Projects', kinds: ['project'] },
    ];
    return groups.map(group => {
      const items = approved.filter(f => group.kinds.includes(f.kind));
      return { sectionId: group.id, label: group.label, factIds: items.map(f => f.fact_id || f.id), items: items.map(f => ({ factId: f.fact_id || f.id, text: f.text })) };
    }).filter(group => group.items.length) as ResumeWorkspaceSectionSource[];
  }, [props.facts]);
  const controller = useMemo(() => createResumeWorkspaceController({ ...props, sections, client: createResumeWorkspaceClient() }), [props.profileId, props.jobId, props.profileVersion, props.jobFingerprint, sections]);
  useEffect(() => { void controller.load(); }, [controller]);
  if (!sections.length) return <section className="panel" aria-label="Resume workspace"><h2>Resume workspace</h2><p>Approve at least one profile fact before building a factual resume preview.</p></section>;
  return <ResumeWorkspace controller={controller} />;
}
