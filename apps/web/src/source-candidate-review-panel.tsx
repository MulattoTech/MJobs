import React, { useEffect, useSyncExternalStore } from 'react';
import type {
  SourceCandidate,
  SourceCandidateReviewInput,
} from './source-candidate-review';
import { SourceCandidateReviewQueue } from './source-candidate-review';
import type { SourceCandidateReviewClient } from './source-candidate-review-client';

export type SourceCandidateReviewPanelState = {
  profileId: string | null;
  candidates: SourceCandidate[];
  loading: boolean;
  error: string | null;
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
  };
  private generation = 0;
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
    this.publish({ profileId, candidates: currentRows, loading: true, error: null });
    try {
      const candidates = await this.client.list(profileId);
      if (generation !== this.generation || this.state.profileId !== profileId) return;
      if (candidates.some(candidate => candidate.profile_id !== profileId)) {
        throw new Error('Source candidate list did not match the selected profile.');
      }
      this.publish({ profileId, candidates, loading: false, error: null });
    } catch (error) {
      if (generation !== this.generation || this.state.profileId !== profileId) return;
      this.publish({
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
      this.publish({ profileId: null, candidates: [], loading: false, error: null });
      return;
    }
    this.publish({ profileId: selectedProfile, candidates: [], loading: true, error: null });
    await this.load(selectedProfile, generation, false);
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
};

export function SourceCandidateReviewPanel({ profileId, client }: SourceCandidateReviewPanelProps) {
  const [controller] = React.useState(() => new SourceCandidateReviewPanelController(client));
  const state = useSyncExternalStore(controller.subscribe, controller.getSnapshot, controller.getSnapshot);

  useEffect(() => {
    void controller.selectProfile(profileId);
  }, [controller, profileId]);

  const stateMatchesSelection = state.profileId === profileId;
  return <SourceCandidateReviewQueue
    profileId={profileId}
    candidates={stateMatchesSelection ? state.candidates : []}
    api={controller}
    loading={!stateMatchesSelection || state.loading}
    error={stateMatchesSelection ? state.error : null}
    onRetry={controller.retry}
  />;
}
