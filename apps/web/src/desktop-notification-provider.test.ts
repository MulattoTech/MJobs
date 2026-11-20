// @vitest-environment jsdom
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import type {NotificationOperation} from './operation-notifications';
import {
  MAX_DESKTOP_EVENTS, boundedDesktopKeys, createDesktopNotificationProvider, desktopEvents,
  desktopStorageKey, dispatchDesktopEvents, readDesktopPreferences, setDesktopPreference,
} from './desktop-notification-provider';

class NativeNotification extends EventTarget {
  static permission: NotificationPermission = 'granted';
  static requestPermission = vi.fn(async (): Promise<NotificationPermission> => 'granted');
  static fail = false;
  static calls: NativeNotification[] = [];
  close = vi.fn(() => this.dispatchEvent(new Event('close')));
  constructor(readonly title: string, readonly options: NotificationOptions) {
    super();
    if (NativeNotification.fail) throw new Error('PRIVATE_NATIVE_FAILURE');
    NativeNotification.calls.push(this);
  }
}
const operation: NotificationOperation = {
  id: 'PRIVATE_OPERATION_ID', kind: 'DISCOVER', state: 'COMPLETED', updated_at: '2026-11-13T00:00:00Z',
};
function installLocks() {
  const queues = new Map<string, Promise<unknown>>();
  const request = vi.fn((name: string, _options: unknown, callback: () => unknown) => {
    const result = (queues.get(name) || Promise.resolve()).catch(() => {}).then(callback);
    queues.set(name, result.catch(() => {}));
    return result;
  });
  Object.defineProperty(navigator, 'locks', {configurable: true, value: {request}});
  return request;
}
beforeEach(() => {
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('Notification', NativeNotification);
  NativeNotification.permission = 'granted';
  NativeNotification.requestPermission = vi.fn(async (): Promise<NotificationPermission> => 'granted');
  NativeNotification.fail = false;
  NativeNotification.calls = [];
  localStorage.clear();
  installLocks();
});
afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  Reflect.deleteProperty(navigator, 'locks');
});

describe('desktop browser provider contract', () => {
  it('does not request permission during detection and distinguishes unsupported boundaries', async () => {
    const provider = createDesktopNotificationProvider();
    expect(provider.availability()).toBe('available');
    expect(provider.permission()).toBe('granted');
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
    NativeNotification.permission = 'denied';
    expect(await provider.requestPermission()).toBe('denied');
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
    NativeNotification.permission = 'default';
    expect(await provider.requestPermission()).toBe('granted');
    expect(NativeNotification.requestPermission).toHaveBeenCalledOnce();
    vi.stubGlobal('isSecureContext', false);
    expect(provider.availability()).toBe('insecure');
    vi.stubGlobal('isSecureContext', true);
    Object.defineProperty(navigator, 'locks', {configurable: true, value: undefined});
    expect(provider.availability()).toBe('coordination-unavailable');
    installLocks();
    vi.stubGlobal('Notification', undefined);
    expect(provider.availability()).toBe('unsupported');
  });

  it('validates bounded operation metadata and emits only fixed private-data-free labels', () => {
    const unsafe = {...operation, id: 'OTHER_PRIVATE_ID', kind: 'PRIVATE_RESUME_TEXT'};
    const events = desktopEvents([operation, operation, unsafe,
      {...operation, state: 'RUNNING'}, {...operation, updated_at: 'invalid'},
      {...operation, id: 'x'.repeat(129)}, null as unknown as NotificationOperation]);
    expect(events).toHaveLength(2);
    expect(events.map(event => event.body)).toEqual(['Job discovery completed.', 'Background task completed.']);
    const provider = createDesktopNotificationProvider();
    expect(provider.deliver('PRIVATE_PROFILE', [events[0]], vi.fn())).toBe('accepted');
    const notification = NativeNotification.calls[0];
    expect(notification.title).toBe('MJobs activity');
    expect(notification.options.body).toBe('Job discovery completed.');
    expect(JSON.stringify(notification.options)).not.toMatch(/PRIVATE|OPERATION_ID|2026/);
    expect(notification.options.tag).toMatch(/^mjobs-activity-[a-f0-9]+$/);
    expect(notification.options.silent).toBe(true);
    expect(provider.deliver('PRIVATE_PROFILE', events, vi.fn())).toBe('accepted');
    expect(NativeNotification.calls[1].options.body).toBe('Background tasks have new outcomes. Open MJobs Activity to review.');
  });

  it('serializes two tab consumers and persists one attempt for the same outcome', async () => {
    const events = desktopEvents([operation]);
    await setDesktopPreference('alpha', true, [], () => true);
    const first = createDesktopNotificationProvider();
    const second = createDesktopNotificationProvider();
    const results = await Promise.all([
      dispatchDesktopEvents('alpha', events, first, () => true, vi.fn()),
      dispatchDesktopEvents('alpha', events, second, () => true, vi.fn()),
    ]);
    expect(results).toEqual(['accepted', 'suppressed']);
    expect(NativeNotification.calls).toHaveLength(1);
    expect(readDesktopPreferences('alpha').seen).toEqual(events.map(event => event.key));
    await setDesktopPreference('beta', true, [], () => true);
    expect(await dispatchDesktopEvents('beta', events, second, () => true, vi.fn())).toBe('accepted');
    expect(NativeNotification.calls).toHaveLength(2);
  });

  it('keeps distinct updates queued behind the same lock and bounds every stored ledger', async () => {
    const first = desktopEvents([operation]);
    const second = desktopEvents([{...operation, state: 'FAILED', updated_at: '2026-11-13T00:00:01Z'}]);
    await setDesktopPreference('alpha', true, [], () => true);
    const provider = createDesktopNotificationProvider();
    await Promise.all([
      dispatchDesktopEvents('alpha', first, provider, () => true, vi.fn()),
      dispatchDesktopEvents('alpha', second, provider, () => true, vi.fn()),
    ]);
    expect(NativeNotification.calls).toHaveLength(2);
    expect(readDesktopPreferences('alpha').seen).toHaveLength(2);
    const keys = Array.from({length: 900}, (_, index) => 'receipt-' + index);
    await setDesktopPreference('alpha', true, keys, () => true);
    expect(readDesktopPreferences('alpha').seen).toHaveLength(MAX_DESKTOP_EVENTS);
    expect(boundedDesktopKeys([null, {}, 2, '', 'x'.repeat(513), 'safe', 'safe'])).toEqual(['safe']);
    expect(desktopEvents(Array.from({length: 900}, (_, index) => ({...operation, id: String(index)})))).toHaveLength(MAX_DESKTOP_EVENTS);
  });

  it('reserves failed attempts without retry storms and reports asynchronous native errors', async () => {
    const events = desktopEvents([operation]);
    await setDesktopPreference('alpha', true, [], () => true);
    const provider = createDesktopNotificationProvider();
    NativeNotification.fail = true;
    expect(await dispatchDesktopEvents('alpha', events, provider, () => true, vi.fn())).toBe('failed');
    NativeNotification.fail = false;
    expect(await dispatchDesktopEvents('alpha', events, provider, () => true, vi.fn())).toBe('suppressed');
    expect(NativeNotification.calls).toHaveLength(0);
    const onError = vi.fn();
    const fresh = desktopEvents([{...operation, state: 'FAILED'}]);
    expect(await dispatchDesktopEvents('alpha', fresh, provider, () => true, onError)).toBe('accepted');
    NativeNotification.calls[0].dispatchEvent(new Event('error'));
    expect(onError).toHaveBeenCalledOnce();
  });

  it('never delivers when disabled, permission revoked, storage denied or owner obsolete', async () => {
    const provider = createDesktopNotificationProvider();
    const events = desktopEvents([operation]);
    expect(await dispatchDesktopEvents('alpha', events, provider, () => true, vi.fn())).toBe('suppressed');
    await setDesktopPreference('alpha', true, [], () => true);
    NativeNotification.permission = 'denied';
    expect(await dispatchDesktopEvents('alpha', events, provider, () => true, vi.fn())).toBe('suppressed');
    NativeNotification.permission = 'granted';
    expect(await dispatchDesktopEvents('alpha', events, provider, () => false, vi.fn())).toBe('suppressed');
    expect(await setDesktopPreference('beta', true, [], () => false)).toBe(false);
    expect(localStorage.getItem(desktopStorageKey('beta'))).toBeNull();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('PRIVATE_STORAGE_ERROR'); });
    await expect(dispatchDesktopEvents('alpha', events, provider, () => true, vi.fn())).rejects.toThrow();
    expect(NativeNotification.calls).toHaveLength(0);
  });

  it('fails closed for corrupt/oversized preference records and normalizes bounded data', () => {
    const key = desktopStorageKey('alpha');
    for (const raw of ['{bad', 'null', '{"version":1,"enabled":"true","seen":[]}', 'x'.repeat(280001)]) {
      localStorage.setItem(key, raw);
      expect(readDesktopPreferences('alpha')).toEqual({version: 1, enabled: false, seen: []});
    }
    localStorage.setItem(key, JSON.stringify({version: 1, enabled: true, seen: [
      ...Array.from({length: 600}, (_, index) => String(index)), null, 'x'.repeat(600),
    ]}));
    expect(readDesktopPreferences('alpha').seen).toHaveLength(MAX_DESKTOP_EVENTS);
    expect(() => desktopStorageKey('')).toThrow('DESKTOP_PROFILE_INVALID');
    expect(() => desktopStorageKey('x'.repeat(129))).toThrow('DESKTOP_PROFILE_INVALID');
    expect(() => desktopStorageKey('\ud800')).toThrow('DESKTOP_PROFILE_INVALID');
  });

  it('bounds native handles and closes them when the consumer stops', () => {
    const provider = createDesktopNotificationProvider();
    const events = desktopEvents([operation]);
    for (let index = 0; index < 9; index++) provider.deliver('alpha', events, vi.fn());
    expect(NativeNotification.calls.slice(0, 4).every(item => item.close.mock.calls.length === 1)).toBe(true);
    provider.close();
    expect(NativeNotification.calls.every(item => item.close.mock.calls.length === 1)).toBe(true);
    provider.close();
    expect(NativeNotification.calls.every(item => item.close.mock.calls.length === 1)).toBe(true);
  });
});
