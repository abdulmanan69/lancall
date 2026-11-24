"""The exchange: registration, presence and the call state machine.

Everything here is synchronous and runs on the event loop thread, so the state
of a call can never be observed half-changed. Outgoing messages go through each
session's bounded queue; a phone that stops reading is dropped, never waited on.

Wire protocol (JSON text frames, one object per frame, ``type`` selects):

  phone -> server   hello, set_profile, call, accept, decline, hangup, signal,
                    missed_ack, report, ping, transfer,
                    supervise, sup_signal, sup_mode, sup_stop
  server -> phone   welcome, profile, directory, ringing, incoming, accepted,
                    call_failed, ended, signal, missed, replaced, error, pong,
                    role, calls, transfer, transfer_failed,
                    sup_started, sup_failed, sup_join, sup_update, sup_signal,
                    sup_mode, sup_end

Supervision: a phone with the supervisor role joins an active call with its own
audio connection to each of the two parties ("legs" a = caller, b = callee).
Monitor, whisper and barge differ only in who the supervisor's microphone
reaches, so switching between them needs no renegotiation.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable

from . import __version__
from .config import Config
from .store import EXT_RE, ROLES, Directory, Org, Phone, clean_name

log = logging.getLogger("lancall.hub")
cdr = logging.getLogger("lancall.cdr")

PROTOCOL_VERSION = 1
ID_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
SECRET_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")

OUTBOX_SIZE = 256
RATE_PER_SECOND = 30.0
RATE_BURST = 120.0
PIN_WINDOW = 300.0
PIN_MAX_FAILURES = 10
TURN_CREDENTIAL_LIFETIME = 12 * 3600
HISTORY_SIZE = 2000  # finished calls kept in memory for the administration console
SUP_MODES = ("monitor", "whisper", "barge")


def _is_lan_ipv4(text: str) -> bool:
    try:
        address = ipaddress.ip_address(text)
    except ValueError:
        return False
    return address.version == 4 and address.is_private and not address.is_loopback


class Session:
    """One WebSocket connection."""

    def __init__(self, ip: str):
        self.ip = ip
        self.phone_id: str | None = None
        self.outbox: asyncio.Queue[str | None] = asyncio.Queue(maxsize=OUTBOX_SIZE)
        self.closed = False
        self.close_code = 1000
        self.abort: Callable[[], None] | None = None  # set by the transport
        self._tokens = RATE_BURST
        self._stamp = time.monotonic()
        self.opened = self._stamp

    def send(self, message: dict) -> None:
        if self.closed:
            return
        try:
            self.outbox.put_nowait(json.dumps(message, separators=(",", ":")))
        except asyncio.QueueFull:
            self.closed = True
            if self.abort:
                self.abort()

    def close(self, code: int = 1000) -> None:
        """Flush what is queued, then close the connection."""
        if self.closed:
            return
        self.closed = True
        self.close_code = code
        try:
            self.outbox.put_nowait(None)
        except asyncio.QueueFull:
            if self.abort:
                self.abort()

    def allow(self) -> bool:
        now = time.monotonic()
        self._tokens = min(RATE_BURST, self._tokens + (now - self._stamp) * RATE_PER_SECOND)
        self._stamp = now
        if self._tokens < 1:
            return False
        self._tokens -= 1
        return True


@dataclass
class Call:
    id: str
    caller: str  # phone ids
    callee: str
    state: str = "ringing"  # ringing -> active
    started: float = field(default_factory=time.time)
    answered: float | None = None
    ring_timer: asyncio.TimerHandle | None = None
    supervised: list[str] = field(default_factory=list)  # for the call record: "ext mode"

    def other(self, phone_id: str) -> str:
        return self.callee if phone_id == self.caller else self.caller


@dataclass
class Supervision:
    id: str
    call_id: str
    supervisor: str  # phone id
    mode: str = "monitor"
    target: str = ""  # phone id that hears a whisper


class Hub:
    def __init__(self, cfg: Config, directory: Directory, org: Org | None = None):
        self.cfg = cfg
        self.directory = directory
        self.org = org or Org()
        self.history: deque[dict] = deque(maxlen=HISTORY_SIZE)
        self.supervisions: dict[str, Supervision] = {}
        self.sup_by_call: dict[str, str] = {}
        self.sup_by_phone: dict[str, str] = {}  # supervisor phone id -> supervision id
        self.connections: set[Session] = set()
        self.sessions: dict[str, Session] = {}  # phone id -> registered session
        self.calls: dict[str, Call] = {}
        self.phone_call: dict[str, str] = {}  # phone id -> call id
        self._grace: dict[str, asyncio.TimerHandle] = {}
        self._pin_failures: dict[str, list[float]] = {}
        self._broadcast_handle: asyncio.TimerHandle | None = None
        self.started = time.time()

    @property
    def name(self) -> str:
        return self.org.name if self.org.name is not None else self.cfg.server_name

    @property
    def pin(self) -> str:
        return self.org.pin if self.org.pin is not None else self.cfg.pin

    @property
    def seats(self) -> int:
        return min(self.org.seats, self.cfg.max_devices) if self.org.seats > 0 else self.cfg.max_devices

    # -- transport hooks ---------------------------------------------------

    def connect(self, session: Session) -> None:
        self.connections.add(session)

    def disconnect(self, session: Session) -> None:
        self.connections.discard(session)
        by_server = session.closed
        session.closed = True
        phone_id = session.phone_id
        if not phone_id or self.sessions.get(phone_id) is not session:
            return
        del self.sessions[phone_id]
        phone = self.directory.get(phone_id)
        if phone:
            self.directory.touch(phone)
            lasted = time.monotonic() - session.opened
            log.info(
                "Offline: %s (%s) after %.0fs, closed by %s",
                phone.ext, phone.name, lasted, "server" if by_server else "phone or network",
            )
        supervision = self.supervisions.get(self.sup_by_phone.get(phone_id, ""))
        if supervision is not None:
            self._end_supervision(supervision, "supervisor_left")  # nobody may be left listening unseen
        if phone_id in self.phone_call:
            # The audio runs phone to phone, so a signalling blip must not end the call.
            self._cancel_grace(phone_id)
            self._grace[phone_id] = asyncio.get_running_loop().call_later(
                self.cfg.grace, self._grace_expired, phone_id
            )
        self._schedule_directory()

    def receive(self, session: Session, raw: str) -> None:
        if not session.allow():
            self._error(session, "rate_limited", "Too many messages.", fatal=True)
            return
        try:
            msg = json.loads(raw)
        except ValueError:
            self._error(session, "bad_message", "Message is not valid JSON.", fatal=True)
            return
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            self._error(session, "bad_message", "Message must be an object with a type.", fatal=True)
            return

        kind = msg["type"]
        if kind == "ping":
            session.send({"type": "pong"})
            return
        if kind == "hello":
            self._hello(session, msg)
            return
        phone = self.directory.get(session.phone_id) if session.phone_id else None
        if phone is None or self.sessions.get(phone.id) is not session:
            self._error(session, "not_registered", "Say hello first.")
            return
        handler = self._handlers.get(kind)
        if handler is None:
            self._error(session, "unknown_type", f"Unknown message type: {kind[:40]}")
            return
        handler(self, session, phone, msg)

    # -- registration ------------------------------------------------------

    def _hello(self, session: Session, msg: dict) -> None:
        if session.phone_id:
            self._error(session, "already_registered", "Already registered on this connection.")
            return
        if msg.get("v") != PROTOCOL_VERSION:
            self._error(session, "unsupported_version", "This app is out of date. Reload the page.", fatal=True)
            return
        phone_id, secret = msg.get("device_id"), msg.get("secret")
        if not (isinstance(phone_id, str) and ID_RE.match(phone_id) and isinstance(secret, str) and SECRET_RE.match(secret)):
            self._error(session, "bad_identity", "Malformed identity.", fatal=True)
            return
        if self.org.suspended:
            self._error(session, "suspended", "This phone system is suspended. Contact your provider.", fatal=True)
            return

        phone = self.directory.get(phone_id)
        if phone is not None:
            if not phone.check_secret(secret):
                log.warning("Rejected wrong secret for extension %s from %s", phone.ext, session.ip)
                self._error(session, "auth_failed", "This phone's saved identity was not accepted.", fatal=True)
                return
            if phone.disabled:
                self._error(session, "disabled", "This phone was switched off by the administrator.", fatal=True)
                return
        else:
            if self.pin and not self._check_pin(session, msg.get("pin")):
                return
            if len(self.directory.phones) >= self.seats:
                self._error(session, "directory_full", "No free phone seats are left. Ask the administrator.", fatal=True)
                return
            wanted = msg.get("ext")
            wanted = str(wanted).strip() if isinstance(wanted, (str, int)) else None
            phone = self.directory.add(phone_id, secret, clean_name(msg.get("name")), wanted)
            log.info("Enrolled new phone: %s (%s) from %s", phone.ext, phone.name, session.ip)

        previous = self.sessions.get(phone.id)
        if previous is not None and previous is not session:
            previous.phone_id = None  # its disconnect must not touch the phone's state
            previous.send({"type": "replaced"})
            previous.close(4001)
        self._cancel_grace(phone.id)
        session.phone_id = phone.id
        self.sessions[phone.id] = session
        self.directory.touch(phone)
        log.info("Online: %s (%s) from %s", phone.ext, phone.name, session.ip)

        session.send(
            {
                "type": "welcome",
                "me": self._me(phone),
                "server": {"name": self.name, "version": __version__},
                "role": phone.role,
                "ice_servers": self._ice_servers(phone),
                "ring_timeout": self.cfg.ring_timeout,
                "call": self._call_snapshot(phone.id),
                "missed": list(phone.missed),
            }
        )
        session.send(self._directory_message())
        if phone.role == "supervisor":
            session.send(self._calls_message())
        self._schedule_directory()

    def _check_pin(self, session: Session, pin: object) -> bool:
        now = time.monotonic()
        failures = [t for t in self._pin_failures.get(session.ip, []) if now - t < PIN_WINDOW]
        if len(failures) >= PIN_MAX_FAILURES:
            self._pin_failures[session.ip] = failures
            self._error(session, "pin_locked", "Too many wrong access codes. Try again in a few minutes.")
            return False
        if isinstance(pin, str) and hmac.compare_digest(pin.encode(), self.pin.encode()):
            self._pin_failures.pop(session.ip, None)
            return True
        if pin:  # an empty pin is the app asking whether one is needed, not a guess
            failures.append(now)
            self._pin_failures[session.ip] = failures
            log.warning("Wrong access code from %s", session.ip)
            self._error(session, "pin_invalid", "That access code is not correct.")
        else:
            self._error(session, "pin_required", "An access code is needed to join.")
        return False

    def _set_profile(self, session: Session, phone: Phone, msg: dict) -> None:
        if "name" in msg:
            name = clean_name(msg["name"])
            if not name:
                self._error(session, "invalid_name", "Enter a name.")
                return
        if "ext" in msg:
            ext = str(msg["ext"]).strip() if isinstance(msg["ext"], (str, int)) else ""
            if not EXT_RE.match(ext):
                self._error(session, "invalid_ext", "An extension is 2 to 6 digits and cannot start with 0.")
                return
            if not self.directory.set_ext(phone, ext):
                self._error(session, "ext_taken", f"Extension {ext} is already in use.")
                return
        if "name" in msg and name != phone.name:
            phone.name = name
            self.directory.dirty = True
        if "dnd" in msg and bool(msg["dnd"]) != phone.dnd:
            phone.dnd = bool(msg["dnd"])
            self.directory.dirty = True
        session.send({"type": "profile", "me": self._me(phone)})
        self._schedule_directory()

    def _missed_ack(self, session: Session, phone: Phone, msg: dict) -> None:
        if phone.missed:
            phone.missed.clear()
            self.directory.dirty = True

    # -- calls -------------------------------------------------------------

    def _call(self, session: Session, phone: Phone, msg: dict) -> None:
        to = msg.get("to")
        to = str(to).strip() if isinstance(to, (str, int)) else ""

        def fail(reason: str) -> None:
            session.send({"type": "call_failed", "to": to, "reason": reason})

        if phone.id in self.phone_call or phone.id in self.sup_by_phone:
            fail("already_in_call")
            return
        target = self.directory.find_ext(to) if EXT_RE.match(to) else None
        if target is None or target.disabled:
            fail("not_found")
            return
        if target.id == phone.id:
            fail("self")
            return
        if target.id not in self.sessions:
            self._missed(target, phone, "offline")
            fail("offline")
            return
        if target.dnd:
            self._missed(target, phone, "dnd")
            fail("dnd")
            return
        if target.id in self.phone_call or target.id in self.sup_by_phone:
            self._missed(target, phone, "busy")
            fail("busy")
            return

        call = Call(id=secrets.token_urlsafe(12), caller=phone.id, callee=target.id)
        call.ring_timer = asyncio.get_running_loop().call_later(self.cfg.ring_timeout, self._ring_timeout, call.id)
        self.calls[call.id] = call
        self.phone_call[phone.id] = call.id
        self.phone_call[target.id] = call.id
        # Fresh relay passwords with every call: a page left open for days still works.
        session.send({"type": "ringing", "call_id": call.id, "to": self._peer(target), "ice_servers": self._ice_servers(phone)})
        self.sessions[target.id].send(
            {"type": "incoming", "call_id": call.id, "from": self._peer(phone), "ice_servers": self._ice_servers(target)}
        )
        log.info("Call %s: %s -> %s ringing", call.id, phone.ext, target.ext)
        self._schedule_directory()

    def _own_call(self, session: Session, phone: Phone, msg: dict) -> Call | None:
        call = self.calls.get(msg.get("call_id")) if isinstance(msg.get("call_id"), str) else None
        if call is None or phone.id not in (call.caller, call.callee):
            # Usually a harmless race: the call ended while this message was in flight.
            session.send({"type": "ended", "call_id": msg.get("call_id"), "reason": "gone"})
            return None
        return call

    def _accept(self, session: Session, phone: Phone, msg: dict) -> None:
        call = self._own_call(session, phone, msg)
        if call is None or call.callee != phone.id or call.state != "ringing":
            return
        call.state = "active"
        call.answered = time.time()
        if call.ring_timer:
            call.ring_timer.cancel()
            call.ring_timer = None
        self._send(call.caller, {"type": "accepted", "call_id": call.id})
        log.info("Call %s: answered", call.id)
        self._schedule_directory()  # supervisors see the call once it is live

    def _decline(self, session: Session, phone: Phone, msg: dict) -> None:
        call = self._own_call(session, phone, msg)
        if call is None:
            return
        if call.callee == phone.id and call.state == "ringing":
            self._end(call, "declined", by=phone.id)
        else:
            self._hangup_call(call, phone)

    def _hangup(self, session: Session, phone: Phone, msg: dict) -> None:
        call = self._own_call(session, phone, msg)
        if call is not None:
            self._hangup_call(call, phone)

    def _hangup_call(self, call: Call, phone: Phone) -> None:
        if call.state == "ringing":
            self._end(call, "cancelled" if phone.id == call.caller else "declined", by=phone.id)
        else:
            self._end(call, "hangup", by=phone.id)

    def _signal(self, session: Session, phone: Phone, msg: dict) -> None:
        call = self._own_call(session, phone, msg)
        if call is None or call.state != "active" or not isinstance(msg.get("data"), dict):
            return
        relay = {"type": "signal", "call_id": call.id, "data": msg["data"]}
        # Browsers may hide their LAN address behind a random ".local" name that the
        # other phone has to look up by multicast, which many networks block. We
        # already know the real address, so pass it along. Skipped when both phones
        # appear to share one address (same machine, or a reverse proxy in front).
        other = self.sessions.get(call.other(phone.id))
        if other is not None and other.ip != session.ip and _is_lan_ipv4(session.ip):
            relay["from_ip"] = session.ip
        self._send(call.other(phone.id), relay)

    def _transfer(self, session: Session, phone: Phone, msg: dict) -> None:
        """Blind transfer: this phone leaves and the other party rings the new extension."""
        call = self._own_call(session, phone, msg)
        if call is None or call.state != "active":
            return
        to = msg.get("to")
        to = str(to).strip() if isinstance(to, (str, int)) else ""
        target = self.directory.find_ext(to) if EXT_RE.match(to) else None
        reason = ""
        if target is None or target.disabled:
            reason = "not_found"
        elif target.id in (call.caller, call.callee):
            reason = "self"
        elif target.id not in self.sessions:
            reason = "offline"
        elif target.dnd:
            reason = "dnd"
        elif target.id in self.phone_call or target.id in self.sup_by_phone:
            reason = "busy"
        if reason:
            session.send({"type": "transfer_failed", "call_id": call.id, "reason": reason})
            return
        # The other phone places the new call itself as soon as this one ends.
        self._send(call.other(phone.id), {"type": "transfer", "call_id": call.id, "to": self._peer(target)})
        log.info("Call %s: %s transfers the call to %s", call.id, phone.ext, target.ext)
        self._end(call, "transferred", by=phone.id)

    # -- supervision -------------------------------------------------------

    def _supervise(self, session: Session, phone: Phone, msg: dict) -> None:
        def fail(reason: str) -> None:
            session.send({"type": "sup_failed", "reason": reason})

        if phone.role != "supervisor":
            fail("forbidden")
            return
        if phone.id in self.phone_call or phone.id in self.sup_by_phone:
            fail("busy")
            return
        call = self.calls.get(msg.get("call_id")) if isinstance(msg.get("call_id"), str) else None
        if call is None or call.state != "active":
            fail("gone")
            return
        if call.id in self.sup_by_call:
            fail("taken")
            return
        sup = Supervision(id=secrets.token_urlsafe(12), call_id=call.id, supervisor=phone.id)
        self.supervisions[sup.id] = sup
        self.sup_by_call[call.id] = sup.id
        self.sup_by_phone[phone.id] = sup.id
        call.supervised.append(f"{phone.ext} monitor")
        parties = []
        for leg, party_id in (("a", call.caller), ("b", call.callee)):
            party = self.directory.get(party_id)
            parties.append({"leg": leg, **(self._peer(party) if party else {"ext": "", "name": "Unknown"})})
            if party:
                self._send(
                    party_id,
                    {"type": "sup_join", "sup_id": sup.id, "call_id": call.id, "ice_servers": self._ice_servers(party),
                     **self._party_view(sup, party_id)},
                )
        session.send(
            {"type": "sup_started", "sup_id": sup.id, "call_id": call.id, "mode": sup.mode, "parties": parties,
             "ice_servers": self._ice_servers(phone)}
        )
        log.info("Supervision %s: %s (%s) monitors call %s", sup.id, phone.ext, phone.name, call.id)
        self._schedule_directory()

    def _party_view(self, sup: Supervision, party_id: str) -> dict:
        """What one party is told. A whisper or a barge can be heard, so it is always shown."""
        supervisor = self.directory.get(sup.supervisor)
        by = self._peer(supervisor) if supervisor else None
        if sup.mode == "barge":
            return {"state": "barge", "by": by}
        if sup.mode == "whisper" and sup.target == party_id:
            return {"state": "whisper", "by": by}
        return {"state": "monitor", "by": by} if self.org.monitor_notice else {"state": "hidden"}

    def _own_supervision(self, phone: Phone, msg: dict) -> tuple[Supervision, Call] | tuple[None, None]:
        sup = self.supervisions.get(msg.get("sup_id")) if isinstance(msg.get("sup_id"), str) else None
        call = self.calls.get(sup.call_id) if sup else None
        if sup is None or call is None or phone.id not in (sup.supervisor, call.caller, call.callee):
            return None, None
        return sup, call

    def _sup_signal(self, session: Session, phone: Phone, msg: dict) -> None:
        sup, call = self._own_supervision(phone, msg)
        if sup is None or not isinstance(msg.get("data"), dict):
            return
        relay = {"type": "sup_signal", "sup_id": sup.id, "data": msg["data"]}
        if phone.id == sup.supervisor:
            to = {"a": call.caller, "b": call.callee}.get(msg.get("leg"))
            if to is None:
                return
        else:
            to = sup.supervisor
            relay["leg"] = "a" if phone.id == call.caller else "b"
        other = self.sessions.get(to)
        if other is not None and other.ip != session.ip and _is_lan_ipv4(session.ip):
            relay["from_ip"] = session.ip
        self._send(to, relay)

    def _sup_mode(self, session: Session, phone: Phone, msg: dict) -> None:
        sup, call = self._own_supervision(phone, msg)
        if sup is None or phone.id != sup.supervisor or msg.get("mode") not in SUP_MODES:
            return
        target = {"a": call.caller, "b": call.callee}.get(msg.get("leg"), "")
        if msg["mode"] == "whisper" and not target:
            return
        sup.mode, sup.target = msg["mode"], target if msg["mode"] == "whisper" else ""
        target_phone = self.directory.get(sup.target)
        entry = f"{phone.ext} {sup.mode}" + (f" to {target_phone.ext}" if target_phone else "")
        if entry not in call.supervised:
            call.supervised.append(entry)
        log.info("Supervision %s: %s on call %s", sup.id, entry, call.id)
        for party_id in (call.caller, call.callee):
            self._send(party_id, {"type": "sup_update", "sup_id": sup.id, **self._party_view(sup, party_id)})
        session.send({"type": "sup_mode", "sup_id": sup.id, "mode": sup.mode, "leg": msg.get("leg") if sup.target else None})

    def _sup_stop(self, session: Session, phone: Phone, msg: dict) -> None:
        sup, _ = self._own_supervision(phone, msg)
        if sup is not None and phone.id == sup.supervisor:
            self._end_supervision(sup, "stopped")

    def _end_supervision(self, sup: Supervision, reason: str) -> None:
        if self.supervisions.pop(sup.id, None) is None:
            return
        self.sup_by_call.pop(sup.call_id, None)
        self.sup_by_phone.pop(sup.supervisor, None)
        message = {"type": "sup_end", "sup_id": sup.id, "reason": reason}
        self._send(sup.supervisor, message)
        call = self.calls.get(sup.call_id)
        if call is not None:
            self._send(call.caller, message)
            self._send(call.callee, message)
        log.info("Supervision %s: ended (%s)", sup.id, reason)
        self._schedule_directory()

    def _report(self, session: Session, phone: Phone, msg: dict) -> None:
        """A phone's own account of why a call's audio failed; kept for the administrator."""
        text = json.dumps(msg.get("data"), ensure_ascii=False)[:6000]
        log.warning("Audio problem reported by %s (%s) at %s: %s", phone.ext, phone.name, session.ip, text)

    def _ring_timeout(self, call_id: str) -> None:
        call = self.calls.get(call_id)
        if call is not None and call.state == "ringing":
            call.ring_timer = None
            self._end(call, "no_answer")

    def _grace_expired(self, phone_id: str) -> None:
        self._grace.pop(phone_id, None)
        if phone_id in self.sessions:
            return
        call = self.calls.get(self.phone_call.get(phone_id, ""))
        if call is not None:
            self._end(call, "connection_lost", by=phone_id)

    def _cancel_grace(self, phone_id: str) -> None:
        handle = self._grace.pop(phone_id, None)
        if handle:
            handle.cancel()

    def _end(self, call: Call, reason: str, by: str | None = None) -> None:
        supervision = self.supervisions.get(self.sup_by_call.get(call.id, ""))
        if supervision is not None:
            self._end_supervision(supervision, "call_ended")
        if self.calls.pop(call.id, None) is None:
            return
        if call.ring_timer:
            call.ring_timer.cancel()
        for phone_id in (call.caller, call.callee):
            if self.phone_call.get(phone_id) == call.id:
                del self.phone_call[phone_id]
            self._cancel_grace(phone_id)
        caller, callee = self.directory.get(call.caller), self.directory.get(call.callee)
        by_ext = (self.directory.get(by).ext if by and self.directory.get(by) else None)
        message = {"type": "ended", "call_id": call.id, "reason": reason, "by": by_ext}
        self._send(call.caller, message)
        self._send(call.callee, message)

        unanswered = call.answered is None
        if unanswered and caller and callee and reason in ("no_answer", "cancelled", "connection_lost"):
            self._missed(callee, caller, reason)
        duration = round(time.time() - call.answered, 1) if call.answered else 0
        record = {
            "org": self.org.slug,
            "call_id": call.id,
            "started": round(call.started, 3),
            "from": caller.ext if caller else None,
            "from_name": caller.name if caller else None,
            "to": callee.ext if callee else None,
            "to_name": callee.name if callee else None,
            "answered": not unanswered,
            "duration": duration,
            "reason": reason,
        }
        if call.supervised:
            record["supervised"] = call.supervised
        self.history.append(record)
        self.org.record_call(not unanswered, duration)
        cdr.info(json.dumps(record, ensure_ascii=False))
        log.info("Call %s: ended (%s, %ss)", call.id, reason, duration)
        self._schedule_directory()

    def _missed(self, callee: Phone, caller: Phone, reason: str) -> None:
        entry = {
            "id": secrets.token_urlsafe(9),
            "ext": caller.ext,
            "name": caller.name,
            "ts": int(time.time() * 1000),
            "reason": reason,
        }
        session = self.sessions.get(callee.id)
        if session is not None and not session.closed:
            session.send({"type": "missed", "entries": [entry]})
        else:
            self.directory.add_missed(callee, entry)

    # -- helpers -----------------------------------------------------------

    _handlers: dict[str, Callable] = {
        "set_profile": _set_profile,
        "missed_ack": _missed_ack,
        "call": _call,
        "accept": _accept,
        "decline": _decline,
        "hangup": _hangup,
        "signal": _signal,
        "report": _report,
        "transfer": _transfer,
        "supervise": _supervise,
        "sup_signal": _sup_signal,
        "sup_mode": _sup_mode,
        "sup_stop": _sup_stop,
    }

    def _send(self, phone_id: str, message: dict) -> None:
        session = self.sessions.get(phone_id)
        if session is not None:
            session.send(message)

    def _error(self, session: Session, code: str, message: str, fatal: bool = False) -> None:
        session.send({"type": "error", "code": code, "message": message, "fatal": fatal})
        if fatal:
            log.warning("Closing connection from %s: %s (%s)", session.ip, code, message)
            session.close(4000)

    def _ice_servers(self, phone: Phone) -> list[dict]:
        servers = list(self.cfg.ice_servers)
        if self.cfg.turn_urls and self.cfg.turn_secret:
            # The relay's "REST API" scheme: the username carries its own expiry and the
            # password is an HMAC of it, so the relay can check it without asking us.
            username = f"{int(time.time()) + TURN_CREDENTIAL_LIFETIME}:{phone.ext}"
            digest = hmac.new(self.cfg.turn_secret.encode(), username.encode(), hashlib.sha1).digest()
            servers.append(
                {"urls": self.cfg.turn_urls, "username": username, "credential": base64.b64encode(digest).decode()}
            )
        return servers

    @staticmethod
    def _me(phone: Phone) -> dict:
        return {"ext": phone.ext, "name": phone.name, "dnd": phone.dnd}

    @staticmethod
    def _peer(phone: Phone) -> dict:
        return {"ext": phone.ext, "name": phone.name}

    def _call_snapshot(self, phone_id: str) -> dict | None:
        call = self.calls.get(self.phone_call.get(phone_id, ""))
        if call is None:
            return None
        peer = self.directory.get(call.other(phone_id))
        return {
            "call_id": call.id,
            "state": call.state,
            "role": "caller" if call.caller == phone_id else "callee",
            "peer": self._peer(peer) if peer else {"ext": "", "name": "Unknown"},
        }

    def _status(self, phone: Phone) -> str:
        if phone.id not in self.sessions:
            return "offline"
        if phone.id in self.phone_call or phone.id in self.sup_by_phone:
            return "busy"
        return "dnd" if phone.dnd else "online"

    def _directory_message(self) -> dict:
        entries = [
            {"ext": p.ext, "name": p.name, "status": self._status(p)}
            for p in sorted(self.directory.phones.values(), key=lambda p: (len(p.ext), p.ext))
            if not p.disabled
        ]
        return {"type": "directory", "entries": entries}

    def _live_calls(self) -> list[dict]:
        out = []
        for call in self.calls.values():
            caller, callee = self.directory.get(call.caller), self.directory.get(call.callee)
            sup = self.supervisions.get(self.sup_by_call.get(call.id, ""))
            supervisor = self.directory.get(sup.supervisor) if sup else None
            out.append(
                {
                    "call_id": call.id,
                    "state": call.state,
                    "a": self._peer(caller) if caller else {"ext": "", "name": "Unknown"},
                    "b": self._peer(callee) if callee else {"ext": "", "name": "Unknown"},
                    "started": int(call.started * 1000),
                    "answered": int(call.answered * 1000) if call.answered else None,
                    "supervisor": supervisor.ext if supervisor else None,
                    "mode": sup.mode if sup else None,
                }
            )
        return out

    def _calls_message(self) -> dict:
        """Calls in progress, for supervisors."""
        return {"type": "calls", "entries": [c for c in self._live_calls() if c["state"] == "active"]}

    def _schedule_directory(self) -> None:
        if self._broadcast_handle is None:
            self._broadcast_handle = asyncio.get_running_loop().call_later(0.05, self._broadcast_directory)

    def _broadcast_directory(self) -> None:
        self._broadcast_handle = None
        message = self._directory_message()
        calls = None
        for phone_id, session in list(self.sessions.items()):
            session.send(message)
            phone = self.directory.get(phone_id)
            if phone is not None and phone.role == "supervisor":
                calls = calls or self._calls_message()
                session.send(calls)

    # -- administration ----------------------------------------------------
    # Called by the administration console, on the event loop like everything else.

    def admin_snapshot(self) -> dict:
        phones = [
            {
                "ext": p.ext,
                "name": p.name,
                "role": p.role,
                "disabled": p.disabled,
                "status": "disabled" if p.disabled else self._status(p),
                "created": int(p.created * 1000),
                "last_seen": int(p.last_seen * 1000),
                "ip": self.sessions[p.id].ip if p.id in self.sessions else None,
            }
            for p in sorted(self.directory.phones.values(), key=lambda p: (len(p.ext), p.ext))
        ]
        return {"phones": phones, "calls": self._live_calls(), "stats": self.stats()}

    def admin_update_phone(self, ext: str, changes: dict) -> str | None:
        """Apply an administrator's edit. Returns an error text, or None when done."""
        phone = self.directory.find_ext(ext)
        if phone is None:
            return "No phone has that extension."
        name = phone.name
        if "name" in changes:
            name = clean_name(changes["name"])
            if not name:
                return "Enter a name."
        if "role" in changes and changes["role"] not in ROLES:
            return "Unknown role."
        if "ext" in changes:
            new_ext = str(changes["ext"]).strip()
            if not EXT_RE.match(new_ext):
                return "An extension is 2 to 6 digits and cannot start with 0."
            if not self.directory.set_ext(phone, new_ext):
                return f"Extension {new_ext} is already in use."
        phone.name = name
        if "role" in changes and changes["role"] != phone.role:
            phone.role = changes["role"]
            supervision = self.supervisions.get(self.sup_by_phone.get(phone.id, ""))
            if supervision is not None and phone.role != "supervisor":
                self._end_supervision(supervision, "stopped")
        if "dnd" in changes:
            phone.dnd = bool(changes["dnd"])
        if "disabled" in changes:
            phone.disabled = bool(changes["disabled"])
        self.directory.dirty = True
        log.info("Administrator changed phone %s (%s): %s", phone.ext, phone.name, ", ".join(sorted(changes)))
        if phone.disabled:
            self._drop_phone(phone, "disabled", "This phone was switched off by the administrator.")
        else:
            self._send(phone.id, {"type": "profile", "me": self._me(phone)})
            self._send(phone.id, {"type": "role", "role": phone.role})
            if phone.role == "supervisor":
                self._send(phone.id, self._calls_message())
        self._schedule_directory()
        return None

    def admin_remove_phone(self, ext: str) -> bool:
        phone = self.directory.find_ext(ext)
        if phone is None:
            return False
        self._drop_phone(phone, "removed", "This phone was removed by the administrator.")
        self.directory.remove(phone.id)
        log.info("Administrator removed phone %s (%s)", phone.ext, phone.name)
        self._schedule_directory()
        return True

    def admin_hangup(self, call_id: str) -> bool:
        call = self.calls.get(call_id)
        if call is None:
            return False
        self._end(call, "admin")
        return True

    def suspend(self) -> None:
        """Stop serving this organisation: end its calls and disconnect its phones."""
        for call in list(self.calls.values()):
            self._end(call, "admin")
        for session in list(self.connections):
            self._error(session, "suspended", "This phone system is suspended. Contact your provider.", fatal=True)

    def announce_settings(self) -> None:
        """The name or the monitoring notice changed: phones pick it up when they next say hello."""
        for supervision in list(self.supervisions.values()):
            call = self.calls.get(supervision.call_id)
            if call is not None:
                for party_id in (call.caller, call.callee):
                    self._send(party_id, {"type": "sup_update", "sup_id": supervision.id, **self._party_view(supervision, party_id)})

    def _drop_phone(self, phone: Phone, code: str, message: str) -> None:
        supervision = self.supervisions.get(self.sup_by_phone.get(phone.id, ""))
        if supervision is not None:
            self._end_supervision(supervision, "supervisor_left")
        call = self.calls.get(self.phone_call.get(phone.id, ""))
        if call is not None:
            self._end(call, "admin", by=phone.id)
        session = self.sessions.pop(phone.id, None)
        if session is not None:
            session.phone_id = None
            self._error(session, code, message, fatal=True)

    # -- maintenance -------------------------------------------------------

    def stats(self) -> dict:
        return {
            "phones": len(self.directory.phones),
            "online": len(self.sessions),
            "calls": len(self.calls),
            "uptime": int(time.time() - self.started),
        }

    def prune(self) -> None:
        gone = self.directory.prune(self.cfg.prune_days, keep=set(self.sessions))
        for phone in gone:
            log.info("Forgot phone %s (%s): not seen for %d days", phone.ext, phone.name, self.cfg.prune_days)
        if gone:
            self._schedule_directory()

    def shutdown(self) -> None:
        for call in list(self.calls.values()):
            self._end(call, "server_shutdown")
        if self._broadcast_handle:
            self._broadcast_handle.cancel()
            self._broadcast_handle = None
        for handle in self._grace.values():
            handle.cancel()
        self._grace.clear()
        if self._broadcast_handle:
            self._broadcast_handle.cancel()
            self._broadcast_handle = None
        for session in list(self.connections):
            session.close(1001)
        self.directory.save()
