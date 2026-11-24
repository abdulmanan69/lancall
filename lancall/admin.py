"""Administration API (JSON over HTTPS), used by the consoles at /admin and /platform.

Two kinds of administrator:

* the platform administrator runs the server, creates organisations (customers)
  and may administer every one of them;
* an organisation administrator manages only that organisation's phones.

Sign-in sets an HttpOnly, SameSite=Strict cookie. Every change must also come
from this site's own pages (Origin check plus a custom header), so another web
site cannot make an administrator's browser act for it.
"""

from __future__ import annotations

import csv
import io
import json
import time
from typing import Callable

from aiohttp import web

from . import __version__
from .store import DEFAULT_ORG
from .tenants import MIN_PASSWORD, PLATFORM, SESSION_LIFETIME, Platform, hash_password

COOKIE = "lancall_admin"


def setup(app: web.Application, platform: Platform, client_ip: Callable, is_secure: Callable, same_origin: Callable) -> None:
    def fail(status: int, message: str) -> web.Response:
        return web.json_response({"error": message}, status=status)

    def scope_of(request: web.Request) -> str | None:
        return platform.scope(request.cookies.get(COOKIE))

    async def body(request: web.Request) -> dict:
        try:
            data = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise web.HTTPBadRequest(text="Expected JSON.")
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        return data

    def guarded(platform_only: bool = False):
        """Wrap a handler: signed in, allowed for this organisation, and not a forged request."""

        def wrap(handler):
            async def wrapped(request: web.Request) -> web.StreamResponse:
                if request.method != "GET" and not (same_origin(request) and request.headers.get("X-Requested-With") == "lancall"):
                    return fail(403, "Request refused.")
                scope = scope_of(request)
                if scope is None:
                    return fail(401, "Sign in first.")
                slug = request.match_info.get("org")
                if platform_only or slug is None:
                    if scope != PLATFORM:
                        return fail(403, "Only the platform administrator may do this.")
                elif scope not in (PLATFORM, slug):
                    return fail(403, "You do not administer this organisation.")
                if slug is not None and platform.hub(slug) is None:
                    return fail(404, "No such organisation.")
                return await handler(request, scope)

            return wrapped

        return wrap

    # -- sign-in -----------------------------------------------------------

    async def login(request: web.Request) -> web.Response:
        if not (same_origin(request) and request.headers.get("X-Requested-With") == "lancall"):
            return fail(403, "Request refused.")
        ip = client_ip(request)
        if platform.locked_out(ip):
            return fail(429, "Too many wrong passwords. Try again in a few minutes.")
        data = await body(request)
        slug = data.get("org") if isinstance(data.get("org"), str) else None
        result = platform.login(ip, data.get("password", ""), slug)
        if result is None:
            return fail(401, "That password is not correct.")
        token, scope = result
        response = web.json_response({"scope": "platform" if scope == PLATFORM else "org"})
        response.set_cookie(COOKIE, token, max_age=SESSION_LIFETIME, httponly=True, samesite="Strict", secure=is_secure(request), path="/")
        return response

    async def logout(request: web.Request) -> web.Response:
        platform.logout(request.cookies.get(COOKIE, ""))
        response = web.json_response({})
        response.del_cookie(COOKIE, path="/")
        return response

    async def session(request: web.Request) -> web.Response:
        scope = scope_of(request)
        slug = request.query.get("org")
        allowed = scope is not None and (scope == PLATFORM or (slug is not None and scope == slug))
        return web.json_response({"signed_in": allowed, "scope": None if not allowed else "platform" if scope == PLATFORM else "org"})

    # -- platform ----------------------------------------------------------

    def org_summary(hub) -> dict:
        org = hub.org
        return {
            "slug": org.slug,
            "name": hub.name,
            "path": "/" if org.slug == DEFAULT_ORG else f"/o/{org.slug}/",
            "seats": org.seats,
            "suspended": org.suspended,
            "created": int(org.created * 1000),
            "has_admin_password": bool(org.admin_hash),
            "usage": org.usage.get(time.strftime("%Y-%m"), {"calls": 0, "answered": 0, "seconds": 0}),
            **hub.stats(),
        }

    @guarded(platform_only=True)
    async def platform_overview(request: web.Request, scope: str) -> web.Response:
        return web.json_response(
            {
                "version": __version__,
                "password_managed": platform.password_managed,
                "relay": bool(platform.cfg.turn_urls),
                "orgs": [org_summary(hub) for hub in platform.hubs.values()],
            }
        )

    @guarded(platform_only=True)
    async def org_create(request: web.Request, scope: str) -> web.Response:
        data = await body(request)
        try:
            seats = int(data.get("seats") or 0)
        except (TypeError, ValueError):
            return fail(400, "Seats must be a number.")
        result = platform.create(
            str(data.get("slug", "")).strip().lower(), str(data.get("name", "")), seats,
            str(data.get("pin", "")), str(data.get("admin_password", "")),
        )
        if isinstance(result, str):
            return fail(400, result)
        return web.json_response(org_summary(result), status=201)

    @guarded(platform_only=True)
    async def org_patch(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        data = await body(request)
        org = hub.org
        if "seats" in data:
            try:
                org.seats = max(0, int(data["seats"] or 0))
            except (TypeError, ValueError):
                return fail(400, "Seats must be a number.")
        if "suspended" in data and org.slug != DEFAULT_ORG:
            org.suspended = bool(data["suspended"])
            if org.suspended:
                hub.suspend()
        platform.dirty = True
        return web.json_response(org_summary(hub))

    @guarded(platform_only=True)
    async def org_delete(request: web.Request, scope: str) -> web.Response:
        if not platform.delete(request.match_info["org"]):
            return fail(400, "This organisation cannot be deleted.")
        return web.json_response({})

    @guarded(platform_only=True)
    async def platform_password(request: web.Request, scope: str) -> web.Response:
        data = await body(request)
        error = platform.set_platform_password(str(data.get("current", "")), str(data.get("new", "")))
        return fail(400, error) if error else web.json_response({})

    # -- one organisation --------------------------------------------------

    @guarded()
    async def org_overview(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        org = hub.org
        return web.json_response(
            {
                "scope": "platform" if scope == PLATFORM else "org",
                "version": __version__,
                "org": {
                    "slug": org.slug,
                    "name": hub.name,
                    "path": "/" if org.slug == DEFAULT_ORG else f"/o/{org.slug}/",
                    "pin": hub.pin,
                    "seats": org.seats,  # 0 = no limit set for this organisation
                    "suspended": org.suspended,
                    "monitor_notice": org.monitor_notice,
                    "has_admin_password": bool(org.admin_hash),
                },
                "usage": org.usage,
                **hub.admin_snapshot(),
            }
        )

    @guarded()
    async def org_settings(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        data = await body(request)
        org = hub.org
        if "admin_password" in data:
            password = str(data["admin_password"])
            if password and len(password) < MIN_PASSWORD:
                return fail(400, f"A password needs at least {MIN_PASSWORD} characters.")
            if not password and scope != PLATFORM:
                return fail(400, "Enter a password.")
            org.admin_hash = hash_password(password) if password else ""
        if "name" in data:
            name = str(data["name"]).strip()[:40]
            if not name:
                return fail(400, "Enter a name.")
            org.name = name
        if "pin" in data:
            org.pin = str(data["pin"]).strip()[:64]
        if "monitor_notice" in data:
            org.monitor_notice = bool(data["monitor_notice"])
        platform.dirty = True
        hub.announce_settings()
        return web.json_response({})

    @guarded()
    async def phone_patch(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        data = await body(request)
        changes = {k: data[k] for k in ("name", "ext", "role", "disabled", "dnd") if k in data}
        error = hub.admin_update_phone(request.match_info["ext"], changes)
        return fail(400, error) if error else web.json_response({})

    @guarded()
    async def phone_delete(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        if not hub.admin_remove_phone(request.match_info["ext"]):
            return fail(404, "No phone has that extension.")
        return web.json_response({})

    @guarded()
    async def call_hangup(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        if not hub.admin_hangup(request.match_info["call"]):
            return fail(404, "That call has already ended.")
        return web.json_response({})

    @guarded()
    async def history(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        try:
            limit = max(1, min(2000, int(request.query.get("limit", "200"))))
        except ValueError:
            limit = 200
        return web.json_response({"calls": list(hub.history)[-limit:][::-1]})

    @guarded()
    async def history_csv(request: web.Request, scope: str) -> web.Response:
        hub = platform.hub(request.match_info["org"])
        out = io.StringIO()
        writer = csv.writer(out)
        writer.writerow(["started", "from", "from_name", "to", "to_name", "answered", "seconds", "ended_by", "supervised"])
        for r in hub.history:
            writer.writerow(
                [
                    time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(r.get("started", 0))),
                    *[_csv_safe(r.get(k)) for k in ("from", "from_name", "to", "to_name")],
                    "yes" if r.get("answered") else "no",
                    r.get("duration", 0),
                    r.get("reason", ""),
                    "; ".join(r.get("supervised", [])),
                ]
            )
        return web.Response(
            text=out.getvalue(),
            content_type="text/csv",
            charset="utf-8",
            headers={"Content-Disposition": f'attachment; filename="calls-{hub.org.slug}.csv"'},
        )

    r = app.router
    r.add_post("/api/admin/login", login)
    r.add_post("/api/admin/logout", logout)
    r.add_get("/api/admin/session", session)
    r.add_get("/api/platform", platform_overview)
    r.add_post("/api/platform/orgs", org_create)
    r.add_patch("/api/platform/orgs/{org}", org_patch)
    r.add_delete("/api/platform/orgs/{org}", org_delete)
    r.add_post("/api/platform/password", platform_password)
    r.add_get("/api/orgs/{org}", org_overview)
    r.add_patch("/api/orgs/{org}", org_settings)
    r.add_get("/api/orgs/{org}/history", history)
    r.add_get("/api/orgs/{org}/history.csv", history_csv)
    r.add_patch("/api/orgs/{org}/phones/{ext}", phone_patch)
    r.add_delete("/api/orgs/{org}/phones/{ext}", phone_delete)
    r.add_post("/api/orgs/{org}/calls/{call}/hangup", call_hangup)


def _csv_safe(value: object) -> str:
    """Phone names are user input: stop a spreadsheet from running one as a formula."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text
