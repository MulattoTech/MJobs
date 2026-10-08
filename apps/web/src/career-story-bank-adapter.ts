export type CareerStoryInput = {
  title: string;
  situation: string;
  task: string;
  action: string;
  result: string;
  factIds: string[];
  tags?: string[];
  allowedUses: string[];
};

export type StoryTextFields = Pick<CareerStoryInput, 'situation' | 'task' | 'action' | 'result'>;

export type DecodedStoryText =
  | ({ kind: 'STAR_STORY' } & StoryTextFields)
  | { kind: 'UNSTRUCTURED'; text: string };

export type CareerStoryApiPayload = {
  kind: 'STAR_STORY';
  title: string;
  text: string;
  fact_ids: string[];
  tags: string[];
  allowed_uses: string[];
};

export type CareerStoryApiRow = {
  story_id: string;
  story_key: string;
  revision: number;
  title?: unknown;
  kind?: unknown;
  text?: unknown;
  profile_version?: unknown;
  fact_ids?: unknown;
  fact_fingerprints?: unknown;
  tags?: unknown;
  allowed_uses?: unknown;
  review_status?: unknown;
  review_note?: unknown;
  created_at?: unknown;
  reviewed_at?: unknown;
  is_latest?: unknown;
  reusable?: unknown;
  stale_fact_ids?: unknown;
};

export type CareerStoryRevisionView = StoryTextFields & {
  revisionId: string;
  revision: number;
  createdAt: string;
  title: string;
  kind: string;
  factIds: string[];
  /** Stable references to the CareerFacts used by the API revision. */
  provenance: string[];
  factFingerprints: Array<{ fact_id: string; fingerprint: string }>;
  tags: string[];
  allowedUses: string[];
  reviewStatus: string;
  reviewNote: string | null;
  reviewedAt: string | null;
  profileVersion: string | null;
  isLatest: boolean;
  reusable: boolean;
  staleFactIds: string[];
  /** Legacy or malformed API text stays visible without being treated as STAR. */
  unstructuredText?: string;
};

export type CareerStoryView = {
  storyId: string;
  revisions: CareerStoryRevisionView[];
};

const STORY_TEXT_PREFIX = 'MJobs-Career-Story/1\n';
const STORY_TEXT_TYPE = 'mjobs.career-story';
const MAX_API_TEXT_LENGTH = 8000;
const ALLOWED_USES = new Set([
  'RESUME',
  'COVER_LETTER',
  'APPLICATION_ANSWER',
  'INTERVIEW_PREPARATION',
]);

function isNonEmptyText(value: unknown): value is string {
  return typeof value === 'string' && value.trim().length > 0;
}

function requireTextFields(value: StoryTextFields): StoryTextFields {
  for (const field of ['situation', 'task', 'action', 'result'] as const) {
    if (!isNonEmptyText(value[field])) {
      throw new TypeError(`STAR ${field} must contain text.`);
    }
  }
  return {
    situation: value.situation,
    task: value.task,
    action: value.action,
    result: value.result,
  };
}

export function encodeStoryText(input: StoryTextFields): string {
  const fields = requireTextFields(input);
  const envelope = {
    type: STORY_TEXT_TYPE,
    version: 1,
    situation: fields.situation,
    task: fields.task,
    action: fields.action,
    result: fields.result,
  };
  const encoded = `${STORY_TEXT_PREFIX}${JSON.stringify(envelope)}`;
  if (encoded.length > MAX_API_TEXT_LENGTH) {
    throw new TypeError('STAR story text exceeds the API limit.');
  }
  return encoded;
}

function isVersionOneEnvelope(value: unknown): value is StoryTextFields & {
  type: typeof STORY_TEXT_TYPE;
  version: 1;
} {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  const expected = ['action', 'result', 'situation', 'task', 'type', 'version'];
  if (Object.keys(row).sort().join('\0') !== expected.join('\0')) return false;
  return row.type === STORY_TEXT_TYPE && row.version === 1
    && isNonEmptyText(row.situation) && isNonEmptyText(row.task)
    && isNonEmptyText(row.action) && isNonEmptyText(row.result);
}

export function decodeStoryText(text: string): DecodedStoryText {
  if (typeof text !== 'string' || !text.startsWith(STORY_TEXT_PREFIX)) {
    return { kind: 'UNSTRUCTURED', text: typeof text === 'string' ? text : '' };
  }
  try {
    const envelope = JSON.parse(text.slice(STORY_TEXT_PREFIX.length)) as unknown;
    if (isVersionOneEnvelope(envelope)) {
      return {
        kind: 'STAR_STORY',
        situation: envelope.situation,
        task: envelope.task,
        action: envelope.action,
        result: envelope.result,
      };
    }
  } catch {
    // Keep invalid, old, or future envelopes visible as authored content.
  }
  return { kind: 'UNSTRUCTURED', text };
}

function uniqueStrings(values: unknown, field: string, max: number): string[] {
  if (!Array.isArray(values) || values.length > max
    || values.some(value => typeof value !== 'string' || value.trim() === '')) {
    throw new TypeError(`${field} must be a list of text values.`);
  }
  const result = values.map(value => value.trim());
  if (new Set(result).size !== result.length) {
    throw new TypeError(`${field} must not contain duplicates.`);
  }
  return result;
}

export function toCreatePayload(input: CareerStoryInput): CareerStoryApiPayload {
  if (!isNonEmptyText(input.title)) throw new TypeError('Story title must contain text.');
  const allowedUses = uniqueStrings(input.allowedUses, 'At least one allowed use', 4);
  if (allowedUses.length === 0 || allowedUses.some(use => !ALLOWED_USES.has(use))) {
    throw new TypeError('Choose at least one supported allowed use.');
  }
  const factIds = uniqueStrings(input.factIds, 'Career fact IDs', 50);
  if (factIds.length === 0) throw new TypeError('Link at least one approved career fact.');
  const tags = uniqueStrings(input.tags ?? [], 'Story tags', 20);
  const payload: CareerStoryApiPayload = {
    kind: 'STAR_STORY',
    title: input.title.trim(),
    text: encodeStoryText(input),
    fact_ids: factIds,
    tags,
    allowed_uses: allowedUses,
  };
  return payload;
}

function stringArray(value: unknown): string[] {
  return Array.isArray(value)
    ? value.filter((item): item is string => typeof item === 'string')
    : [];
}

function fingerprints(value: unknown): Array<{ fact_id: string; fingerprint: string }> {
  if (!Array.isArray(value)) return [];
  return value.filter((entry): entry is { fact_id: string; fingerprint: string } => (
    Boolean(entry) && typeof entry === 'object'
    && typeof (entry as Record<string, unknown>).fact_id === 'string'
    && typeof (entry as Record<string, unknown>).fingerprint === 'string'
  )).map(entry => ({ fact_id: entry.fact_id, fingerprint: entry.fingerprint }));
}

function optionalText(value: unknown): string | null {
  return typeof value === 'string' && value.length > 0 ? value : null;
}

type PendingRevision = {
  row: CareerStoryApiRow;
  decoded: DecodedStoryText;
  factIds: string[];
  fingerprints: Array<{ fact_id: string; fingerprint: string }>;
  tags: string[];
  allowedUses: string[];
  staleFactIds: string[];
};

export function groupStoryRows(rows: readonly CareerStoryApiRow[]): CareerStoryView[] {
  if (!Array.isArray(rows)) throw new TypeError('Career story rows must be a list.');
  const groups = new Map<string, PendingRevision[]>();
  const seenRevisionIds = new Set<string>();

  for (const row of rows) {
    if (!row || typeof row !== 'object'
      || typeof row.story_id !== 'string' || !row.story_id
      || typeof row.story_key !== 'string' || !row.story_key
      || !Number.isSafeInteger(row.revision) || row.revision < 1) {
      throw new TypeError('Career story API row is missing revision identity.');
    }
    if (seenRevisionIds.has(row.story_id)) {
      throw new TypeError('Career story API returned a duplicate revision ID.');
    }
    seenRevisionIds.add(row.story_id);

    const factIds = stringArray(row.fact_ids);
    const pending: PendingRevision = {
      row,
      decoded: decodeStoryText(typeof row.text === 'string' ? row.text : ''),
      factIds,
      fingerprints: fingerprints(row.fact_fingerprints),
      tags: stringArray(row.tags),
      allowedUses: stringArray(row.allowed_uses),
      staleFactIds: stringArray(row.stale_fact_ids),
    };
    const revisions = groups.get(row.story_key) ?? [];
    revisions.push(pending);
    groups.set(row.story_key, revisions);
  }

  return [...groups.entries()]
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([storyId, revisions]) => {
      revisions.sort((left, right) => left.row.revision - right.row.revision);
      const latestRevision = revisions[revisions.length - 1]?.row.revision;
      return {
        storyId,
        revisions: revisions.map(({ row, decoded, factIds, fingerprints: factFingerprints, tags, allowedUses, staleFactIds }) => {
          const reviewStatus = typeof row.review_status === 'string'
            ? row.review_status
            : 'PENDING';
          const reviewedAt = optionalText(row.reviewed_at);
          const isLatest = typeof row.is_latest === 'boolean'
            ? row.is_latest
            : row.revision === latestRevision;
          const reusable = row.reusable === true && isLatest
            && reviewStatus === 'APPROVED' && reviewedAt !== null
            && staleFactIds.length === 0 && allowedUses.length > 0;
          const storyFields = decoded.kind === 'STAR_STORY'
            ? decoded
            : { situation: '', task: '', action: '', result: '', unstructuredText: decoded.text };
          return {
            revisionId: row.story_id,
            revision: row.revision,
            createdAt: optionalText(row.created_at) ?? '',
            title: optionalText(row.title) ?? '',
            kind: optionalText(row.kind) ?? 'STAR_STORY',
            factIds,
            provenance: [...factIds],
            factFingerprints,
            tags,
            allowedUses,
            reviewStatus,
            reviewNote: optionalText(row.review_note),
            reviewedAt,
            profileVersion: optionalText(row.profile_version),
            isLatest,
            reusable,
            staleFactIds,
            ...storyFields,
          };
        }),
      };
    });
}
