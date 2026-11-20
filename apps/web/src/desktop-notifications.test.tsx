// @vitest-environment jsdom
import React, {act} from 'react';
import {createRoot, type Root} from 'react-dom/client';
import {afterEach, beforeEach, describe, expect, it, vi} from 'vitest';
import type {NotificationOperation} from './operation-notifications';
import {desktopStorageKey, readDesktopPreferences} from './desktop-notification-provider';
import {DesktopNotifications} from './desktop-notifications';

class NativeNotification extends EventTarget {
  static permission: NotificationPermission = 'granted';
  static requestPermission = vi.fn(async (): Promise<NotificationPermission> => 'granted');
  static fail = false;
  static calls: NativeNotification[] = [];
  close = vi.fn(() => this.dispatchEvent(new Event('close')));
  constructor(readonly title: string, readonly options: NotificationOptions) {
    super();
    if (NativeNotification.fail) throw new Error('PRIVATE_NATIVE_ERROR');
    NativeNotification.calls.push(this);
  }
}
const initial: NotificationOperation = {
  id: 'old', kind: 'DISCOVER', state: 'COMPLETED', updated_at: '2026-11-13T00:00:00Z',
};
const next = (id: string, kind = 'PREPARE'): NotificationOperation => ({
  id, kind, state: 'COMPLETED', updated_at: '2026-11-13T00:01:00Z',
});
let container: HTMLDivElement;
let root: Root;
let activeRoot = false;
let lockPause: Promise<void> | undefined;

function installLocks() {
  const queues = new Map<string, Promise<unknown>>();
  const request = (name: string, _options: unknown, callback: () => unknown) => {
    const pause = lockPause;
    const result = (queues.get(name) || Promise.resolve()).catch(() => {})
      .then(async () => { if (pause) await pause; return callback(); });
    queues.set(name, result.catch(() => {}));
    return result;
  };
  Object.defineProperty(navigator, 'locks', {configurable: true, value: {request}});
}
async function render(
  operations: NotificationOperation[] = [initial],
  options: {profileId?: string; enabled?: boolean; loadError?: boolean} = {},
) {
  await act(async () => root.render(<DesktopNotifications
    profileId={options.profileId || 'alpha'} enabled={options.enabled ?? true}
    operations={operations} loadError={options.loadError || false}/>));
}
async function click(text: string) {
  const button = Array.from(container.querySelectorAll('button')).find(item => item.textContent === text);
  expect(button, text).toBeDefined();
  await act(async () => button!.click());
}
const enable = () => click('Enable desktop notifications');
const disable = () => click('Disable desktop notifications');
async function unmount() {
  if (activeRoot) {await act(async () => root.unmount()); activeRoot = false;}
}
beforeEach(() => {
  vi.stubGlobal('isSecureContext', true);
  vi.stubGlobal('Notification', NativeNotification);
  NativeNotification.permission = 'granted';
  NativeNotification.requestPermission = vi.fn(async (): Promise<NotificationPermission> => 'granted');
  NativeNotification.calls = [];
  NativeNotification.fail = false;
  localStorage.clear();
  lockPause = undefined;
  installLocks();
  container = document.createElement('div');
  document.body.append(container);
  root = createRoot(container);
  activeRoot = true;
});
afterEach(async () => {
  await unmount();
  container.remove();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  localStorage.clear();
  Reflect.deleteProperty(navigator, 'locks');
});

describe('explicit Desktop notification controls', () => {
  it('requires global and profile opt-in and never replays the initial history', async () => {
    await render([initial], {enabled: false});
    expect(container.textContent).toBe('');
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
    await render();
    expect(container.textContent).toContain('off for this profile');
    expect(NativeNotification.calls).toHaveLength(0);
    await enable();
    expect(container.textContent).toContain('enabled for this profile while this page is open');
    expect(NativeNotification.calls).toHaveLength(0);
    await render([initial, next('new')]);
    expect(NativeNotification.calls).toHaveLength(1);
    expect(NativeNotification.calls[0].options.body).toBe('Document preparation completed.');
    await render([initial, next('new')]);
    expect(NativeNotification.calls).toHaveLength(1);
    await render([initial, next('new'), {...next('new'), state: 'FAILED'}]);
    expect(NativeNotification.calls).toHaveLength(2);
    expect(NativeNotification.calls[1].options.body).toBe('Document preparation needs attention.');
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it('requests permission only after a gesture and baselines outcomes arriving during the prompt', async () => {
    NativeNotification.permission = 'default';
    let finish!: (permission: NotificationPermission) => void;
    NativeNotification.requestPermission = vi.fn(() => new Promise(resolve => { finish = resolve; }));
    await render();
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
    await enable();
    expect(NativeNotification.requestPermission).toHaveBeenCalledOnce();
    await render([initial, next('during-prompt')]);
    await act(async () => { NativeNotification.permission = 'granted'; finish('granted'); });
    expect(container.textContent).toContain('enabled for this profile');
    expect(NativeNotification.calls).toHaveLength(0);
    await render([initial, next('during-prompt'), next('after-prompt')]);
    expect(NativeNotification.calls).toHaveLength(1);
  });

  it.each(['unmount', 'profile', 'global-disable'])('ignores stale permission after %s', async reason => {
    NativeNotification.permission = 'default';
    let finish!: (permission: NotificationPermission) => void;
    NativeNotification.requestPermission = vi.fn(() => new Promise(resolve => { finish = resolve; }));
    await render();
    await enable();
    if (reason === 'unmount') await unmount();
    else if (reason === 'profile') await render([initial], {profileId: 'beta'});
    else await render([initial], {enabled: false});
    await act(async () => { NativeNotification.permission = 'granted'; finish('granted'); });
    expect(localStorage.getItem(desktopStorageKey('alpha'))).toBeNull();
    expect(localStorage.getItem(desktopStorageKey('beta'))).toBeNull();
    expect(NativeNotification.calls).toHaveLength(0);
    if (reason === 'global-disable') {
      await render([initial], {enabled: true});
      expect(container.querySelector('button')!.textContent).toBe('Enable desktop notifications');
      expect(container.querySelector('button')!.disabled).toBe(false);
    }
  });

  it('keeps denied/default permissions off and handles unsupported and insecure contexts', async () => {
    NativeNotification.permission = 'denied';
    await render();
    expect(container.textContent).toContain('blocked in browser settings');
    expect(container.querySelector('button')!.disabled).toBe(true);
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
    NativeNotification.permission = 'default';
    NativeNotification.requestPermission = vi.fn(async (): Promise<NotificationPermission> => 'default');
    await act(async () => window.dispatchEvent(new Event('focus')));
    await enable();
    expect(container.textContent).toContain('off for this profile');
    expect(readDesktopPreferences('alpha').enabled).toBe(false);
    vi.stubGlobal('isSecureContext', false);
    await render([next('insecure')]);
    expect(container.textContent).toContain('need HTTPS or a local browser address');
    vi.stubGlobal('isSecureContext', true);
    Object.defineProperty(navigator, 'locks', {configurable: true, value: undefined});
    await render([next('unsupported')]);
    expect(container.textContent).toContain('unavailable in this browser');
    expect(NativeNotification.calls).toHaveLength(0);
  });

  it('pauses failures and baselines recovery rather than replaying uncertain history', async () => {
    await render();
    await enable();
    await render([initial], {loadError: true});
    expect(container.textContent).toContain('paused while activity updates are unavailable');
    await render([initial, next('during-outage')], {loadError: true});
    await render([initial, next('during-outage')]);
    expect(NativeNotification.calls).toHaveLength(0);
    await render([initial, next('during-outage'), next('after-recovery')]);
    expect(NativeNotification.calls).toHaveLength(1);
    await disable();
    await render([initial, next('disabled-history')], {loadError: true});
    await enable();
    await render([initial, next('disabled-history')]);
    expect(NativeNotification.calls).toHaveLength(1);
    await render([initial, next('disabled-history'), next('new-again')]);
    expect(NativeNotification.calls).toHaveLength(2);
  });

  it('isolates profiles, suppresses reload history, and closes native handles on disable', async () => {
    await render();
    await enable();
    await render([initial, next('alpha-new')]);
    const first = NativeNotification.calls[0];
    await render([next('beta-history')], {profileId: 'beta'});
    expect(first.close).toHaveBeenCalledOnce();
    expect(container.textContent).toContain('off for this profile');
    expect(readDesktopPreferences('alpha').enabled).toBe(true);
    expect(readDesktopPreferences('beta').enabled).toBe(false);
    await render([initial, next('alpha-new')]);
    expect(NativeNotification.calls).toHaveLength(1);
    await unmount();
    root = createRoot(container); activeRoot = true;
    await render([initial, next('alpha-new'), next('while-closed')]);
    expect(NativeNotification.calls).toHaveLength(1);
    await render([initial, next('alpha-new'), next('while-closed'), next('after-reload')]);
    expect(NativeNotification.calls).toHaveLength(2);
    await disable();
    expect(NativeNotification.calls[1].close).toHaveBeenCalledOnce();
    await render([next('disabled')]);
    expect(NativeNotification.calls).toHaveLength(2);
    await enable();
    expect(NativeNotification.calls).toHaveLength(2);
  });

  it('does not retry failed attempts, surfaces native onerror, and recovers on a new outcome', async () => {
    await render(); await enable();
    NativeNotification.fail = true;
    await render([initial, next('failed')]);
    expect(container.textContent).toContain('A desktop alert could not be displayed');
    expect(container.textContent).not.toContain('PRIVATE_NATIVE_ERROR');
    NativeNotification.fail = false;
    await render([initial, next('failed')]);
    expect(NativeNotification.calls).toHaveLength(0);
    await render([initial, next('failed'), next('recover')]);
    expect(NativeNotification.calls).toHaveLength(1);
    expect(container.textContent).toContain('enabled for this profile');
    await act(async () => NativeNotification.calls[0].dispatchEvent(new Event('error')));
    expect(container.textContent).toContain('A desktop alert could not be displayed');
    await disable();
    await act(async () => NativeNotification.calls[0].dispatchEvent(new Event('error')));
    expect(container.textContent).toContain('off for this profile');
  });

  it('synchronizes another tab disabling and fails closed when shared receipts cannot persist', async () => {
    await render(); await enable();
    await render([initial, next('first')]);
    localStorage.setItem(desktopStorageKey('alpha'), JSON.stringify({version: 1, enabled: false, seen: []}));
    await act(async () => window.dispatchEvent(new StorageEvent('storage', {key: desktopStorageKey('alpha')})));
    expect(container.textContent).toContain('off for this profile');
    expect(NativeNotification.calls[0].close).toHaveBeenCalledOnce();
    await enable();
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('PRIVATE_STORAGE_ERROR'); });
    await render([initial, next('first'), next('blocked-storage')]);
    expect(container.textContent).toContain('browser preferences could not be saved');
    expect(container.textContent).not.toContain('PRIVATE_STORAGE_ERROR');
    expect(NativeNotification.calls).toHaveLength(1);
  });

  it('retains failed explicit opt-out across focus and profile remounts', async () => {
    await render(); await enable();
    await render([initial, next('before-disable')]);
    const write = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => { throw new Error('PRIVATE_STORAGE_ERROR'); });
    await disable();
    expect(container.textContent).toContain('off in this tab');
    await act(async () => window.dispatchEvent(new Event('focus')));
    await render([initial, next('before-disable'), next('after-focus')]);
    expect(NativeNotification.calls).toHaveLength(1);
    expect(container.textContent).toContain('off in this tab');
    await render([initial], {profileId: 'beta'});
    await render([initial, next('after-return')]);
    expect(container.textContent).toContain('off in this tab');
    expect(NativeNotification.calls).toHaveLength(1);
    await unmount();
    root = createRoot(container); activeRoot = true;
    await render([initial, next('after-remount')]);
    expect(container.textContent).toContain('off in this tab');
    write.mockRestore();
    await enable();
    expect(container.textContent).toContain('enabled for this profile');
    await render([initial, next('after-remount'), next('explicitly-reenabled')]);
    expect(NativeNotification.calls).toHaveLength(2);
  });

  it('honors another tab opting out during a pending re-enable after a failed disable', async () => {
    await render(); await enable();
    const write = vi.spyOn(Storage.prototype, 'setItem')
      .mockImplementation(() => { throw new Error('PRIVATE_STORAGE_ERROR'); });
    await disable();
    write.mockRestore();
    NativeNotification.permission = 'default';
    let finish!: (permission: NotificationPermission) => void;
    NativeNotification.requestPermission = vi.fn(() => new Promise(resolve => { finish = resolve; }));
    await enable();
    localStorage.setItem(desktopStorageKey('alpha'), JSON.stringify({version: 1, enabled: false, seen: []}));
    await act(async () => window.dispatchEvent(new StorageEvent('storage', {key: desktopStorageKey('alpha')})));
    await act(async () => { NativeNotification.permission = 'granted'; finish('granted'); });
    expect(readDesktopPreferences('alpha').enabled).toBe(false);
    expect(NativeNotification.calls).toHaveLength(0);
    expect(container.textContent).toContain('off in this tab');
    await enable(); await disable();
  });

  it('ignores corrupt preferences and notices permission revoked without prompting', async () => {
    localStorage.setItem(desktopStorageKey('alpha'), '{bad');
    await render();
    expect(container.textContent).toContain('off for this profile');
    await enable();
    NativeNotification.permission = 'denied';
    await act(async () => window.dispatchEvent(new Event('focus')));
    expect(container.textContent).toContain('blocked in browser settings');
    await render([initial, next('denied')]);
    expect(NativeNotification.calls).toHaveLength(0);
    expect(NativeNotification.requestPermission).not.toHaveBeenCalled();
  });

  it('preserves two distinct updates waiting for the shared delivery lock', async () => {
    await render(); await enable();
    let release!: () => void;
    lockPause = new Promise(resolve => { release = resolve; });
    await render([initial, next('first-waiting')]);
    await render([initial, next('first-waiting'), next('second-waiting')]);
    expect(NativeNotification.calls).toHaveLength(0);
    lockPause = undefined;
    await act(async () => release());
    expect(NativeNotification.calls).toHaveLength(2);
    expect(readDesktopPreferences('alpha').seen).toHaveLength(3);
  });

  it('coalesces a burst into one private notification and bounds operation metadata', async () => {
    await render(); await enable();
    const burst = Array.from({length: 600}, (_, index) => next(String(index), 'PRIVATE_PAYLOAD'));
    await render(burst);
    expect(NativeNotification.calls).toHaveLength(1);
    expect(NativeNotification.calls[0].options.body).toBe('Background tasks have new outcomes. Open MJobs Activity to review.');
    expect(JSON.stringify(NativeNotification.calls[0].options)).not.toContain('PRIVATE_PAYLOAD');
    expect(readDesktopPreferences('alpha').seen).toHaveLength(512);
    await render(burst);
    expect(NativeNotification.calls).toHaveLength(1);
  });
});
