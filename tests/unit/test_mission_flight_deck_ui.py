"""Contract checks for the locally owned Mission Control navigation/theme."""
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "tools" / "agent_dashboard" / "static"


def test_mission_control_returns_to_fleet_hub_locally_and_remotely():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    js = (STATIC / "mission.js").read_text(encoding="utf-8")
    assert 'id="fleetOverview"' in page
    assert "http://127.0.0.1:4320/" in js
    assert "https://fleet.m-techlabs.com/" in page
    assert 'href="#releaseTracker"' in page


def test_mission_control_uses_neutral_flight_deck_theme_without_changing_acceptance():
    page = (STATIC / "index.html").read_text(encoding="utf-8")
    theme = (STATIC / "fleet-theme.css").read_text(encoding="utf-8")
    assert 'href="/static/fleet-theme.css?v=1"' in page
    assert "--fd-accent:#8bbcff" in theme
    assert ".release-meter>span,.area-progress .fill{background:var(--fd-accent)}" in theme
    assert "fetch(" not in theme
