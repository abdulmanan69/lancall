"""Runtime configuration: command line flags with LANCALL_* environment defaults."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__

ROOT = Path(__file__).resolve().parent.parent


@dataclass
class Config:
    host: str = "0.0.0.0"
    port: int = 8443
    http_port: int = 8080  # plain-HTTP helper (certificate download + redirect); 0 disables
    data_dir: Path = ROOT / "data"
    web_dir: Path = ROOT / "web"
    tls: bool = True
    trust_proxy: bool = False  # believe X-Forwarded-* headers from a reverse proxy in front
    cert_file: Path | None = None  # bring-your-own certificate
    key_file: Path | None = None
    extra_names: list[str] = field(default_factory=list)  # extra DNS names / IPs for the certificate
    server_name: str = "LAN Call"
    pin: str = ""  # access code required to enrol a new phone; empty = open
    admin_password: str = ""  # administration console; empty = generated on first start
    ext_start: int = 100
    ring_timeout: float = 45.0
    grace: float = 15.0  # seconds a dropped phone may reconnect before its call is ended
    prune_days: int = 0  # forget phones not seen for this many days; 0 = never
    max_devices: int = 2000
    max_connections: int = 1000
    max_connections_per_ip: int = 30
    ice_servers: list[dict] = field(default_factory=list)  # empty: direct LAN paths only
    turn_urls: list[str] = field(default_factory=list)  # relay for phones that cannot reach each other
    turn_secret: str = ""  # coturn static-auth-secret; phones get short-lived passwords made from it
    log_level: str = "INFO"

    @property
    def cert_dir(self) -> Path:
        return self.data_dir / "certs"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(f"LANCALL_{name}", default)


def parse_args(argv: list[str] | None = None) -> Config:
    d = Config()
    p = argparse.ArgumentParser(
        prog="lancall",
        description="Self-hosted telephone system for a local network.",
        epilog="Every option can also be set with a LANCALL_<OPTION> environment variable, "
        "for example LANCALL_PORT=443 or LANCALL_PIN=2468.",
    )
    p.add_argument("--version", action="version", version=f"lancall {__version__}")
    p.add_argument("--host", default=_env("HOST", d.host), help="address to listen on (default %(default)s)")
    p.add_argument("--port", type=int, default=int(_env("PORT", str(d.port))), help="HTTPS port (default %(default)s)")
    p.add_argument(
        "--http-port",
        type=int,
        default=int(_env("HTTP_PORT", str(d.http_port))),
        help="plain HTTP port for certificate download and redirect; 0 disables (default %(default)s)",
    )
    p.add_argument("--data-dir", type=Path, default=Path(_env("DATA_DIR", str(d.data_dir))), help="where the directory, certificates and logs are kept")
    p.add_argument("--name", dest="server_name", default=_env("NAME", d.server_name), help="name shown in the app (default %(default)r)")
    p.add_argument("--pin", default=_env("PIN", ""), help="access code a new phone must enter to join (default: none)")
    p.add_argument("--admin-password", default=_env("ADMIN_PASSWORD", ""), help="password for the administration console at /admin and /platform (default: generated and written to the data folder)")
    p.add_argument("--ext-start", type=int, default=int(_env("EXT_START", str(d.ext_start))), help="first extension number handed out (default %(default)s)")
    p.add_argument("--ring-timeout", type=float, default=float(_env("RING_TIMEOUT", str(d.ring_timeout))), help="seconds before an unanswered call gives up (default %(default)s)")
    p.add_argument("--prune-days", type=int, default=int(_env("PRUNE_DAYS", str(d.prune_days))), help="forget phones not seen for this many days; 0 keeps them forever")
    p.add_argument("--cert", type=Path, default=_env("CERT") or None, help="use this TLS certificate instead of the built-in local CA")
    p.add_argument("--key", type=Path, default=_env("KEY") or None, help="private key for --cert")
    p.add_argument("--san", action="append", default=[s for s in _env("SAN").split(",") if s.strip()], metavar="NAME", help="extra DNS name or IP to put in the generated certificate (repeatable)")
    p.add_argument("--no-tls", action="store_true", default=_env("NO_TLS") in ("1", "true", "yes"), help="serve plain HTTP; only for running behind a TLS-terminating reverse proxy")
    p.add_argument("--trust-proxy", action="store_true", default=_env("TRUST_PROXY") in ("1", "true", "yes"), help="take the client address and scheme from X-Forwarded-* headers; only when a reverse proxy you control is the sole way in")
    p.add_argument("--ice-servers", default=_env("ICE_SERVERS"), metavar="JSON", help='JSON list of RTCIceServer objects, e.g. \'[{"urls":"stun:10.0.0.5:3478"}]\' (default: none, direct LAN only)')
    p.add_argument("--turn-url", action="append", default=[s for s in _env("TURN_URLS").split(",") if s.strip()], metavar="URL", help="TURN relay address such as turn:relay.example.com:3478 (repeatable); for phones on networks that block direct connections")
    p.add_argument("--turn-secret", default=_env("TURN_SECRET"), help="the relay's shared secret (coturn: static-auth-secret)")
    p.add_argument("--log-level", default=_env("LOG_LEVEL", d.log_level), choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    a = p.parse_args(argv)

    if bool(a.turn_url) != bool(a.turn_secret):
        p.error("--turn-url and --turn-secret must be given together")
    if bool(a.cert) != bool(a.key):
        p.error("--cert and --key must be given together")
    if not 1 <= a.port <= 65535 or not 0 <= a.http_port <= 65535:
        p.error("ports must be between 1 and 65535")
    if a.ext_start < 10 or a.ext_start > 99999:
        p.error("--ext-start must be between 10 and 99999")
    ice: list[dict] = []
    if a.ice_servers:
        try:
            ice = json.loads(a.ice_servers)
            if not isinstance(ice, list) or not all(isinstance(x, dict) and "urls" in x for x in ice):
                raise ValueError
        except ValueError:
            p.error("--ice-servers must be a JSON list of objects with a 'urls' key")

    return Config(
        host=a.host,
        port=a.port,
        http_port=0 if a.no_tls else a.http_port,
        data_dir=a.data_dir.resolve(),
        tls=not a.no_tls,
        trust_proxy=a.trust_proxy,
        cert_file=a.cert,
        key_file=a.key,
        extra_names=[s.strip() for s in a.san if s.strip()],
        server_name=a.server_name.strip()[:40] or d.server_name,
        pin=a.pin.strip(),
        admin_password=a.admin_password,
        ext_start=a.ext_start,
        ring_timeout=max(5.0, a.ring_timeout),
        prune_days=max(0, a.prune_days),
        ice_servers=ice,
        turn_urls=[s.strip() for s in a.turn_url if s.strip()],
        turn_secret=a.turn_secret.strip(),
        log_level=a.log_level,
    )
