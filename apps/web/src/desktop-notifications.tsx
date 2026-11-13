import React, {useEffect, useMemo, useRef, useState} from 'react';
import type {NotificationOperation} from './operation-notifications';
import {
  boundedDesktopKeys, createDesktopNotificationProvider, desktopEvents, desktopStorageKey,
  dispatchDesktopEvents, readDesktopPreferences, setDesktopPreference,
} from './desktop-notification-provider';

type Props = {
  profileId: string; enabled: boolean; operations: NotificationOperation[]; loadError?: boolean;
};
type Fault = '' | 'storage' | 'disable-storage' | 'permission' | 'delivery';
type PreferenceState = {profileId: string; enabled: boolean};

// Retain a failed explicit opt-out across React/profile remounts within this tab.
// If pathological churn fills the bound, stop restoring cached opt-ins until reload.
const sessionOptOuts = new Set<string>();
let automaticRestoreSuspended = false;
function suppressInThisTab(profileId: string) {
  if (sessionOptOuts.size >= 512) automaticRestoreSuspended = true;
  else sessionOptOuts.add(profileId);
}
function suppressedInThisTab(profileId: string) {
  return automaticRestoreSuspended || sessionOptOuts.has(profileId);
}

export function DesktopNotifications({profileId, enabled, operations, loadError = false}: Props) {
  const provider = useMemo(() => createDesktopNotificationProvider(), [profileId]);
  const [preference, setPreference] = useState<PreferenceState>({profileId, enabled: false});
  const [permission, setPermission] = useState<NotificationPermission>(() => provider.permission());
  const [fault, setFault] = useState<Fault>('');
  const [pending, setPending] = useState(false);
  const mounted = useRef(false);
  const actionSequence = useRef(0);
  const deliveryGeneration = useRef(0);
  const baseline = useRef({profileId, initialized: false, keys: [] as string[]});
  const latest = useRef({profileId, enabled, operations, loadError});
  latest.current = {profileId, enabled, operations, loadError};
  const optedIn = preference.profileId === profileId && preference.enabled;
  const optedInRef = useRef(false);
  optedInRef.current = optedIn;

  useEffect(() => {
    mounted.current = true;
    baseline.current = {profileId, initialized: false, keys: []};
    setPending(false);
    const refresh = (event?: StorageEvent) => {
      try {
        if (event && event.key !== null && event.key !== desktopStorageKey(profileId)) return;
        const saved = readDesktopPreferences(profileId);
        if (event && !saved.enabled) {
          ++actionSequence.current;
          ++deliveryGeneration.current;
          setPending(false);
        }
        if (suppressedInThisTab(profileId)) {
          optedInRef.current = false;
          setPreference({profileId, enabled: false});
          setFault('disable-storage');
          setPermission(provider.permission());
          return;
        }
        if (optedInRef.current !== saved.enabled) baseline.current.initialized = false;
        optedInRef.current = saved.enabled;
        setPreference({profileId, enabled: saved.enabled});
        setFault(current => current === 'storage' || current === 'disable-storage' ? '' : current);
      } catch {
        optedInRef.current = false;
        setPreference({profileId, enabled: false});
        setFault('storage');
      }
      setPermission(provider.permission());
    };
    const onStorage = (event: StorageEvent) => refresh(event);
    const onFocus = () => refresh();
    refresh();
    window.addEventListener('storage', onStorage);
    window.addEventListener('focus', onFocus);
    return () => {
      mounted.current = false;
      ++actionSequence.current;
      ++deliveryGeneration.current;
      provider.close();
      window.removeEventListener('storage', onStorage);
      window.removeEventListener('focus', onFocus);
    };
  }, [profileId, provider]);

  useEffect(() => {
    const allowed = enabled && optedIn && !loadError
      && provider.availability() === 'available' && provider.permission() === 'granted';
    setPermission(provider.permission());
    if (!allowed) {
      if (!enabled) {++actionSequence.current; setPending(false);}
      ++deliveryGeneration.current;
      baseline.current = {profileId, initialized: false, keys: []};
      provider.close();
      return;
    }
    const events = desktopEvents(operations);
    if (baseline.current.profileId !== profileId || !baseline.current.initialized) {
      // The first known successful snapshot is history, including after recovery/re-enable.
      baseline.current = {profileId, initialized: true, keys: events.map(event => event.key)};
      return;
    }
    const seen = new Set(baseline.current.keys);
    const fresh = events.filter(event => !seen.has(event.key));
    baseline.current.keys = boundedDesktopKeys([...baseline.current.keys, ...events.map(event => event.key)]);
    if (!fresh.length) return;
    const generation = deliveryGeneration.current;
    const current = () => mounted.current && latest.current.profileId === profileId
      && latest.current.enabled && !latest.current.loadError && optedInRef.current
      && generation === deliveryGeneration.current;
    let nativeError = false;
    void dispatchDesktopEvents(profileId, fresh, provider, current, () => {
      nativeError = true;
      if (current()) setFault('delivery');
    }).then(result => {
      if (!current()) return;
      setPermission(provider.permission());
      if (result === 'failed' || result === 'not-permitted') setFault('delivery');
      else if (result === 'accepted' && !nativeError) setFault('');
    }).catch(() => {
      if (!current()) return;
      // Never send without a durable shared receipt when browser storage is unavailable.
      ++deliveryGeneration.current;
      optedInRef.current = false;
      setPreference({profileId, enabled: false});
      setFault('storage');
      provider.close();
    });
  }, [profileId, enabled, optedIn, operations, loadError, provider, permission]);

  const availability = provider.availability();
  async function enableDesktop() {
    const sequence = ++actionSequence.current;
    const current = () => mounted.current && latest.current.profileId === profileId
      && latest.current.enabled && actionSequence.current === sequence;
    setPending(true);
    setFault('');
    let granted: NotificationPermission;
    try {
      // The native prompt begins synchronously within this explicit button click.
      granted = await provider.requestPermission();
    } catch {
      if (current()) {setFault('permission'); setPending(false);}
      return;
    }
    if (!current()) return;
    setPermission(granted);
    if (granted !== 'granted') {setPending(false); return;}
    try {
      const keys = desktopEvents(latest.current.operations).map(event => event.key);
      const saved = await setDesktopPreference(profileId, true, keys, current);
      if (saved && current()) {
        sessionOptOuts.delete(profileId);
        baseline.current.initialized = false;
        optedInRef.current = true;
        setPreference({profileId, enabled: true});
      }
    } catch {
      if (current()) setFault('storage');
    } finally {
      if (current()) setPending(false);
    }
  }

  async function disableDesktop() {
    const sequence = ++actionSequence.current;
    const current = () => mounted.current && latest.current.profileId === profileId
      && actionSequence.current === sequence;
    ++deliveryGeneration.current;
    suppressInThisTab(profileId);
    optedInRef.current = false;
    setPreference({profileId, enabled: false});
    setPending(false);
    setFault('');
    baseline.current.initialized = false;
    provider.close();
    try {
      await setDesktopPreference(profileId, false, [], current);
      if (current()) {
        sessionOptOuts.delete(profileId);
        setPreference({profileId, enabled: false});
      }
    } catch {
      if (current()) setFault('disable-storage');
    }
  }

  if (!enabled) return null;
  let status = 'Desktop notifications are off for this profile.';
  if (availability === 'insecure') status = 'Desktop notifications need HTTPS or a local browser address.';
  else if (availability !== 'available') status = 'Desktop notifications are unavailable in this browser. In-app activity still works.';
  else if (fault === 'disable-storage') status = 'Desktop notifications are off in this tab, but the change could not be saved for other tabs or a page reload.';
  else if (fault === 'storage') status = 'Desktop notifications are paused because browser preferences could not be saved. Check browser storage and try enabling again.';
  else if (permission === 'denied') status = 'Desktop notifications are blocked in browser settings. In-app activity still works.';
  else if (fault === 'permission') status = 'The browser could not request desktop notification permission. Try again.';
  else if (fault === 'delivery') status = 'A desktop alert could not be displayed. In-app activity is still available; the next new outcome can try again.';
  else if (pending) status = 'Waiting for browser notification permission.';
  else if (optedIn && loadError) status = 'Desktop notifications are paused while activity updates are unavailable.';
  else if (optedIn && permission === 'granted') status = 'Desktop notifications are enabled for this profile while this page is open.';
  else if (optedIn) status = 'Browser permission is needed. Disable and enable desktop notifications to request it again.';

  return <section className="panel" aria-label="Desktop notifications">
    <h2>Desktop notifications</h2>
    <p role="status" aria-live="polite">{status}</p>
    <p>New activity outcomes only. Existing history and updates during an outage are not replayed.
      Browser and operating-system settings control whether an alert appears.</p>
    {optedIn
      ? <button type="button" className="quiet" onClick={() => void disableDesktop()}>Disable desktop notifications</button>
      : <button type="button" className="quiet"
          disabled={pending || availability !== 'available' || permission === 'denied'}
          onClick={() => void enableDesktop()}>
          {pending ? 'Enabling desktop notifications…' : 'Enable desktop notifications'}
        </button>}
  </section>;
}
