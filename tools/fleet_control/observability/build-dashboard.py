"""Generate a provisioned, read-only Grafana dashboard from sanitized fleet telemetry."""

import json
from pathlib import Path

ROOT = Path(__file__).parent
DS = {"type": "yesoreyeram-infinity-datasource", "uid": "fleet-native"}
URL = "http://fleet-analytics-data:8080/analytics.json"
PANELS = []


def query(root, columns, fmt="table"):
    return {
        "refId": "A",
        "datasource": DS,
        "type": "json",
        "source": "url",
        "url": URL,
        "url_options": {"method": "GET", "data": ""},
        "parser": "backend",
        "format": fmt,
        "root_selector": root if root == "$" else "($age := $millis() - $toMillis(generatedAt); $age >= -10000 and $age <= 90000 ? (" + root + ") : [])",
        "columns": [
            {"selector": selector, "text": name, "type": kind}
            for selector, name, kind in columns
        ],
    }


def panel(title, kind, x, y, w, h, target=None, unit="none", description=""):
    item = {
        "id": len(PANELS) + 1,
        "title": title,
        "description": description,
        "type": kind,
        "gridPos": {"x": x, "y": y, "w": w, "h": h},
        "datasource": DS,
        "targets": [target] if target else [],
        "fieldConfig": {
            "defaults": {
                "unit": unit,
                "color": {"mode": "palette-classic"},
                "thresholds": {
                    "mode": "absolute",
                    "steps": [{"color": "green", "value": None}],
                },
            },
            "overrides": [],
        },
        "options": {},
    }
    if kind == "stat":
        item["options"] = {
            "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False},
            "orientation": "auto",
            "textMode": "auto",
            "colorMode": "value",
            "graphMode": "none",
            "justifyMode": "auto",
            "wideLayout": True,
        }
    if kind == "timeseries":
        item["options"] = {
            "legend": {"displayMode": "list", "placement": "bottom"},
            "tooltip": {"mode": "multi", "sort": "desc"},
        }
        item["fieldConfig"]["defaults"]["custom"] = {
            "drawStyle": "line",
            "lineInterpolation": "smooth",
            "fillOpacity": 15,
            "lineWidth": 2,
            "showPoints": "never",
            "axisPlacement": "auto",
        }
    if kind == "table":
        item["options"] = {
            "showHeader": True,
            "cellHeight": "sm",
            "footer": {"show": False},
            "sortBy": [],
        }
    if kind == "barchart":
        item["options"] = {
            "orientation": "horizontal",
            "showValue": "auto",
            "groupWidth": 0.7,
            "barWidth": 0.8,
            "legend": {"displayMode": "hidden"},
            "tooltip": {"mode": "single"},
        }
    PANELS.append(item)
    return item


intro = panel(
    "FLEET ATLAS · Native telemetry, not simulated productivity", "text", 0, 0, 24, 3
)
intro["options"] = {
    "mode": "markdown",
    "content": "## Work, not waiting.\n[Open the visual Fleet Atlas](/mission-control/analytics/) · [Mission Control](/mission-control/)\n\n**Executing** means a native active turn. Reserved agents are **on-call**, not busy. ETA values are conditional. Token use is not acceptance. Missing per-turn attribution remains unavailable.",
}
for i, (title, key, unit) in enumerate(
    [
        ("Executing now", "executing", "short"),
        ("Queued", "queued", "short"),
        ("On-call reserve", "reserved", "short"),
        ("Completed turns · 24h", "completedTurns", "short"),
    ]
):
    panel(
        title,
        "stat",
        i * 6,
        3,
        6,
        4,
        query("[summary]", [(key, title, "number")]),
        unit,
    )
panel(
    "Output-token flow · 15-minute buckets",
    "timeseries",
    0,
    7,
    16,
    8,
    query(
        "tokenSeries",
        [("at", "Time", "timestamp"), ("output", "Output tokens", "number")],
        "timeseries",
    ),
    "short",
    "Native reported usage. The underlying feed is a rolling 24-hour window.",
)
panel(
    "Models by observed output tokens",
    "barchart",
    16,
    7,
    8,
    8,
    query(
        "models", [("model", "Model", "string"), ("output", "Output tokens", "number")]
    ),
    "short",
    "Observed usage models, not just the configured launch model.",
)
for i, (title, root, key, unit) in enumerate(
    [
        ("Input tokens · 24h", "summary", "inputTokens", "short"),
        ("Cache hits · 24h", "summary", "cachedInputTokens", "short"),
        ("Documentation drafts ready", "productivity", "artifactsReady", "short"),
        ("Quota remaining", "quota", "remainingPercent", "percent"),
    ]
):
    panel(
        title,
        "stat",
        i * 6,
        15,
        6,
        4,
        query("[" + root + "]", [(key, title, "number")]),
        unit,
    )
panel(
    "Dispatch flight board",
    "table",
    0,
    19,
    24,
    10,
    query(
        "agents",
        [
            ("name", "Agent", "string"),
            ("work.label", "Workflow state", "string"),
            ("work.assignedTask", "Assigned / planned task", "string"),
            ("work.queuePosition", "Queue position", "number"),
            ("work.estimatedStartAt", "Conditional start estimate", "timestamp"),
            ("work.nextEvaluationAt", "Next dispatcher check", "timestamp"),
            ("turnsCompleted", "Completed turns", "number"),
        ],
    ),
    description="An ETA is not a promise. CI waits, reserves, resource holds and paused dispatch remain distinct.",
)
for i, (title, key, unit) in enumerate(
    [
        ("Selected agent · completed turns", "turnsCompleted", "short"),
        ("Median turn wall time", "medianTurnSeconds", "s"),
        ("95th percentile turn", "p95TurnSeconds", "s"),
        ("Selected output tokens", "usage.output", "short"),
    ]
):
    panel(
        title,
        "stat",
        i * 6,
        29,
        6,
        4,
        query("agents[id='${agent}']", [(key, title, "number")]),
        unit,
    )
panel(
    "Selected agent · recorded workflow states",
    "state-timeline",
    0,
    33,
    24,
    7,
    query(
        "agents[id='${agent}'].transitions",
        [("at", "Time", "timestamp"), ("state", "Workflow", "string")],
    ),
    description="State observation begins when this collector was installed; no historical state is fabricated.",
)
panel(
    "Selected agent · latest 30 turns",
    "table",
    0,
    40,
    24,
    10,
    query(
        "agents[id='${agent}'].latestTurns",
        [
            ("startedAt", "Started", "timestamp"),
            ("endedAt", "Ended", "timestamp"),
            ("durationSeconds", "Wall seconds", "number"),
            ("state", "State", "string"),
            ("inputTokens", "Input tokens", "number"),
            ("cachedInputTokens", "Cached tokens", "number"),
            ("outputTokens", "Output tokens", "number"),
            ("usageAttribution", "Attribution basis", "string"),
        ],
    ),
    description="Per-turn usage requires a unique native-conversation and time-window match. Missing values are not zero; wall time includes tools and waiting.",
)
panel(
    "Freshness receipt",
    "table",
    0,
    50,
    24,
    5,
    query(
        "$",
        [
            ("generatedAt", "Feed generated", "timestamp"),
            ("source.observedAt", "Native source observed", "timestamp"),
            ("source.usageWindowComplete", "Usage import complete", "boolean"),
            ("dispatch.lastSuccess", "Last dispatch", "timestamp"),
        ],
    ),
    description="Do not infer current activity when the feed timestamp is over 90 seconds old. Fleet Atlas marks stale data explicitly.",
)
agents = [
    "mjobs-luna-" + str(n)
    for n in [
        3,
        4,
        5,
        6,
        7,
        8,
        9,
        10,
        11,
        12,
        17,
        18,
        19,
        20,
        21,
        22,
        23,
        24,
        25,
        26,
        27,
        28,
        29,
        30,
        31,
        32,
        33,
        36,
        37,
        38,
        39,
    ]
]
dashboard = {
    "uid": "fleet-atlas",
    "title": "Fleet Atlas · Operations Intelligence",
    "tags": ["fleet", "native-telemetry", "read-only"],
    "schemaVersion": 40,
    "version": 1,
    "editable": False,
    "style": "dark",
    "timezone": "browser",
    "refresh": "30s",
    "time": {"from": "now-24h", "to": "now"},
    "graphTooltip": 1,
    "templating": {
        "list": [
            {
                "name": "agent",
                "label": "Inspect agent",
                "type": "custom",
                "query": ",".join(agents),
                "options": [
                    {"text": x, "value": x, "selected": x == "mjobs-luna-36"}
                    for x in agents
                ],
                "current": {"text": "mjobs-luna-36", "value": "mjobs-luna-36"},
                "includeAll": False,
                "multi": False,
                "hide": 0,
            }
        ]
    },
    "panels": PANELS,
}
(ROOT / "dashboards").mkdir(exist_ok=True)
(ROOT / "dashboards/fleet-atlas.json").write_text(
    json.dumps(dashboard, indent=2), encoding="utf-8"
)
print(
    json.dumps({"dashboard": "fleet-atlas", "panels": len(PANELS), "modelRequests": 0})
)
