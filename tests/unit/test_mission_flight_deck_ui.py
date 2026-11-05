"""Contract checks for the locally owned Mission Control navigation/theme."""
from html.parser import HTMLParser
from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "tools" / "agent_dashboard" / "static"


class _PageTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def _page_tags():
    page = _PageTags()
    page.feed((STATIC / "index.html").read_text(encoding="utf-8"))
    return page.tags


def _theme_rules():
    """Read flat declaration blocks, including each selector in grouped rules."""
    theme = (STATIC / "fleet-theme.css").read_text(encoding="utf-8")
    theme = re.sub(r"/\*.*?\*/", "", theme, flags=re.DOTALL)
    rules = {}
    for selectors, block in re.findall(r"([^{}]+)\{([^{}]*)\}", theme):
        declarations = dict(
            (name.strip(), value.strip())
            for declaration in block.split(";") if ":" in declaration
            for name, value in [declaration.split(":", 1)]
        )
        for selector in selectors.split(","):
            rules.setdefault(selector.strip(), {}).update(declarations)
    return rules


def test_mission_control_returns_to_fleet_hub_locally_and_remotely():
    tags = _page_tags()
    js = (STATIC / "mission.js").read_text(encoding="utf-8")
    links = [attrs for tag, attrs in tags
             if tag == "a" and attrs.get("id") == "fleetOverview"]
    assert len(links) == 1
    assert links[0]["href"] == "https://fleet.m-techlabs.com/"
    assert links[0]["aria-label"] == "Return to Fleet Overview with all projects"
    local_href = re.search(r'\$\("#fleetOverview"\)\.href\s*=\s*"([^"]+)";', js)
    assert local_href is not None
    assert local_href.group(1) == "http://127.0.0.1:4320/"
    skip_links = [attrs for tag, attrs in tags
                  if tag == "a" and "fleet-skip" in attrs.get("class", "").split()]
    assert len(skip_links) == 1
    assert skip_links[0]["href"] == "#releaseTracker"
    assert any(attrs.get("id") == "releaseTracker" for _, attrs in tags)


def test_mission_theme_loads_after_every_component_stylesheet():
    stylesheets = [attrs["href"] for tag, attrs in _page_tags()
                   if tag == "link" and attrs.get("rel") == "stylesheet"]
    assert stylesheets.count("/static/fleet-runtime.css") == 1
    assert stylesheets.count("/static/fleet-theme.css?v=1") == 1
    assert stylesheets[-1] == "/static/fleet-theme.css?v=1"


def test_current_progress_is_blue_with_and_without_historical_comparison():
    rules = _theme_rules()
    assert rules[":root"]["--fd-accent"] == "#8bbcff"
    for selector in (
        ".release-meter>span", ".area-progress .fill",
        ".progress-compare-bar .progress-current-fill",
        ".progress-card-summary .progress-compare-bar .progress-current-fill",
    ):
        assert rules[selector]["background"] == "var(--fd-accent)"
    # Do not hide the distinct historical comparison overlay with a broad rule.
    assert ".progress-compare-bar .progress-historical-fill" not in rules


def test_mission_theme_covers_runtime_history_ledger_and_token_drilldowns():
    rules = _theme_rules()
    assert rules[".fleet-runtime-panel"]["background"] == "var(--fd-surface)"
    for selector in (
        ".live-provider-card", ".progress-time-machine", ".progress-item-list",
        ".ledger-highlights", ".ledger-summary>div", ".ledger-coverage details",
        ".ledger-record", ".stats-section", ".token-stat",
    ):
        assert rules[selector]["background"] == "var(--fd-raised)"
        assert rules[selector]["border-color"] == "var(--fd-line)"
    for selector in (".codex-raw-stats", ".antigravity-raw-stats", ".ollama-raw-stats"):
        assert rules[selector]["color"] == "var(--fd-text)"
    assert rules[".progress-delta.up"]["color"] == "var(--fd-positive)"
    assert rules[".progress-delta.down"]["color"] == "var(--fd-danger)"


def test_theme_keeps_prompt_approval_locked_in_initial_markup():
    approvals = [attrs for tag, attrs in _page_tags()
                 if tag == "button" and attrs.get("id") == "launchReviewApprove"]
    assert len(approvals) == 1
    assert "disabled" in approvals[0]


def test_acceptance_polling_is_bounded_and_independent_of_launcher_scans():
    js = (STATIC / "mission.js").read_text(encoding="utf-8")
    assert "if (progressInflight) return;" in js
    assert "finally { progressInflight = false; }" in js
    assert 'fetchJson("/api/progress", {signal: AbortSignal.timeout(8000)})' in js
    assert "if (!document.hidden) refreshProgress(); }, 5000)" in js
    assert "if (!document.hidden) refreshEverything(); }, 11000)" in js
