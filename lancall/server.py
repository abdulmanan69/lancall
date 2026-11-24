"""HTTPS + WebSocket front end. One port serves the app and the signalling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import ssl
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from . import __version__, admin, certs, netinfo
from .config import Config
from .hub import Hub, Session
from .store import DEFAULT_ORG
from .tenants import Platform

log = logging.getLogger("lancall.server")

MAX_MESSAGE = 64 * 1024
HELLO_TIMEOUT = 15.0
PLATFORM_KEY = web.AppKey("platform", Platform)
CFG_KEY = web.AppKey("cfg", Config)

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".webmanifest": "application/manifest+json",
}


@web.middleware
async def security_headers(request: web.Request, handler):
    try:
        response = await handler(request)
    except web.HTTPException as exc:
        response = exc
    if isinstance(response, web.WebSocketResponse):
        return response
    scheme = "wss" if _is_secure(request) else "ws"
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        f"media-src 'self' blob:; connect-src 'self' {scheme}://{request.host}; "
        "base-uri 'none'; form-action 'self'; frame-ancestors 'none'; object-src 'none'",
    )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    response.headers.setdefault("Permissions-Policy", "microphone=(self), camera=(), geolocation=()")
    response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
    # Deliberately no HSTS: with a private CA it would turn a certificate
    # problem into a lock-out with no way for the user to proceed.
    response.headers.setdefault("Cache-Control", "no-cache")
    if isinstance(response, web.HTTPException):
        raise response
    return response


def _static_handler(path: Path):
    content_type = CONTENT_TYPES.get(path.suffix, "application/octet-stream")

    async def handler(request: web.Request) -> web.StreamResponse:
        if "org" in request.match_info:
            _hub_for(request)  # an unknown organisation gets a 404, not somebody else's page
        response = web.FileResponse(path)  # handles ETag / If-None-Match / ranges
        response.headers["Content-Type"] = content_type
        return response

    return handler


def _hub_for(request: web.Request) -> Hub:
    hub = request.app[PLATFORM_KEY].hub(request.match_info.get("org", DEFAULT_ORG))
    if hub is None:
        raise web.HTTPNotFound(text="No phone system at this address.")
    return hub


async def health(request: web.Request) -> web.Response:
    platform = request.app[PLATFORM_KEY]
    totals = {"phones": 0, "online": 0, "calls": 0}
    for hub in platform.hubs.values():
        stats = hub.stats()
        for key in totals:
            totals[key] += stats[key]
    return web.json_response(
        {"status": "ok", "version": __version__, **totals, "uptime": platform.default.stats()["uptime"], "organisations": len(platform.hubs)}
    )


async def ca_certificate(request: web.Request) -> web.StreamResponse:
    path = request.app[CFG_KEY].cert_dir / "ca.crt"
    if request.app[CFG_KEY].cert_file or not path.exists():
        raise web.HTTPNotFound(text="This server does not use the built-in certificate authority.")
    return web.Response(
        body=path.read_bytes(),
        content_type="application/x-x509-ca-cert",
        headers={"Content-Disposition": 'attachment; filename="lancall-ca.crt"'},
    )


def _is_secure(request: web.Request) -> bool:
    if request.app[CFG_KEY].trust_proxy:
        return request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower() == "https"
    return request.secure


def _client_ip(request: web.Request) -> str:
    if request.app[CFG_KEY].trust_proxy:
        # The proxy appends the address it saw, so the last entry is the one it vouches for.
        forwarded = request.headers.get("X-Forwarded-For", "").split(",")[-1].strip()
        if forwarded:
            return forwarded
    return request.remote or "unknown"


def _same_origin(request: web.Request) -> bool:
    origin = request.headers.get("Origin")
    if origin is None:
        return True  # not a browser; browsers always send Origin on WebSocket handshakes
    return urlsplit(origin).netloc.lower() == request.host.lower()


async def websocket(request: web.Request) -> web.StreamResponse:
    hub, cfg = _hub_for(request), request.app[CFG_KEY]
    if not _same_origin(request):
        raise web.HTTPForbidden(text="Cross-origin connections are not allowed.")
    ip = _client_ip(request)
    if len(hub.connections) >= cfg.max_connections:
        raise web.HTTPServiceUnavailable(text="Server is at capacity.")
    if sum(1 for s in hub.connections if s.ip == ip) >= cfg.max_connections_per_ip:
        raise web.HTTPTooManyRequests(text="Too many connections from this address.")

    ws = web.WebSocketResponse(heartbeat=20.0, max_msg_size=MAX_MESSAGE)
    await ws.prepare(request)
    session = Session(ip)
    loop = asyncio.get_running_loop()

    async def writer() -> None:
        # A cancel (session.abort: the phone stopped reading) still falls through
        # to the close below, so the socket never lingers.
        with contextlib.suppress(ConnectionError, asyncio.TimeoutError, RuntimeError, asyncio.CancelledError):
            while True:
                item = await session.outbox.get()
                if item is None:
                    break
                await asyncio.wait_for(ws.send_str(item), 10)
        with contextlib.suppress(Exception):
            await ws.close(code=session.close_code)

    writer_task = asyncio.create_task(writer())
    session.abort = writer_task.cancel

    def hello_deadline() -> None:
        if session.phone_id is None and not session.closed:
            session.close(4002)

    deadline = loop.call_later(HELLO_TIMEOUT, hello_deadline)
    hub.connect(session)
    try:
        async for message in ws:
            if message.type is WSMsgType.TEXT:
                hub.receive(session, message.data)
            elif message.type is WSMsgType.BINARY:
                session.close(1003)
            else:
                break
    finally:
        deadline.cancel()
        hub.disconnect(session)
        if not writer_task.done():
            with contextlib.suppress(asyncio.QueueFull):
                session.outbox.put_nowait(None)
            try:
                await asyncio.wait_for(asyncio.shield(writer_task), 2)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                writer_task.cancel()
        if not ws.closed:
            with contextlib.suppress(Exception):
                await ws.close()
    return ws


async def _maintenance(app: web.Application):
    platform = app[PLATFORM_KEY]

    async def loop() -> None:
        ticks = 0
        while True:
            await asyncio.sleep(5)
            ticks += 1
            try:
                platform.save()  # small files; on the loop so it never races a change
                if ticks % 720 == 0:  # hourly
                    platform.prune()
            except Exception:
                log.exception("Maintenance task failed")

    task = asyncio.create_task(loop())
    yield
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    platform.shutdown()


async def _add_slash(request: web.Request) -> web.Response:
    _hub_for(request)
    raise web.HTTPPermanentRedirect(f"/o/{request.match_info['org']}/")


def build_app(cfg: Config, hub: Hub | None = None) -> web.Application:
    """``hub``, when given, serves the default organisation (used by the tests)."""
    platform = Platform(cfg, hub)
    app = web.Application(middlewares=[security_headers], client_max_size=64 * 1024)
    app[PLATFORM_KEY] = platform
    app[CFG_KEY] = cfg
    add = app.router.add_get
    add("/api/health", health)
    add("/ca.crt", ca_certificate)
    admin.setup(app, platform, _client_ip, _is_secure, _same_origin)

    web_dir = cfg.web_dir
    index, console = _static_handler(web_dir / "index.html"), _static_handler(web_dir / "admin.html")
    add("/setup", _static_handler(web_dir / "setup.html"))
    add("/platform", console)
    add("/o/{org}", _add_slash)
    # Every organisation gets the same app under its own address, so relative
    # links in the pages work unchanged and each keeps its own browser storage keys.
    for prefix in ("", "/o/{org}"):
        add(f"{prefix}/", index)
        add(f"{prefix}/ws", websocket)
        add(f"{prefix}/admin", console)
        for path in sorted(web_dir.iterdir()):
            if path.is_file() and path.suffix in CONTENT_TYPES and not path.name.startswith("."):
                add(f"{prefix}/{path.name}", _static_handler(path))
    app.cleanup_ctx.append(_maintenance)
    return app


def build_redirect_app(cfg: Config) -> web.Application:
    """Plain-HTTP helper: hands out the CA certificate and points everything else at HTTPS."""

    async def redirect(request: web.Request) -> web.Response:
        host = request.host.rsplit(":", 1)[0] if request.host.count(":") == 1 else request.host
        port = "" if cfg.port == 443 else f":{cfg.port}"
        raise web.HTTPFound(f"https://{host}{port}{request.rel_url}")

    app = web.Application(client_max_size=1024)
    app[CFG_KEY] = cfg
    app.router.add_get("/ca.crt", ca_certificate)
    app.router.add_get("/setup", _static_handler(cfg.web_dir / "setup.html"))
    app.router.add_get("/setup.css", _static_handler(cfg.web_dir / "setup.css"))
    app.router.add_get("/icon.svg", _static_handler(cfg.web_dir / "icon.svg"))
    app.router.add_get("/doodle-strolling.svg", _static_handler(cfg.web_dir / "doodle-strolling.svg"))
    app.router.add_route("*", "/{tail:.*}", redirect)
    return app


def _certificate_names(cfg: Config) -> list[str]:
    host = netinfo.hostname().lower()
    return [host, f"{host}.local", *netinfo.local_ips(), *cfg.extra_names]


def _tls_context(cfg: Config) -> ssl.SSLContext:
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    if cfg.cert_file and cfg.key_file:
        context.load_cert_chain(cfg.cert_file, cfg.key_file)
    else:
        certs.ensure(cfg.cert_dir, _certificate_names(cfg), netinfo.hostname())
        context.load_cert_chain(cfg.cert_dir / "server.crt", cfg.cert_dir / "server.key")
    return context


async def _certificate_watch(cfg: Config, context: ssl.SSLContext) -> None:
    """Re-issue the certificate when the machine's address changes or expiry nears.

    New connections pick the new certificate up at once; no restart needed.
    """
    while True:
        await asyncio.sleep(300)
        try:
            if await asyncio.to_thread(certs.ensure, cfg.cert_dir, _certificate_names(cfg), netinfo.hostname()):
                context.load_cert_chain(cfg.cert_dir / "server.crt", cfg.cert_dir / "server.key")
                log.info("Loaded renewed certificate. Reachable at: %s", ", ".join(urls(cfg)))
        except Exception:
            log.exception("Certificate check failed")


def urls(cfg: Config) -> list[str]:
    scheme = "https" if cfg.tls else "http"
    default = 443 if cfg.tls else 80
    port = "" if cfg.port == default else f":{cfg.port}"
    hosts = netinfo.local_ips() or ["localhost"]
    if cfg.host not in ("0.0.0.0", "::", ""):
        hosts = [cfg.host]
    return [f"{scheme}://{h}{port}" for h in hosts]


async def serve(cfg: Config, stop: asyncio.Event) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    context = _tls_context(cfg) if cfg.tls else None

    runner = web.AppRunner(build_app(cfg), access_log=None, handle_signals=False)
    await runner.setup()
    runners = [runner]
    watch: asyncio.Task | None = None
    try:
        await web.TCPSite(runner, cfg.host, cfg.port, ssl_context=context, reuse_address=None).start()
        if cfg.http_port:
            helper = web.AppRunner(build_redirect_app(cfg), access_log=None, handle_signals=False)
            await helper.setup()
            runners.append(helper)
            try:
                await web.TCPSite(helper, cfg.host, cfg.http_port).start()
            except OSError as exc:
                log.warning("Could not open helper port %d (%s); continuing without it.", cfg.http_port, exc)
                cfg.http_port = 0
        if context is not None and not cfg.cert_file:
            watch = asyncio.create_task(_certificate_watch(cfg, context))

        log.info("LAN Call %s is running.", __version__)
        for url in urls(cfg):
            log.info("  Open on any phone or computer:  %s", url)
        if cfg.tls and not cfg.cert_file:
            setup_host = (netinfo.local_ips() or ["localhost"])[0]
            if cfg.http_port:
                log.info("  First-time phone setup (trust the certificate):  http://%s:%d/setup", setup_host, cfg.http_port)
            else:
                log.info("  First-time phone setup (trust the certificate):  %s/setup", urls(cfg)[0])
        if cfg.pin:
            log.info("  New phones need the access code to join.")
        log.info("  Administration:  %s/admin   (all customers: %s/platform)", urls(cfg)[0], urls(cfg)[0])
        await stop.wait()
    finally:
        log.info("Shutting down...")
        if watch:
            watch.cancel()
        for r in reversed(runners):
            with contextlib.suppress(Exception):
                await r.cleanup()
