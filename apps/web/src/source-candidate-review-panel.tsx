import React, { useEffect, useSyncExternalStore } from 'react';
import type {
  SourceCandidate,
  SourceCandidateState,
  SourceCandidateReviewInput,
} from './source-candidate-review';
import { SourceCandidateReviewQueue } from './source-candidate-review';
import type { SourceCandidateReviewClient } from './source-candidate-review-client';

export type SourceCandidateReviewStateFilter = 'ALL' | SourceCandidateState;

export type SourceCandidateReviewPanelState = {
  profileId: string | null;
  candidates: SourceCandidate[];
  loading: boolean;
  error: string | null;
  stateFilter: SourceCandidateReviewStateFilter;
  sourceQuery: string;
};

function errorMessage(error: unknown): string {
  return error instanceof Error
    ? error.message
    : 'Source candidates could not be loaded. Retry the request.';
}

/** Profile-scoped list/review controller used by the panel and unit tests. */
export class SourceCandidateReviewPanelController {
  private state: SourceCandidateReviewPanelState = {
    profileId: null, candidates: [], loading: false, error: null,
    stateFilter: 'ALL', sourceQuery: '',
  };
  private generation = 0;
  private syncedProfile: string | null | undefined;
  private syncedRefreshToken: string | number | null | undefined;
  private readonly listeners = new Set<() => void>();

  constructor(private readonly client: SourceCandidateReviewClient) {}

  getSnapshot = (): SourceCandidateReviewPanelState => this.state;

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  };

  private publish(next: SourceCandidateReviewPanelState): void {
    this.state = next;
    this.listeners.forEach(listener => listener());
  }

  private async load(profileId: string, generation: number, retainRows: boolean): Promise<void> {
    const currentRows = retainRows && this.state.profileId === profileId ? this.state.candidates : [];
    this.publish({ ...this.state, profileId, candidates: currentRows, loading: true, error: null });
    try {
      const candidates = await this.client.list(profileId);
      if (generation !== this.generation || this.state.profileId !== profileId) return;
      if (candidates.some(candidate => candidate.profile_id !== profileId)) {
        throw new Error('Source candidate list did not match the selected profile.');
      }
      this.publish({ ...this.state, profileId, candidates, loading: false, error: null });
    } catch (error) {
      if (generation !== this.generation || this.state.profileId !== profileId) return;
      this.publish({
        ...this.state,
        profileId,
        candidates: currentRows,
        loading: false,
        error: errorMessage(error),
      });
    }
  }

  async selectProfile(profileId: string | null): Promise<void> {
    const selectedProfile = profileId?.trim() || null;
    const generation = ++this.generation;
    if (!selectedProfile) {
      this.publish({
        profileId: null, candidates: [], loading: false, error: null,
        stateFilter: 'ALL', sourceQuery: '',
      });
      return;
    }
    this.publish({
      profileId: selectedProfile, candidates: [], loading: true, error: null,
      stateFilter: 'ALL', sourceQuery: '',
    });
    await this.load(selectedProfile, generation, false);
  }

  async syncSelection(
    profileId: string | null,
    refreshToken?: string | number | null,
  ): Promise<'PROFILE' | 'REFRESH' | 'NONE'> {
    if (this.syncedProfile === undefined || this.syncedProfile !== profileId) {
      this.syncedProfile = profileId;
      this.syncedRefreshToken = refreshToken;
      await this.selectProfile(profileId);
      return 'PROFILE';
    }
    if (!Object.is(this.syncedRefreshToken, refreshToken)) {
      this.syncedRefreshToken = refreshToken;
      if (profileId) {
        await this.refreshProfile(profileId);
        return 'REFRESH';
      }
    }
    return 'NONE';
  }

  async refreshProfile(profileId: string): Promise<void> {
    if (!profileId || this.state.profileId !== profileId) return;
    const generation = ++this.generation;
    await this.load(profileId, generation, true);
  }

  setFilters(stateFilter: SourceCandidateReviewStateFilter, sourceQuery: string): void {
    if (stateFilter === this.state.stateFilter && sourceQuery === this.state.sourceQuery) return;
    this.publish({ ...this.state, stateFilter, sourceQuery });
  }

  retry = async (): Promise<void> => {
    const profileId = this.state.profileId;
    if (!profileId) return;
    const generation = ++this.generation;
    await this.load(profileId, generation, true);
  };

  review = async (
    profileId: string,
    candidateId: string,
    input: SourceCandidateReviewInput,
  ): Promise<SourceCandidate> => {
    if (this.state.profileId !== profileId) {
      throw new Error('Select this candidate’s profile before reviewing it.');
    }
    const generation = this.generation;
    const updated = await this.client.review(profileId, candidateId, input);
    if (updated.profile_id !== profileId || updated.id !== candidateId) {
      throw new Error('Source candidate review response did not match the selected profile and candidate.');
    }
    if (generation === this.generation && this.state.profileId === profileId) {
      // A successful write is followed by a profile-scoped readback. Readback
      // errors remain retryable list errors and do not replay the successful PUT.
      const readbackGeneration = ++this.generation;
      await this.load(profileId, readbackGeneration, true);
    }
    return updated;
  };
}

export type SourceCandidateReviewPanelProps = {
  profileId: string | null;
  client: SourceCandidateReviewClient;
  refreshToken?: string | number | null;
};

const statePriority: Record<SourceCandidateState, number> = {
  NEEDS_REVIEW: 0,
  DISCOVERED: 1,
  BLOCKED: 2,
  INTEGRATION_QUEUED: 3,
  INTEGRATED: 4,
  REJECTED: 5,
};

function compareStableText(left: string, right: string): number {
  const a = left.toLowerCase();
  const b = right.toLowerCase();
  return a < b ? -1 : a > b ? 1 : 0;
}

export function filterAndSortSourceCandidates(
  candidates: SourceCandidate[],
  profileId: string | null,
  stateFilter: SourceCandidateReviewStateFilter,
  sourceQuery = '',
): SourceCandidate[] {
  if (!profileId) return [];
  const query = sourceQuery.trim().toLowerCase();
  return candidates
    .filter(candidate => candidate.profile_id === profileId)
    .filter(candidate => stateFilter === 'ALL' || candidate.state === stateFilter)
    .filter(candidate => !query || candidate.domain.toLowerCase().includes(query) ||
      candidate.display_name.toLowerCase().includes(query))
    .slice()
    .sort((left, right) => statePriority[left.state] - statePriority[right.state] ||
      compareStableText(left.domain, right.domain) || compareStableText(left.id, right.id));
}

export function SourceCandidateReviewPanel({ profileId, client, refreshToken }: SourceCandidateReviewPanelProps) {
  const [controller] = React.useState(() => new SourceCandidateReviewPanelController(client));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);

  useEffect(() => {
    void controller.syncSelection(profileId, refreshToken);
  }, [controller, profileId, refreshToken]);

  const stateMatchesSelection = state.profileId === profileId;
  const profileCandidates = stateMatchesSelection ? state.candidates : [];
  const filteredCandidates = filterAndSortSourceCandidates(
    profileCandidates, profileId, state.stateFilter, state.sourceQuery,
  );
  const controlsDisabled = !stateMatchesSelection || state.loading;
  const filtersActive = state.stateFilter !== 'ALL' || state.sourceQuery.trim().length > 0;
  return <div className="source-candidate-review-panel">
    {profileId && <section className="panel" aria-label="Source candidate queue filters">
      <div className="toolbar"><div><h2>Filter source candidates</h2>
        <p>Changing filters hides unmatched rows; entered review notes and retry IDs remain with their candidates.</p></div>
        <span className="tag">Showing {filteredCandidates.length} of {profileCandidates.length}</span></div>
      <div className="toolbar">
        <label>Review state<select aria-label="Filter by review state" value={state.stateFilter} disabled={controlsDisabled}
          onChange={event => controller.setFilters(event.currentTarget.value as SourceCandidateReviewStateFilter, state.sourceQuery)}>
          <option value="ALL">All review states</option>
          <option value="NEEDS_REVIEW">Needs review</option>
          <option value="DISCOVERED">Discovered</option>
          <option value="BLOCKED">Blocked</option>
          <option value="INTEGRATION_QUEUED">Integration queued</option>
          <option value="INTEGRATED">Integrated</option>
          <option value="REJECTED">Rejected</option>
        </select></label>
        <label>Source or domain<input aria-label="Filter by source name or domain" type="search" value={state.sourceQuery}
          disabled={controlsDisabled} onChange={event => controller.setFilters(state.stateFilter, event.currentTarget.value)} /></label>
        <button type="button" disabled={controlsDisabled || !filtersActive}
          onClick={() => controller.setFilters('ALL', '')}>Clear filters</button>
      </div>
      <p className="muted">Sorted by review priority, then source domain; candidate ID breaks ties.</p>
      {profileCandidates.length > 0 && filteredCandidates.length === 0 &&
        <p role="status">No candidates match these filters. Change or clear filters to see this profile’s source candidates.</p>}
    </section>}
    <SourceCandidateReviewQueue
      profileId={profileId}
      candidates={filteredCandidates}
      api={controller}
      loading={!stateMatchesSelection || state.loading}
      error={stateMatchesSelection ? state.error : null}
      onRetry={controller.retry}
    />
  </div>;
}
