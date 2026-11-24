"""The phone directory: every enrolled phone, its extension and its pending missed calls.

Kept in memory and written to ``directory.json`` atomically, so a crash or power
cut can never leave a half-written file behind.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path

log = logging.getLogger("lancall.store")

EXT_RE = re.compile(r"^[1-9][0-9]{1,5}$")  # 2-6 digits, no leading zero
MAX_NAME = 32
MAX_MISSED = 50


def clean_name(raw: object) -> str:
    """Normalise a display name; returns '' when nothing usable is left."""
    if not isinstance(raw, str):
        return ""
    text = unicodedata.normalize("NFC", raw)
    text = "".join(" " if unicodedata.category(ch)[0] in ("C", "Z") else ch for ch in text)
    return " ".join(text.split())[:MAX_NAME].strip()


def hash_secret(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


@dataclass
class Phone:
    id: str
    ext: str
    name: str
    secret_hash: str
    created: float
    last_seen: float
    dnd: bool = False
    missed: list[dict] = field(default_factory=list)
    role: str = "user"  # user | supervisor (may monitor, whisper to and barge into calls)
    disabled: bool = False  # switched off by the administrator; keeps its extension

    def check_secret(self, secret: str) -> bool:
        return hmac.compare_digest(self.secret_hash, hash_secret(secret))


ROLES = ("user", "supervisor")
SLUG_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
DEFAULT_ORG = "default"


@dataclass
class Org:
    """One customer: its own phones, extensions, access code and administrator.

    The built-in ``default`` organisation is served at ``/``; the others at
    ``/o/<slug>/``. ``name`` and ``pin`` left as None fall back to the
    command-line settings (only meaningful for the default organisation).
    """

    slug: str = DEFAULT_ORG
    name: str | None = None
    pin: str | None = None
    admin_hash: str = ""  # organisation administrator's password; empty = platform administrator only
    seats: int = 0  # most phones it may enrol; 0 = the server-wide limit
    suspended: bool = False
    monitor_notice: bool = True  # tell the people on a call when a supervisor listens in
    created: float = field(default_factory=time.time)
    usage: dict[str, dict] = field(default_factory=dict)  # "YYYY-MM" -> calls, answered, seconds
    dirty: bool = False  # not saved; set when something above changed

    def record_call(self, answered: bool, seconds: float) -> None:
        month = self.usage.setdefault(time.strftime("%Y-%m"), {"calls": 0, "answered": 0, "seconds": 0})
        month["calls"] += 1
        month["answered"] += 1 if answered else 0
        month["seconds"] = round(month["seconds"] + seconds)
        for old in sorted(self.usage)[:-24]:
            del self.usage[old]
        self.dirty = True


class Directory:
    def __init__(self, path: Path, ext_start: int = 100):
        self.path = path
        self.ext_start = ext_start
        self.phones: dict[str, Phone] = {}
        self.by_ext: dict[str, str] = {}
        self.dirty = False

    # -- persistence -------------------------------------------------------

    def load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            for item in raw.get("phones", []):
                phone = Phone(
                    id=str(item["id"]),
                    ext=str(item["ext"]),
                    name=clean_name(item.get("name")) or f"Phone {item['ext']}",
                    secret_hash=str(item["secret_hash"]),
                    created=float(item.get("created", 0)),
                    last_seen=float(item.get("last_seen", 0)),
                    dnd=bool(item.get("dnd", False)),
                    missed=[m for m in item.get("missed", []) if isinstance(m, dict)][-MAX_MISSED:],
                    role=item.get("role") if item.get("role") in ROLES else "user",
                    disabled=bool(item.get("disabled", False)),
                )
                if phone.id in self.phones or phone.ext in self.by_ext or not EXT_RE.match(phone.ext):
                    log.warning("Skipping invalid or duplicate directory entry (ext %s)", phone.ext)
                    continue
                self.phones[phone.id] = phone
                self.by_ext[phone.ext] = phone.id
        except Exception:
            # Never start from a file we could not understand and then overwrite it.
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            os.replace(self.path, backup)
            self.phones.clear()
            self.by_ext.clear()
            log.exception("Directory file was unreadable; moved it to %s and starting empty", backup)
            return
        log.info("Loaded %d phone(s) from %s", len(self.phones), self.path)

    def save(self) -> None:
        if not self.dirty:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"version": 1, "phones": [asdict(p) for p in self.phones.values()]}
        tmp = self.path.with_suffix(".json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self.path)
        self.dirty = False

    # -- queries and changes -----------------------------------------------

    def get(self, phone_id: str) -> Phone | None:
        return self.phones.get(phone_id)

    def find_ext(self, ext: str) -> Phone | None:
        phone_id = self.by_ext.get(ext)
        return self.phones.get(phone_id) if phone_id else None

    def next_free_ext(self) -> str:
        n = self.ext_start
        while str(n) in self.by_ext:
            n += 1
        return str(n)

    def ext_available(self, ext: str, for_phone: str | None = None) -> bool:
        if not EXT_RE.match(ext):
            return False
        owner = self.by_ext.get(ext)
        return owner is None or owner == for_phone

    def add(self, phone_id: str, secret: str, name: str, ext: str | None = None) -> Phone:
        if not ext or not self.ext_available(ext):
            ext = self.next_free_ext()
        now = time.time()
        phone = Phone(
            id=phone_id,
            ext=ext,
            name=clean_name(name) or f"Phone {ext}",
            secret_hash=hash_secret(secret),
            created=now,
            last_seen=now,
        )
        self.phones[phone_id] = phone
        self.by_ext[ext] = phone_id
        self.dirty = True
        return phone

    def set_ext(self, phone: Phone, ext: str) -> bool:
        if not self.ext_available(ext, phone.id):
            return False
        if ext != phone.ext:
            del self.by_ext[phone.ext]
            phone.ext = ext
            self.by_ext[ext] = phone.id
            self.dirty = True
        return True

    def remove(self, phone_id: str) -> None:
        phone = self.phones.pop(phone_id, None)
        if phone:
            self.by_ext.pop(phone.ext, None)
            self.dirty = True

    def add_missed(self, phone: Phone, entry: dict) -> None:
        phone.missed.append(entry)
        del phone.missed[:-MAX_MISSED]
        self.dirty = True

    def touch(self, phone: Phone) -> None:
        phone.last_seen = time.time()
        self.dirty = True

    def prune(self, older_than_days: int, keep: set[str]) -> list[Phone]:
        """Forget phones not seen for a long time. ``keep`` holds ids that are connected."""
        if older_than_days <= 0:
            return []
        cutoff = time.time() - older_than_days * 86400
        gone = [p for p in self.phones.values() if p.last_seen < cutoff and p.id not in keep]
        for phone in gone:
            self.remove(phone.id)
        return gone
