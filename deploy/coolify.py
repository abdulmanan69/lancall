#!/usr/bin/env python3
"""Deploy LAN Call to a Coolify server through its API.

Coolify builds from a single Dockerfile here, so the source is packed into the
Dockerfile itself; no git repository or image registry is needed.

    python deploy/coolify.py --url https://coolify.example.com \\
        --token-file ~/.coolify-token --domain https://phones.example.com \\
        --env LANCALL_PIN=2468

Run it again after changing the code to publish the new version. The token is
read from a file (or the COOLIFY_TOKEN environment variable) and never printed.
"""

from __future__ import annotations

import argparse
import base64
import io
import json
import os
import sys
import tarfile
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORT = "8000"


def pack_source() -> str:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for folder in ("lancall", "web"):
            for path in sorted((ROOT / folder).rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    tar.add(path, arcname=path.relative_to(ROOT).as_posix())
        tar.add(ROOT / "requirements.txt", arcname="requirements.txt")
    return "\n".join(textwrap.wrap(base64.b64encode(buffer.getvalue()).decode(), 76))


def dockerfile() -> str:
    return f"""# syntax=docker/dockerfile:1
FROM python:3.13-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \\
    LANCALL_NO_TLS=1 LANCALL_TRUST_PROXY=1 \\
    LANCALL_HOST=0.0.0.0 LANCALL_PORT={PORT} LANCALL_DATA_DIR=/data
WORKDIR /app
COPY <<'LANCALL_SOURCE' /tmp/source.b64
{pack_source()}
LANCALL_SOURCE
RUN python -c "import base64,io,tarfile; tarfile.open(fileobj=io.BytesIO(base64.b64decode(open('/tmp/source.b64').read())), mode='r:gz').extractall('/app')" \\
    && rm /tmp/source.b64 \\
    && pip install --no-cache-dir -r requirements.txt \\
    && mkdir -p /data
VOLUME /data
EXPOSE {PORT}
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \\
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:{PORT}/api/health', timeout=3)"
CMD ["python", "-m", "lancall"]
"""


class Coolify:
    def __init__(self, url: str, token: str):
        self.base = url.rstrip("/") + "/api/v1"
        self.token = token

    def call(self, method: str, path: str, body: dict | None = None, ok_missing: bool = False):
        request = urllib.request.Request(
            self.base + path,
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "lancall-deploy",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read()
        except urllib.error.HTTPError as error:
            if ok_missing and error.code in (404, 405, 422):
                return None
            raise SystemExit(f"Coolify {method} {path} failed: {error.code} {error.read().decode(errors='replace')[:600]}")
        return json.loads(raw) if raw else None


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True, help="Coolify address, e.g. https://coolify.example.com")
    p.add_argument("--token-file", type=Path, help="file holding a Coolify API token (or set COOLIFY_TOKEN)")
    p.add_argument("--domain", required=True, help="public address for the app, e.g. https://phones.example.com")
    p.add_argument("--name", default="lancall")
    p.add_argument("--project", default="LAN Call")
    p.add_argument("--env", action="append", default=[], metavar="KEY=VALUE", help="environment variable to set (repeatable)")
    a = p.parse_args()

    token = os.environ.get("COOLIFY_TOKEN") or (a.token_file.expanduser().read_text().strip() if a.token_file else "")
    if not token:
        p.error("give --token-file or set COOLIFY_TOKEN")
    api = Coolify(a.url, token)
    encoded = base64.b64encode(dockerfile().encode()).decode()

    app = next((x for x in api.call("GET", "/applications") if x.get("name") == a.name), None)
    if app is None:
        project = next((x for x in api.call("GET", "/projects") if x.get("name") == a.project), None)
        if project is None:
            project = api.call("POST", "/projects", {"name": a.project, "description": "Browser telephone system"})
            print(f"Created project {a.project!r}")
        server = api.call("GET", "/servers")[0]
        app = api.call(
            "POST",
            "/applications/dockerfile",
            {
                "project_uuid": project["uuid"],
                "server_uuid": server["uuid"],
                "environment_name": "production",
                "name": a.name,
                "description": "LAN Call telephone system",
                "dockerfile": encoded,
                "domains": a.domain,
                "ports_exposes": PORT,
                "instant_deploy": False,
            },
        )
        print(f"Created application {a.name!r}")
        stored = api.call(
            "POST",
            f"/applications/{app['uuid']}/storages",
            {"type": "persistent", "name": f"{a.name}-data", "mount_path": "/data"},
            ok_missing=True,
        )
        print("Persistent storage for /data: " + ("added" if stored is not None else "NOT added (add it in the Coolify UI)"))
    else:
        print(f"Updating application {a.name!r}")
    uuid = app["uuid"]
    # Also after creating: Coolify can ignore the port given at creation and route to 80.
    api.call("PATCH", f"/applications/{uuid}", {"dockerfile": encoded, "domains": a.domain, "ports_exposes": PORT})

    existing = {e["key"] for e in (api.call("GET", f"/applications/{uuid}/envs") or [])}
    for item in a.env:
        key, _, value = item.partition("=")
        api.call("PATCH" if key in existing else "POST", f"/applications/{uuid}/envs", {"key": key, "value": value})
        print(f"Set {key}")

    started = api.call("POST", f"/deploy?uuid={uuid}&force=true")
    deployment = started["deployments"][0]["deployment_uuid"]
    print("Building", end="", flush=True)
    status = "queued"
    for _ in range(120):
        time.sleep(5)
        info = api.call("GET", f"/deployments/{deployment}")
        status = info.get("status", "")
        print(".", end="", flush=True)
        if status in ("finished", "failed", "cancelled-by-user", "error"):
            break
    print(f" {status}")
    if status != "finished":
        try:
            lines = [entry.get("output", "") for entry in json.loads(info.get("logs") or "[]") if not entry.get("hidden")]
        except ValueError:
            lines = [str(info.get("logs"))[-3000:]]
        print("\n".join(lines[-40:]))
        return 1
    print(f"Live at {a.domain}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
