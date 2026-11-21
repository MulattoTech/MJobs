import type {NotificationOperation} from './operation-notifications';

export const DESKTOP_STORAGE_PREFIX = 'mjobs-desktop-notifications:v1:';
export const MAX_DESKTOP_EVENTS = 512;
const MAX_EVENT_KEY_LENGTH = 512;
const MAX_RECORD_LENGTH = 280000;
const TITLE = 'MJobs activity';
const outcomes: Record<string, string> = {
  COMPLETED: 'completed', FAILED: 'needs attention', DEAD_LETTERED: 'needs review',
  CANCELLED: 'was cancelled', BLOCKED: 'needs review',
};
const names: Record<string, string> = {
  DISCOVER: 'Job discovery', SOURCE_COLLECT: 'Source collection',
  COMPANY_DISCOVER: 'Company discovery', PREPARE: 'Document preparation',
  APPLY_MOCK: 'Fixture application', AI_EVALUATE: 'AI evaluation', AI_EVALUATE_QUEUED: 'AI evaluation',
  RESUME_EXTRACT_QUEUED: 'Resume extraction', UPLOADED_RESUME_COMPARE_QUEUED: 'Resume comparison',
  COMPANY_SOURCE_REFRESH: 'Company source refresh', APPLICATION_DRY_RUN: 'Application dry-run',
  APPLICATION_FORM_REVIEW: 'Application form review',
};

export type DesktopEvent = {key: string; body: string};
export type DesktopAvailability = 'available' | 'insecure' | 'unsupported' | 'coordination-unavailable';
export type DesktopPreferences = {version: 1; enabled: boolean; seen: string[]};
export type DesktopDelivery = 'accepted' | 'not-permitted' | 'failed';

/** Browser providers own permission and delivery; server-side providers are separate. */
export interface NotificationDeliveryProvider {
  readonly id: 'desktop';
  availability(): DesktopAvailability;
  permission(): NotificationPermission;
  requestPermission(): Promise<NotificationPermission>;
  deliver(profileId: string, events: DesktopEvent[], onError: () => void): DesktopDelivery;
  close(): void;
}

export function desktopStorageKey(profileId: string): string {
  if (!profileId || profileId.length > 128) throw new Error('DESKTOP_PROFILE_INVALID');
  try { return DESKTOP_STORAGE_PREFIX + encodeURIComponent(profileId); }
  catch { throw new Error('DESKTOP_PROFILE_INVALID'); }
}

export function boundedDesktopKeys(keys: unknown): string[] {
  if (!Array.isArray(keys)) return [];
  const valid = keys.filter((key): key is string =>
    typeof key === 'string' && key.length > 0 && key.length <= MAX_EVENT_KEY_LENGTH);
  return Array.from(new Set(valid)).slice(-MAX_DESKTOP_EVENTS);
}

/** Ignore corrupt preferences. Access failures remain errors so delivery fails closed. */
export function readDesktopPreferences(profileId: string): DesktopPreferences {
  const empty: DesktopPreferences = {version: 1, enabled: false, seen: []};
  const raw = window.localStorage.getItem(desktopStorageKey(profileId));
  if (!raw || raw.length > MAX_RECORD_LENGTH) return empty;
  try {
    const value: unknown = JSON.parse(raw);
    if (!value || typeof value !== 'object' || Array.isArray(value)) return empty;
    const record = value as Record<string, unknown>;
    if (record.version !== 1 || typeof record.enabled !== 'boolean' || !Array.isArray(record.seen)) return empty;
    return {version: 1, enabled: record.enabled, seen: boundedDesktopKeys(record.seen)};
  } catch { return empty; }
}

function saveDesktopPreferences(profileId: string, preference: DesktopPreferences): void {
  window.localStorage.setItem(desktopStorageKey(profileId), JSON.stringify({
    version: 1, enabled: preference.enabled, seen: boundedDesktopKeys(preference.seen),
  }));
}

async function withDesktopLock<T>(profileId: string, action: () => T | Promise<T>): Promise<T> {
  // This is real same-origin inter-tab exclusion, not a localStorage check-then-write election.
  if (typeof navigator === 'undefined' || !navigator.locks?.request) {
    throw new Error('DESKTOP_COORDINATION_UNAVAILABLE');
  }
  return navigator.locks.request(desktopStorageKey(profileId), {mode: 'exclusive'}, action);
}

export async function setDesktopPreference(
  profileId: string, enabled: boolean, baseline: string[], isCurrent: () => boolean,
): Promise<boolean> {
  return withDesktopLock(profileId, () => {
    if (!isCurrent()) return false;
    const current = readDesktopPreferences(profileId);
    saveDesktopPreferences(profileId, {
      version: 1, enabled,
      seen: boundedDesktopKeys([...current.seen, ...baseline]),
    });
    return true;
  });
}

/** Only fixed labels reach the OS. IDs and timestamps stay in bounded local metadata. */
export function desktopEvents(operations: NotificationOperation[]): DesktopEvent[] {
  if (!Array.isArray(operations)) return [];
  const events = new Map<string, DesktopEvent>();
  for (const operation of operations.slice(0, MAX_DESKTOP_EVENTS)) {
    if (!operation || typeof operation !== 'object'
      || typeof operation.id !== 'string' || !operation.id || operation.id.length > 128
      || typeof operation.state !== 'string' || !Object.hasOwn(outcomes, operation.state)
      || typeof operation.updated_at !== 'string' || operation.updated_at.length > 64
      || !Number.isFinite(Date.parse(operation.updated_at))) continue;
    const key = JSON.stringify([operation.id, operation.state, operation.updated_at]);
    const label = typeof operation.kind === 'string' && Object.hasOwn(names, operation.kind)
      ? names[operation.kind] : 'Background task';
    events.set(key, {key, body: label + ' ' + outcomes[operation.state] + '.'});
  }
  return Array.from(events.values());
}

/** Opaque replacement tag: never put a profile, operation ID or arbitrary text in OS metadata. */
function opaqueTag(profileId: string, events: DesktopEvent[]): string {
  const input = profileId + '\0' + events.map(event => event.key).sort().join('\0');
  let left = 2166136261;
  let right = 2246822507;
  for (let index = 0; index < input.length; index++) {
    left = Math.imul(left ^ input.charCodeAt(index), 16777619);
    right = Math.imul(right ^ input.charCodeAt(index), 3266489909);
  }
  return 'mjobs-activity-' + (left >>> 0).toString(16) + (right >>> 0).toString(16);
}

export function createDesktopNotificationProvider(): NotificationDeliveryProvider {
  const opened = new Set<Notification>();
  const availability = (): DesktopAvailability => {
    if (typeof window === 'undefined' || window.isSecureContext !== true) return 'insecure';
    if (typeof window.Notification !== 'function'
      || typeof window.Notification.requestPermission !== 'function') return 'unsupported';
    if (typeof navigator === 'undefined' || typeof navigator.locks?.request !== 'function') {
      return 'coordination-unavailable';
    }
    return 'available';
  };
  const permission = (): NotificationPermission => {
    if (availability() !== 'available') return 'default';
    const value = window.Notification.permission;
    return value === 'granted' || value === 'denied' ? value : 'default';
  };
  return {
    id: 'desktop',
    availability,
    permission,
    async requestPermission() {
      if (availability() !== 'available') return 'default';
      if (permission() !== 'default') return permission();
      const result = await window.Notification.requestPermission();
      return result === 'granted' || result === 'denied' ? result : 'default';
    },
    deliver(profileId, events, onError) {
      if (availability() !== 'available' || permission() !== 'granted' || !events.length) {
        return 'not-permitted';
      }
      try {
        const notification = new window.Notification(TITLE, {
          body: events.length === 1 ? events[0].body
            : 'Background tasks have new outcomes. Open MJobs Activity to review.',
          tag: opaqueTag(profileId, events), silent: true,
        });
        while (opened.size >= 5) {
          const oldest = opened.values().next().value;
          if (!oldest) break;
          opened.delete(oldest);
          try { oldest.close(); } catch { /* Bound native handles even if closing fails. */ }
        }
        opened.add(notification);
        notification.addEventListener('error', () => {
          opened.delete(notification);
          onError();
        }, {once: true});
        notification.addEventListener('close', () => opened.delete(notification), {once: true});
        notification.addEventListener('click', () => {
          window.focus();
          notification.close();
          opened.delete(notification);
        }, {once: true});
        // Returning accepted means native constructor handoff, never confirmed OS display.
        return 'accepted';
      } catch { return 'failed'; }
    },
    close() {
      for (const notification of opened) {
        try { notification.close(); } catch { /* Closing is best-effort after native failure. */ }
      }
      opened.clear();
    },
  };
}

export async function dispatchDesktopEvents(
  profileId: string, events: DesktopEvent[], provider: NotificationDeliveryProvider,
  isCurrent: () => boolean, onError: () => void,
): Promise<DesktopDelivery | 'suppressed'> {
  return withDesktopLock(profileId, () => {
    if (!isCurrent() || provider.permission() !== 'granted') return 'suppressed';
    const preference = readDesktopPreferences(profileId);
    if (!preference.enabled) return 'suppressed';
    const remembered = new Set(preference.seen);
    const fresh = events.filter(event => !remembered.has(event.key)).slice(0, MAX_DESKTOP_EVENTS);
    if (!fresh.length) return 'suppressed';
    // Reserve all outcomes before the native call. Failed attempts are not retried in a storm.
    saveDesktopPreferences(profileId, {
      version: 1, enabled: true, seen: boundedDesktopKeys([...preference.seen, ...fresh.map(event => event.key)]),
    });
    if (!isCurrent()) return 'suppressed';
    return provider.deliver(profileId, fresh, onError);
  });
}
