"""Organisations (customers), their exchanges, and administrator sign-in.

One server can run many separate phone systems. Each organisation has its own
directory file, extensions, access code and administrator; nothing is shared
between them. ``orgs.json`` holds the list, written atomically.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import time
from dataclasses import asdict
from pathlib import Path

from .config import Config
from .hub import Hub
from .store import DEFAULT_ORG, SLUG_RE, Directory, Org

log = logging.getLogger("lancall.tenants")

SESSION_LIFETIME = 12 * 3600
LOGIN_WINDOW = 300.0
LOGIN_MAX_FAILURES = 8
RESERVED_SLUGS = {DEFAULT_ORG, "admin", "platform", "api", "ws", "o", "setup", "static"}
MIN_PASSWORD = 8
PLATFORM = "*"  # session scope that may administer every organisation


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def check_password(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        salt_bytes, expected = base64.b64decode(salt), base64.b64decode(digest)
    except ValueError:
        return False
    actual = hashlib.scrypt(password.encode(), salt=salt_bytes, n=2**14, r=8, p=1, dklen=32)
    return hmac.compare_digest(actual, expected)


class Platform:
    def __init__(self, cfg: Config, default_hub: Hub | None = None):
        self.cfg = cfg
        self.path = cfg.data_dir / "orgs.json"
        self.hubs: dict[str, Hub] = {}
        self.platform_hash = ""
        self.dirty = False
        self._sessions: dict[str, tuple[str, float]] = {}  # token -> (scope, expires)
        self._failures: dict[str, list[float]] = {}
        self._load(default_hub)

    # -- persistence -------------------------------------------------------

    def _directory_path(self, slug: str) -> Path:
        if slug == DEFAULT_ORG:
            return self.cfg.data_dir / "directory.json"  # where single-site installs always kept it
        return self.cfg.data_dir / "orgs" / slug / "directory.json"

    def _open(self, org: Org) -> Hub:
        directory = Directory(self._directory_path(org.slug), self.cfg.ext_start)
        directory.load()
        hub = Hub(self.cfg, directory, org)
        self.hubs[org.slug] = hub
        return hub

    def _load(self, default_hub: Hub | None) -> None:
        records: list[dict] = []
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.platform_hash = str(raw.get("platform_hash", ""))
                records = [r for r in raw.get("orgs", []) if isinstance(r, dict)]
            except Exception:
                backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
                os.replace(self.path, backup)
                log.exception("Organisation list was unreadable; moved it to %s", backup)
        fields = set(Org.__dataclass_fields__) - {"dirty"}
        for record in records:
            slug = str(record.get("slug", ""))
            if slug in self.hubs or not (slug == DEFAULT_ORG or SLUG_RE.match(slug)):
                continue
            org = Org(**{k: v for k, v in record.items() if k in fields})
            if slug == DEFAULT_ORG and default_hub is not None:
                default_hub.org = org
                self.hubs[slug] = default_hub
            else:
                self._open(org)
        if DEFAULT_ORG not in self.hubs:
            if default_hub is not None:
                self.hubs[DEFAULT_ORG] = default_hub
            else:
                self._open(Org())
        self._load_history()

        if not self.cfg.admin_password and not self.platform_hash:
            password = secrets.token_urlsafe(12)
            self.platform_hash = hash_password(password)
            self.dirty = True
            note = self.cfg.data_dir / "admin-password.txt"
            try:
                note.parent.mkdir(parents=True, exist_ok=True)
                note.write_text(
                    f"LAN Call administrator password: {password}\n"
                    "Sign in at /admin, change it there, then delete this file.\n",
                    encoding="utf-8",
                )
                with_suppressed_chmod(note)
                log.info("Administrator password created. It is in %s", note)
            except OSError:
                log.warning("Could not write %s. Administrator password: %s", note, password)

    def _load_history(self) -> None:
        """Give each exchange its recent finished calls back after a restart."""
        path = self.cfg.data_dir / "calls.log"
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as fh:
                for line in fh:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    hub = self.hubs.get(record.get("org", DEFAULT_ORG)) if isinstance(record, dict) else None
                    if hub is not None:
                        hub.history.append(record)
        except OSError:
            log.warning("Could not read %s", path)

    def save(self) -> None:
        for hub in self.hubs.values():
            hub.directory.save()
            if hub.org.dirty:
                hub.org.dirty = False
                self.dirty = True
        if not self.dirty:
            return
        orgs = []
        for hub in self.hubs.values():
            record = asdict(hub.org)
            record.pop("dirty", None)
            orgs.append(record)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"version": 1, "platform_hash": self.platform_hash, "orgs": orgs}, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self.dirty = False

    # -- organisations -----------------------------------------------------

    @property
    def default(self) -> Hub:
        return self.hubs[DEFAULT_ORG]

    def hub(self, slug: str) -> Hub | None:
        return self.hubs.get(slug)

    def create(self, slug: str, name: str, seats: int = 0, pin: str = "", admin_password: str = "") -> Hub | str:
        """Returns the new exchange, or an error text."""
        if not SLUG_RE.match(slug) or slug in RESERVED_SLUGS:
            return "The address may use lower-case letters, digits and hyphens (2 to 32 characters)."
        if slug in self.hubs:
            return "That address is already taken."
        if admin_password and len(admin_password) < MIN_PASSWORD:
            return f"A password needs at least {MIN_PASSWORD} characters."
        org = Org(
            slug=slug,
            name=name.strip()[:40] or slug,
            pin=pin.strip(),
            seats=max(0, int(seats)),
            admin_hash=hash_password(admin_password) if admin_password else "",
        )
        hub = self._open(org)
        self.dirty = True
        log.info("Organisation created: %s (%s)", slug, org.name)
        return hub

    def delete(self, slug: str) -> bool:
        if slug == DEFAULT_ORG or slug not in self.hubs:
            return False
        hub = self.hubs.pop(slug)
        hub.shutdown()
        folder = self._directory_path(slug).parent
        if folder.exists():
            # Kept, not destroyed: a deleted customer can be restored by hand.
            trash = self.cfg.data_dir / "deleted" / f"{slug}-{int(time.time())}"
            trash.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(folder), str(trash))
        for token, (scope, _) in list(self._sessions.items()):
            if scope == slug:
                del self._sessions[token]
        self.dirty = True
        log.info("Organisation deleted: %s", slug)
        return True

    def prune(self) -> None:
        for hub in self.hubs.values():
            hub.prune()
        now = time.time()
        for token, (_, expires) in list(self._sessions.items()):
            if expires < now:
                del self._sessions[token]

    def shutdown(self) -> None:
        for hub in self.hubs.values():
            hub.shutdown()
        self.save()

    # -- sign-in -----------------------------------------------------------

    @property
    def password_managed(self) -> bool:
        """True when the platform password comes from configuration and cannot be changed here."""
        return bool(self.cfg.admin_password)

    def _check_platform(self, password: str) -> bool:
        if self.cfg.admin_password:
            return hmac.compare_digest(password.encode(), self.cfg.admin_password.encode())
        return bool(self.platform_hash) and check_password(password, self.platform_hash)

    def locked_out(self, ip: str) -> bool:
        now = time.monotonic()
        recent = [t for t in self._failures.get(ip, []) if now - t < LOGIN_WINDOW]
        self._failures[ip] = recent
        return len(recent) >= LOGIN_MAX_FAILURES

    def login(self, ip: str, password: str, slug: str | None) -> tuple[str, str] | None:
        """Returns (token, scope) or None. ``slug`` None asks for the platform console."""
        scope = None
        if isinstance(password, str) and password:
            hub = self.hubs.get(slug) if slug else None
            if hub is not None and hub.org.admin_hash and check_password(password, hub.org.admin_hash):
                scope = slug
            elif self._check_platform(password):
                scope = PLATFORM
        if scope is None:
            self._failures.setdefault(ip, []).append(time.monotonic())
            log.warning("Wrong administrator password from %s", ip)
            return None
        self._failures.pop(ip, None)
        token = secrets.token_urlsafe(32)
        self._sessions[token] = (scope, time.time() + SESSION_LIFETIME)
        log.info("Administrator signed in from %s (%s)", ip, "platform" if scope == PLATFORM else scope)
        return token, scope

    def logout(self, token: str) -> None:
        self._sessions.pop(token, None)

    def scope(self, token: str | None) -> str | None:
        entry = self._sessions.get(token or "")
        if entry is None:
            return None
        if entry[1] < time.time():
            del self._sessions[token]
            return None
        return entry[0]

    def set_platform_password(self, current: str, new: str) -> str | None:
        if self.password_managed:
            return "The password is set in the server configuration and cannot be changed here."
        if not self._check_platform(current):
            return "The current password is not correct."
        if len(new) < MIN_PASSWORD:
            return f"A password needs at least {MIN_PASSWORD} characters."
        self.platform_hash = hash_password(new)
        self.dirty = True
        with_suppressed_unlink(self.cfg.data_dir / "admin-password.txt")
        log.info("Platform administrator password changed")
        return None


def with_suppressed_chmod(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def with_suppressed_unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass
