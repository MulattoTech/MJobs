import React, {useEffect, useState} from 'react';

export type ThemePreference = 'system' | 'light' | 'dark';
const THEME_KEY = 'mjobs-theme';

export function readThemePreference(storage: Pick<Storage, 'getItem'>): ThemePreference {
  try {
    const value = storage.getItem(THEME_KEY);
    return value === 'light' || value === 'dark' ? value : 'system';
  } catch {return 'system';}
}

export function saveThemePreference(storage: Pick<Storage, 'setItem'>, value: ThemePreference): boolean {
  try {storage.setItem(THEME_KEY, value); return true;} catch {return false;}
}

export function resolveTheme(preference: ThemePreference, systemDark: boolean): 'light' | 'dark' {
  return preference === 'system' ? (systemDark ? 'dark' : 'light') : preference;
}

function browserPreference(): ThemePreference {
  try {return readThemePreference(window.localStorage);} catch {return 'system';}
}

function applyTheme(preference: ThemePreference, systemDark: boolean) {
  const theme = resolveTheme(preference, systemDark);
  document.documentElement.dataset.theme = theme;
  document.documentElement.style.colorScheme = theme;
}

/** Apply the saved appearance before React renders the workspace. */
export function initializeTheme() {
  applyTheme(browserPreference(), window.matchMedia('(prefers-color-scheme: dark)').matches);
}

export function ThemeControl() {
  const [preference, setPreference] = useState<ThemePreference>(browserPreference);
  const [saved, setSaved] = useState(true);
  useEffect(() => {
    const media = window.matchMedia('(prefers-color-scheme: dark)');
    const update = () => applyTheme(preference, media.matches);
    const synchronize = (event: StorageEvent) => {
      if (event.key === THEME_KEY || event.key === null) {
        setPreference(browserPreference());
        setSaved(true);
      }
    };
    update();
    media.addEventListener('change', update);
    window.addEventListener('storage', synchronize);
    return () => {
      media.removeEventListener('change', update);
      window.removeEventListener('storage', synchronize);
    };
  }, [preference]);
  return <div className="theme-control">
    <label htmlFor="workspace-theme">Appearance</label>
    <select id="workspace-theme" value={preference} onChange={event => {
      const next = event.target.value as ThemePreference;
      setPreference(next);
      try {setSaved(saveThemePreference(window.localStorage, next));} catch {setSaved(false);}
    }}>
      <option value="system">System</option>
      <option value="light">Light</option>
      <option value="dark">Dark</option>
    </select>
    {!saved && <small role="status">Appearance applies for this visit. Browser storage is unavailable.</small>}
  </div>;
}
