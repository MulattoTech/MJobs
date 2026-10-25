"""Guarded, fixed-target Fleet Control route on Mission Control's loopback port."""
from urllib.parse import urlsplit
import httpx
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse, Response

ASSETS = {"", "index.html", "app.js", "turbo-ui.js", "sustain-ui.js", "master-ui.js",
          "restore-ui.js", "quota-ui.js", "find-work-ui.js", "style.css", "turbo.css",
          "master.css", "restore.css", "quota.css", "find-work.css"}
READS = {"api/health", "api/state", "api/environment", "api/quota", "api/master",
         "api/preflight", "api/providers", "api/gemini-usage", "api/agent-plan",
         "api/turbo", "api/turbo/preview", "api/turbo/all-hands/preview",
         "api/sustain", "api/sustain/preview", "api/find-work/preview",
         "api/find-work/status", "api/master/restore/preview"}
WRITES = {"api/nudge", "api/replace", "api/retire", "api/agent-plan",
          "api/agent-plan/apply", "api/find-work", "api/find-work/all",
          "api/quota/config", "api/quota/arm", "api/quota/disarm",
          "api/master/pause", "api/master/drain", "api/master/cancel-drain",
          "api/master/restore", "api/master/restore/pause", "api/master/emergency",
          "api/master/rearm", "api/sustain/toggle", "api/sustain/config",
          "api/sustain/tick", "api/turbo/toggle", "api/turbo/config",
          "api/turbo/run", "api/turbo/all-hands"}


async def _relay(method, tail, body, headers):
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False,
                                 timeout=httpx.Timeout(95, connect=2)) as client:
        return await client.request(method, "http://127.0.0.1:8787/" + tail,
                                    content=body, headers=headers)


def install_fleet_proxy(app, relay=None):
    relay = relay or _relay

    @app.get("/fleet")
    async def fleet_redirect():
        return RedirectResponse("/fleet/", status_code=307)

    @app.api_route("/fleet/{tail:path}", methods=["GET", "POST"])
    async def fleet_proxy(tail: str, request: Request):
        # The target is fixed. Never accept URLs, arbitrary paths or redirect hops.
        if request.client is None or request.client.host not in {"127.0.0.1", "::1"}:
            raise HTTPException(403, "Loopback clients only")
        host = request.headers.get("host", "")
        parsed_host = urlsplit("http://" + host)
        if parsed_host.hostname not in {"127.0.0.1", "localhost"}:
            raise HTTPException(403, "Local host only")
        allowed = ASSETS | READS if request.method == "GET" else WRITES
        if tail not in allowed or request.url.query:
            raise HTTPException(404, "Unknown Fleet Control route")
        if request.method == "POST":
            if request.headers.get("x-ao-fleet") != "1":
                raise HTTPException(403, "Fleet request header required")
            origin = request.headers.get("origin")
            if origin is not None and origin != str(request.base_url).rstrip("/"):
                raise HTTPException(403, "Same-origin controls only")
            if request.headers.get("sec-fetch-site") == "cross-site":
                raise HTTPException(403, "Cross-site controls are blocked")
            if request.headers.get("content-type", "").split(";")[0] != "application/json":
                raise HTTPException(415, "JSON controls only")
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 65536:
                raise HTTPException(413, "Control request too large")
        headers = {"Host": "127.0.0.1:8787", "Content-Type": "application/json"}
        if request.method == "POST":
            headers["X-AO-Fleet"] = "1"
            headers["Origin"] = "http://127.0.0.1:8787"
        try:
            upstream = await relay(request.method, tail, bytes(body), headers)
        except (httpx.HTTPError, OSError):
            raise HTTPException(503, "Fleet Control is unavailable; no automatic retry was sent") from None
        content = upstream.content
        content_type = upstream.headers.get("content-type", "application/octet-stream")
        if "text/html" in content_type:
            text = content.decode("utf-8")
            for asset in ASSETS:
                for attribute in ("src", "href"):
                    text = text.replace(f'{attribute}="/{asset}"',
                                        f'{attribute}="/fleet/{asset}"')
            content = text.encode("utf-8")
        response_headers = {
            "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
            "Referrer-Policy": "no-referrer",
            "Content-Security-Policy": upstream.headers.get(
                "content-security-policy", "default-src 'none'; frame-ancestors 'none'"),
        }
        return Response(content=content, status_code=upstream.status_code,
                        media_type=content_type, headers=response_headers)
