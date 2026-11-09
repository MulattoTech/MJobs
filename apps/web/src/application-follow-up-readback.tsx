import { ApplicationFollowUpDrafts } from './application-follow-up-drafts';

type ExternalApplicationReport = {
  source?: string | null;
  applied_at?: string | null;
  sequence?: number | null;
  timestamp?: string | null;
};

type ApplicationSummary = {
  id: string;
  profile_id: string;
  job_id?: string | null;
  title?: string | null;
  company?: string | null;
  pipeline_status?: string | null;
  pipeline_attribution?: string | null;
  external_reports?: readonly ExternalApplicationReport[] | null;
};

type TrackingEvent = ExternalApplicationReport & {
  type: string;
  status?: string | null;
  attribution?: string | null;
};

type ApplicationTracking = {
  status?: string | null;
  attribution?: string | null;
  history?: readonly TrackingEvent[] | null;
};

export type ApplicationFollowUpReadbackProps = {
  profileId: string;
  applications: readonly ApplicationSummary[];
  selectedApplication?: ApplicationSummary | null;
  tracking?: ApplicationTracking | null;
};

function externalReportRevision(report: ExternalApplicationReport): string {
  return JSON.stringify([
    report.sequence ?? null, report.timestamp ?? null,
    report.applied_at ?? null, report.source ?? null,
  ]);
}

function readbackRevision({
  profileId, applications, selectedApplication, tracking,
}: ApplicationFollowUpReadbackProps): string {
  // Sort new projections only. Readback order and caller object identity do not
  // change application history or justify another request.
  const summaries = applications
    .filter(application => application.profile_id === profileId)
    .map(application => JSON.stringify([
      application.id, application.job_id ?? null,
      application.title ?? null, application.company ?? null,
      application.pipeline_status ?? null, application.pipeline_attribution ?? null,
      (application.external_reports ?? []).map(externalReportRevision).sort(),
    ]))
    .sort();

  const selectedHistory = selectedApplication?.profile_id === profileId && tracking
    ? [
      selectedApplication.id, tracking.status ?? null, tracking.attribution ?? null,
      (tracking.history ?? [])
        .filter(event => event.type === 'status' || event.type === 'external_application')
        .map(event => JSON.stringify(event.type === 'status'
          ? [event.type, event.sequence ?? null, event.timestamp ?? null,
            event.status ?? null, event.attribution ?? null]
          : [event.type, externalReportRevision(event)]))
        .sort(),
    ]
    : null;

  return JSON.stringify(['follow-up-readback-v1', profileId, summaries, selectedHistory]);
}

export function ApplicationFollowUpReadback(props: ApplicationFollowUpReadbackProps) {
  // A durable history change removes old draft/copy controls in the same commit,
  // before the replacement GET resolves. The existing child cancels stale reads
  // when unmounted. A changed durable readback or selected-application context
  // restores its default seven-day threshold; equivalent readbacks and note-only
  // edits preserve user selection.
  return <ApplicationFollowUpDrafts key={readbackRevision(props)} profileId={props.profileId}/>;
}
