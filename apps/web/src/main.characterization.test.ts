import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';

const mainSource = readFileSync(new URL('./main.tsx', import.meta.url), 'utf8');
const navigationSource = readFileSync(new URL('./navigation.tsx', import.meta.url), 'utf8');
const indexSource = readFileSync(new URL('../index.html', import.meta.url), 'utf8');

describe('web entrypoint characterization', () => {
  it('keeps the existing primary navigation order and labels', () => {
    expect(navigationSource).toContain(
      "export type Tab = 'Jobs'|'Pipeline'|'Profile'|'Career Stories'|'Learning'|'Sources'|'Applications'|'Review Queue'|'AI Settings'|'Activity';",
    );
    expect(navigationSource).toContain(
      "export const MAIN_TABS: Tab[]",
    );
    expect(navigationSource).toContain(
      "if(tab==='Jobs')return 'Find your next opportunity.'",
    );
  });

  it('keeps first-run actions mapped to their current tabs and anchors', () => {
    expect(navigationSource).toContain(
      "'create-profile':{tab:'Profile',anchor:'create-profile'}",
    );
    expect(navigationSource).toContain(
      "'upload-resume':{tab:'Profile',anchor:'upload-resume'}",
    );
    expect(navigationSource).toContain(
      "'review-extracted-facts':{tab:'Profile',anchor:'review-extracted-facts'}",
    );
    expect(navigationSource).toContain(
      "'configure-desired-jobs':{tab:'Sources',anchor:'configure-desired-jobs'}",
    );
    expect(navigationSource).toContain(
      "'configure-location':{tab:'Profile',anchor:'configure-location'}",
    );
    expect(navigationSource).toContain(
      "'configure-application-preferences':{tab:'Profile',anchor:'configure-application-preferences'}",
    );
    expect(navigationSource).toContain(
      "'configure-ai':{tab:'AI Settings',anchor:''}",
    );
    expect(navigationSource).toContain(
      "'enable-sources':{tab:'Sources',anchor:'enable-sources'}",
    );
  });

  it('keeps tab changes as the navigation cancellation seam', () => {
    expect(mainSource).toMatch(
      /function setTab\(next:Tab\) \{\s*\+\+navigationSequence\.current;\s*\+\+appSequence\.current;\s*setApplicationLoading\(null\);\s*applicationRestoreProfile\.current=profileRef\.current;\s*setTabState\(next\);\s*\}/,
    );
  });

  it('keeps the document root and startup initialization order', () => {
    expect(indexSource).toContain('<div id="root"></div>');
    expect(mainSource).toMatch(
      /initializeTheme\(\);\s*createRoot\(document\.getElementById\('root'\)!\)\.render\(<React\.StrictMode><App\/><\/React\.StrictMode>\);/,
    );
  });
});
