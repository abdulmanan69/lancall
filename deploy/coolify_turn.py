#!/usr/bin/env python3
"""Deploy a TURN relay (coturn) next to LAN Call on a Coolify server.

Phones on different networks often cannot reach each other directly; the relay
carries the audio for them. It runs as its own Coolify application and touches
nothing else on the server.

    python deploy/coolify_turn.py --url https://coolify.example.com \\
        --token-file ~/.coolify-token --secret-file ~/.lancall-turn-secret \\
        --public-ip 203.0.113.10 --realm phones.example.com

The server's firewall must allow UDP and TCP 3478 and UDP 49160-49200.
Then give LAN Call the same secret:

    LANCALL_TURN_URLS=turn:phones.example.com:3478?transport=udp,turn:phones.example.com:3478?transport=tcp
    LANCALL_TURN_SECRET=<contents of the secret file>
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from coolify import Coolify  # noqa: E402

LISTEN = 3478
RELAY_PORTS = (49160, 49200)  # 41 ports: about 40 relayed calls at once
# Never relay into the server's own or any private network.
DENIED_PEERS = [
    "0.0.0.0-0.255.255.255", "10.0.0.0-10.255.255.255", "100.64.0.0-100.127.255.255",
    "127.0.0.0-127.255.255.255", "169.254.0.0-169.254.255.255", "172.16.0.0-172.31.255.255",
    "192.168.0.0-192.168.255.255", "224.0.0.0-255.255.255.255",
]


def dockerfile(public_ip: str, realm: str) -> str:
    low, high = RELAY_PORTS
    flags = [
        "-n", "--log-file=stdout", "--no-cli", "--no-tls", "--no-dtls", "--fingerprint",
        "--no-multicast-peers", "--no-software-attribute",
        f"--listening-port={LISTEN}", f"--external-ip={public_ip}",
        f"--min-port={low}", f"--max-port={high}", f"--realm={realm}",
        "--use-auth-secret", '--static-auth-secret="$TURN_SECRET"',
        "--user-quota=8", "--total-quota=80",
        *[f"--denied-peer-ip={r}" for r in DENIED_PEERS],
    ]
    # The shell is only there to read the secret from the environment, so it never sits in the image.
    return f"""FROM coturn/coturn:4.6-alpine
EXPOSE {LISTEN}
ENTRYPOINT ["sh", "-c", {json.dumps("exec turnserver " + " ".join(flags))}]
"""


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--url", required=True)
    p.add_argument("--token-file", type=Path)
    p.add_argument("--secret-file", type=Path, required=True, help="shared secret; created if missing")
    p.add_argument("--public-ip", required=True, help="the server's public IPv4 address")
    p.add_argument("--realm", required=True, help="any name for the relay, usually the LAN Call host name")
    p.add_argument("--name", default="lancall-turn")
    p.add_argument("--project", default="LAN Call")
    a = p.parse_args()

    token = os.environ.get("COOLIFY_TOKEN") or (a.token_file.expanduser().read_text().strip() if a.token_file else "")
    if not token:
        p.error("give --token-file or set COOLIFY_TOKEN")
    secret_file = a.secret_file.expanduser()
    if not secret_file.exists():
        secret_file.write_text(secrets.token_urlsafe(32))
        print(f"Created secret in {secret_file}")
    secret = secret_file.read_text().strip()

    api = Coolify(a.url, token)
    encoded = base64.b64encode(dockerfile(a.public_ip, a.realm).encode()).decode()
    low, high = RELAY_PORTS
    mappings = f"{LISTEN}:{LISTEN}/udp,{LISTEN}:{LISTEN}/tcp,{low}-{high}:{low}-{high}/udp"

    app = next((x for x in api.call("GET", "/applications") if x.get("name") == a.name), None)
    if app is None:
        project = next((x for x in api.call("GET", "/projects") if x.get("name") == a.project), None)
        if project is None:
            project = api.call("POST", "/projects", {"name": a.project})
        server = api.call("GET", "/servers")[0]
        app = api.call(
            "POST",
            "/applications/dockerfile",
            {
                "project_uuid": project["uuid"],
                "server_uuid": server["uuid"],
                "environment_name": "production",
                "name": a.name,
                "description": "TURN relay for LAN Call",
                "dockerfile": encoded,
                "ports_exposes": str(LISTEN),
                "ports_mappings": mappings,
                "health_check_enabled": False,
                "instant_deploy": False,
            },
        )
        print(f"Created application {a.name!r}")
    else:
        print(f"Updating application {a.name!r}")
    uuid = app["uuid"]
    # No web address: this is not a website and must not get a route in the proxy.
    api.call("PATCH", f"/applications/{uuid}", {"dockerfile": encoded, "ports_exposes": str(LISTEN), "ports_mappings": mappings, "domains": ""})

    existing = {e["key"] for e in (api.call("GET", f"/applications/{uuid}/envs") or [])}
    api.call("PATCH" if "TURN_SECRET" in existing else "POST", f"/applications/{uuid}/envs", {"key": "TURN_SECRET", "value": secret})

    started = api.call("POST", f"/deploy?uuid={uuid}&force=true")
    deployment = started["deployments"][0]["deployment_uuid"]
    print("Building", end="", flush=True)
    status, info = "queued", {}
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
        print("\n".join(lines[-40:]).replace(secret, "***"))
        return 1
    print(f"Relay running on {a.public_ip}:{LISTEN}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
