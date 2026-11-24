"""End-to-end tests of the signalling protocol against a real in-process server."""

import asyncio
import json
import secrets
import tempfile
import unittest
from pathlib import Path

from aiohttp.test_utils import TestClient, TestServer

from lancall.config import Config
from lancall.hub import Hub
from lancall.server import build_app
from lancall.store import Directory


class Phone:
    """A scripted phone speaking the wire protocol."""

    def __init__(self, ws, device_id, secret):
        self.ws, self.device_id, self.secret = ws, device_id, secret
        self.ext = None

    async def send(self, **message):
        await self.ws.send_str(json.dumps(message))

    async def expect(self, kind, timeout=3.0):
        """Next message of the given type; other types are skipped."""
        async with asyncio.timeout(timeout):
            while True:
                frame = await self.ws.receive()
                if frame.type.name != "TEXT":
                    raise AssertionError(f"connection closed while waiting for {kind}: {frame.type.name}")
                message = json.loads(frame.data)
                if message["type"] == kind:
                    return message

    async def silent(self, kind, wait=0.3):
        try:
            message = await self.expect(kind, wait)
        except TimeoutError:
            return
        raise AssertionError(f"unexpected {kind}: {message}")


class SignalingTest(unittest.IsolatedAsyncioTestCase):
    cfg_overrides: dict = {}

    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = Config(data_dir=Path(self.tmp.name), tls=False, ring_timeout=0.6, grace=0.4, **self.cfg_overrides)
        self.directory = Directory(self.cfg.data_dir / "directory.json", self.cfg.ext_start)
        self.hub = Hub(self.cfg, self.directory)
        self.client = TestClient(TestServer(build_app(self.cfg, self.hub)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.tmp.cleanup()

    async def join(self, name, identity=None, **extra):
        device_id, secret = identity or (secrets.token_urlsafe(18), secrets.token_urlsafe(32))
        phone = Phone(await self.client.ws_connect("/ws"), device_id, secret)
        await phone.send(type="hello", v=1, device_id=device_id, secret=secret, name=name, **extra)
        return phone

    async def enrol(self, name, **extra):
        phone = await self.join(name, **extra)
        phone.welcome = await phone.expect("welcome")
        phone.ext = phone.welcome["me"]["ext"]
        return phone

    async def connected_pair(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await alice.send(type="call", to=bob.ext)
        call_id = (await alice.expect("ringing"))["call_id"]
        await bob.expect("incoming")
        await bob.send(type="accept", call_id=call_id)
        await alice.expect("accepted")
        return alice, bob, call_id


class Registration(SignalingTest):
    async def test_extensions_are_assigned_in_order_and_kept(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        self.assertEqual((alice.ext, bob.ext), ("100", "101"))
        await alice.ws.close()
        again = await self.join("Alice again", (alice.device_id, alice.secret))
        welcome = await again.expect("welcome")
        self.assertEqual(welcome["me"], {"ext": "100", "name": "Alice", "dnd": False})

    async def test_wrong_secret_cannot_take_an_extension(self):
        alice = await self.enrol("Alice")
        thief = await self.join("Mallory", (alice.device_id, secrets.token_urlsafe(32)))
        error = await thief.expect("error")
        self.assertEqual((error["code"], error["fatal"]), ("auth_failed", True))
        self.assertIn(alice.device_id, self.hub.sessions)

    async def test_second_tab_replaces_the_first(self):
        first = await self.enrol("Alice")
        second = await self.join("Alice", (first.device_id, first.secret))
        await second.expect("welcome")
        await first.expect("replaced")
        self.assertEqual(len(self.hub.sessions), 1)

    async def test_directory_shows_presence(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await bob.ws.close()
        for _ in range(5):
            entries = {e["ext"]: e["status"] for e in (await alice.expect("directory"))["entries"]}
            if entries.get("101") == "offline":
                break
        self.assertEqual(entries, {"100": "online", "101": "offline"})

    async def test_profile_change_and_taken_extension(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await alice.send(type="set_profile", name="  Front\u0000 Desk  ", ext="200")
        self.assertEqual((await alice.expect("profile"))["me"], {"ext": "200", "name": "Front Desk", "dnd": False})
        await bob.send(type="set_profile", ext="200")
        self.assertEqual((await bob.expect("error"))["code"], "ext_taken")
        await bob.send(type="set_profile", ext="007")
        self.assertEqual((await bob.expect("error"))["code"], "invalid_ext")

    async def test_garbage_is_rejected_without_crashing(self):
        phone = Phone(await self.client.ws_connect("/ws"), "x", "y")
        await phone.send(type="call", to="100")
        self.assertEqual((await phone.expect("error"))["code"], "not_registered")
        await phone.ws.send_str("not json")
        self.assertTrue((await phone.expect("error"))["fatal"])
        self.assertEqual((await self.client.get("/api/health")).status, 200)

    async def test_cross_origin_websocket_is_refused(self):
        response = await self.client.get("/ws", headers={"Origin": "https://evil.example", "Upgrade": "websocket", "Connection": "Upgrade", "Sec-WebSocket-Version": "13", "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="})
        self.assertEqual(response.status, 403)


class Calls(SignalingTest):
    async def test_call_answer_signal_hangup(self):
        alice, bob, call_id = await self.connected_pair()
        await alice.send(type="signal", call_id=call_id, data={"description": {"type": "offer", "sdp": "x"}})
        self.assertEqual((await bob.expect("signal"))["data"]["description"]["type"], "offer")
        await bob.send(type="signal", call_id=call_id, data={"candidate": {"candidate": "c"}})
        self.assertEqual((await alice.expect("signal"))["data"]["candidate"]["candidate"], "c")
        await bob.send(type="hangup", call_id=call_id)
        self.assertEqual((await alice.expect("ended"))["reason"], "hangup")
        self.assertEqual((await bob.expect("ended"))["reason"], "hangup")
        self.assertEqual(self.hub.calls, {})
        self.assertEqual(self.hub.phone_call, {})

    async def test_decline(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await alice.send(type="call", to=bob.ext)
        call_id = (await bob.expect("incoming"))["call_id"]
        await bob.send(type="decline", call_id=call_id)
        self.assertEqual((await alice.expect("ended"))["reason"], "declined")
        await bob.silent("missed")

    async def test_no_answer_times_out_and_leaves_a_missed_call(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await alice.send(type="call", to=bob.ext)
        self.assertEqual((await alice.expect("ended"))["reason"], "no_answer")
        missed = (await bob.expect("missed"))["entries"]
        self.assertEqual((missed[0]["ext"], missed[0]["name"], missed[0]["reason"]), ("100", "Alice", "no_answer"))

    async def test_caller_cancel_leaves_a_missed_call(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await alice.send(type="call", to=bob.ext)
        call_id = (await alice.expect("ringing"))["call_id"]
        await alice.send(type="hangup", call_id=call_id)
        self.assertEqual((await bob.expect("ended"))["reason"], "cancelled")
        self.assertEqual((await bob.expect("missed"))["entries"][0]["reason"], "cancelled")

    async def test_busy_dnd_unknown_and_self(self):
        alice, bob, _ = await self.connected_pair()
        carol = await self.enrol("Carol")
        await carol.send(type="call", to=bob.ext)
        self.assertEqual((await carol.expect("call_failed"))["reason"], "busy")
        self.assertEqual((await bob.expect("missed"))["entries"][0]["name"], "Carol")
        await carol.send(type="call", to="999")
        self.assertEqual((await carol.expect("call_failed"))["reason"], "not_found")
        await carol.send(type="call", to=carol.ext)
        self.assertEqual((await carol.expect("call_failed"))["reason"], "self")

        dave = await self.enrol("Dave")
        await dave.send(type="set_profile", dnd=True)
        await dave.expect("profile")
        await carol.send(type="call", to=dave.ext)
        self.assertEqual((await carol.expect("call_failed"))["reason"], "dnd")
        self.assertEqual((await dave.expect("missed"))["entries"][0]["reason"], "dnd")

    async def test_offline_callee_gets_the_missed_call_on_return(self):
        alice, bob = await self.enrol("Alice"), await self.enrol("Bob")
        await bob.ws.close()
        await asyncio.sleep(0.1)
        await alice.send(type="call", to=bob.ext)
        self.assertEqual((await alice.expect("call_failed"))["reason"], "offline")
        back = await self.join("Bob", (bob.device_id, bob.secret))
        welcome = await back.expect("welcome")
        self.assertEqual([m["name"] for m in welcome["missed"]], ["Alice"])
        await back.send(type="missed_ack")
        await back.send(type="ping")
        await back.expect("pong")
        self.assertEqual(self.directory.get(bob.device_id).missed, [])

    async def test_outsider_cannot_touch_a_call(self):
        alice, bob, call_id = await self.connected_pair()
        eve = await self.enrol("Eve")
        await eve.send(type="signal", call_id=call_id, data={"x": 1})
        await eve.send(type="hangup", call_id=call_id)
        self.assertEqual((await eve.expect("ended"))["reason"], "gone")
        await bob.silent("signal")
        self.assertIn(call_id, self.hub.calls)

    async def test_signalling_blip_does_not_end_the_call(self):
        alice, bob, call_id = await self.connected_pair()
        await alice.ws.close()
        await asyncio.sleep(0.1)
        back = await self.join("Alice", (alice.device_id, alice.secret))
        welcome = await back.expect("welcome")
        self.assertEqual(welcome["call"]["call_id"], call_id)
        self.assertEqual((welcome["call"]["state"], welcome["call"]["role"], welcome["call"]["peer"]["name"]), ("active", "caller", "Bob"))
        await bob.silent("ended", 0.6)  # longer than the grace period
        self.assertIn(call_id, self.hub.calls)

    async def test_call_ends_when_a_phone_never_comes_back(self):
        alice, bob, _ = await self.connected_pair()
        await alice.ws.close()
        self.assertEqual((await bob.expect("ended"))["reason"], "connection_lost")
        self.assertEqual(self.hub.calls, {})


class AccessCode(SignalingTest):
    cfg_overrides = {"pin": "2468"}

    async def test_new_phone_needs_the_code_and_known_phone_does_not(self):
        phone = await self.join("Alice")
        self.assertEqual((await phone.expect("error"))["code"], "pin_required")
        await phone.send(type="hello", v=1, device_id=phone.device_id, secret=phone.secret, name="Alice", pin="0000")
        self.assertEqual((await phone.expect("error"))["code"], "pin_invalid")
        await phone.send(type="hello", v=1, device_id=phone.device_id, secret=phone.secret, name="Alice", pin="2468")
        await phone.expect("welcome")
        await phone.ws.close()
        again = await self.join("Alice", (phone.device_id, phone.secret))
        await again.expect("welcome")

    async def test_guessing_is_locked_out(self):
        phone = await self.join("Mallory")
        await phone.expect("error")
        for guess in range(10):
            await phone.send(type="hello", v=1, device_id=phone.device_id, secret=phone.secret, pin=f"{guess:04d}")
            self.assertEqual((await phone.expect("error"))["code"], "pin_invalid")
        await phone.send(type="hello", v=1, device_id=phone.device_id, secret=phone.secret, pin="2468")
        self.assertEqual((await phone.expect("error"))["code"], "pin_locked")


class Http(SignalingTest):
    async def test_pages_and_headers(self):
        for path, kind in (("/", "text/html"), ("/app.js", "text/javascript"), ("/style.css", "text/css"), ("/manifest.webmanifest", "application/manifest+json"), ("/setup", "text/html")):
            response = await self.client.get(path)
            self.assertEqual(response.status, 200, path)
            self.assertTrue(response.headers["Content-Type"].startswith(kind), (path, response.headers["Content-Type"]))
            self.assertIn("default-src 'self'", response.headers["Content-Security-Policy"])
            self.assertEqual(response.headers["X-Content-Type-Options"], "nosniff")

    async def test_plain_http_helper_serves_the_setup_page(self):
        from lancall.server import build_redirect_app

        helper = TestClient(TestServer(build_redirect_app(self.cfg)))
        await helper.start_server()
        try:
            for path in ("/setup", "/setup.css", "/icon.svg", "/doodle-strolling.svg"):
                self.assertEqual((await helper.get(path)).status, 200, path)
            self.assertEqual((await helper.get("/", allow_redirects=False)).status, 302)
        finally:
            await helper.close()

    async def test_nothing_outside_the_web_folder_is_served(self):
        for path in ("/../lancall/hub.py", "/directory.json", "/.env", "/%2e%2e/requirements.txt"):
            self.assertEqual((await self.client.get(path)).status, 404, path)

    async def test_page_has_no_external_or_inline_code(self):
        html = (self.cfg.web_dir / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertNotIn("https://", html)
        self.assertNotIn("style=", html)


if __name__ == "__main__":
    unittest.main()
