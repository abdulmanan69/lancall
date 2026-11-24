"""Supervision, transfer, the administration API and separate organisations."""

import json
import secrets
import unittest

from tests.test_signaling import Phone, SignalingTest

PASSWORD = "correct horse battery"


class AdminTest(SignalingTest):
    cfg_overrides = {"admin_password": PASSWORD}

    async def sign_in(self, password=PASSWORD, org=None):
        response = await self.client.post("/api/admin/login", json={"password": password, "org": org}, headers={"X-Requested-With": "lancall"})
        self.client.session.cookie_jar.clear()  # each caller carries its own cookie, like separate browsers
        if response.status != 200:
            return response.status
        return {"Cookie": f"lancall_admin={response.cookies['lancall_admin'].value}", "X-Requested-With": "lancall"}

    async def api(self, auth, method, path, body=None, expect=200):
        response = await self.client.request(method, path, json=body, headers=auth)
        self.assertEqual(response.status, expect, await response.text())
        return await response.json() if response.content_type == "application/json" else await response.text()

    async def supervisor(self, name="Boss"):
        boss = await self.enrol(name)
        self.assertIsNone(self.hub.admin_update_phone(boss.ext, {"role": "supervisor"}))
        self.assertEqual((await boss.expect("role"))["role"], "supervisor")
        return boss


class Supervision(AdminTest):
    async def test_an_ordinary_phone_cannot_monitor(self):
        _, _, call_id = await self.connected_pair()
        carol = await self.enrol("Carol")
        await carol.send(type="supervise", call_id=call_id)
        self.assertEqual((await carol.expect("sup_failed"))["reason"], "forbidden")
        self.assertEqual(self.hub.supervisions, {})

    async def test_monitor_whisper_barge_and_the_record(self):
        alice, bob, call_id = await self.connected_pair()
        boss = await self.supervisor()
        for _ in range(5):
            calls = (await boss.expect("calls"))["entries"]
            if calls:
                break
        self.assertEqual([(c["a"]["ext"], c["b"]["ext"]) for c in calls], [(alice.ext, bob.ext)])

        await boss.send(type="supervise", call_id=call_id)
        started = await boss.expect("sup_started")
        sup_id = started["sup_id"]
        self.assertEqual([(p["leg"], p["ext"]) for p in started["parties"]], [("a", alice.ext), ("b", bob.ext)])
        for party in (alice, bob):
            joined = await party.expect("sup_join")
            self.assertEqual((joined["sup_id"], joined["state"], joined["by"]["name"]), (sup_id, "monitor", "Boss"))

        # audio negotiation is relayed leg by leg, and never to the wrong phone
        await boss.send(type="sup_signal", sup_id=sup_id, leg="a", data={"description": {"type": "offer", "sdp": "x"}})
        self.assertEqual((await alice.expect("sup_signal"))["data"]["description"]["type"], "offer")
        await bob.silent("sup_signal")
        await bob.send(type="sup_signal", sup_id=sup_id, data={"candidate": {"candidate": "c"}})
        self.assertEqual((await boss.expect("sup_signal"))["leg"], "b")

        await boss.send(type="sup_mode", sup_id=sup_id, mode="whisper", leg="a")
        self.assertEqual((await alice.expect("sup_update"))["state"], "whisper")
        self.assertEqual((await bob.expect("sup_update"))["state"], "monitor")
        await boss.send(type="sup_mode", sup_id=sup_id, mode="barge")
        self.assertEqual((await alice.expect("sup_update"))["state"], "barge")
        self.assertEqual((await bob.expect("sup_update"))["state"], "barge")

        # a supervisor who is listening is busy for callers
        carol = await self.enrol("Carol")
        await carol.send(type="call", to=boss.ext)
        self.assertEqual((await carol.expect("call_failed"))["reason"], "busy")

        await alice.send(type="hangup", call_id=call_id)
        self.assertEqual((await boss.expect("sup_end"))["reason"], "call_ended")
        self.assertEqual(self.hub.supervisions, {})
        self.assertEqual(self.hub.history[-1]["supervised"], [f"{boss.ext} monitor", f"{boss.ext} whisper to {alice.ext}", f"{boss.ext} barge"])

    async def test_notice_can_be_turned_off_but_a_whisper_is_always_shown(self):
        self.hub.org.monitor_notice = False
        alice, bob, call_id = await self.connected_pair()
        boss = await self.supervisor()
        await boss.send(type="supervise", call_id=call_id)
        sup_id = (await boss.expect("sup_started"))["sup_id"]
        joined = await alice.expect("sup_join")
        self.assertEqual(joined["state"], "hidden")
        self.assertNotIn("by", joined)
        await boss.send(type="sup_mode", sup_id=sup_id, mode="whisper", leg="b")
        self.assertEqual((await bob.expect("sup_update"))["state"], "whisper")
        self.assertEqual((await alice.expect("sup_update"))["state"], "hidden")

    async def test_one_supervisor_per_call_and_leaving_ends_it(self):
        alice, _, call_id = await self.connected_pair()
        boss, other = await self.supervisor("Boss"), await self.supervisor("Other")
        await boss.send(type="supervise", call_id=call_id)
        sup_id = (await boss.expect("sup_started"))["sup_id"]
        await other.send(type="supervise", call_id=call_id)
        self.assertEqual((await other.expect("sup_failed"))["reason"], "taken")
        await boss.ws.close()
        self.assertEqual((await alice.expect("sup_end"))["sup_id"], sup_id)
        self.assertEqual(self.hub.supervisions, {})
        self.assertIn(call_id, self.hub.calls)  # the call itself goes on

    async def test_an_outsider_cannot_inject_supervision_signals(self):
        alice, _, call_id = await self.connected_pair()
        boss = await self.supervisor()
        await boss.send(type="supervise", call_id=call_id)
        sup_id = (await boss.expect("sup_started"))["sup_id"]
        mallory = await self.enrol("Mallory")
        await mallory.send(type="sup_signal", sup_id=sup_id, leg="a", data={"x": 1})
        await mallory.send(type="sup_mode", sup_id=sup_id, mode="barge")
        await mallory.send(type="sup_stop", sup_id=sup_id)
        await alice.expect("sup_join")
        await alice.silent("sup_signal")
        self.assertEqual(self.hub.supervisions[sup_id].mode, "monitor")


class Transfer(AdminTest):
    async def test_blind_transfer(self):
        alice, bob, call_id = await self.connected_pair()
        carol = await self.enrol("Carol")
        await bob.send(type="transfer", call_id=call_id, to=carol.ext)
        self.assertEqual((await alice.expect("transfer"))["to"], {"ext": carol.ext, "name": "Carol"})
        self.assertEqual((await alice.expect("ended"))["reason"], "transferred")
        self.assertEqual((await bob.expect("ended"))["reason"], "transferred")
        await alice.send(type="call", to=carol.ext)  # what the phone app does next
        await alice.expect("ringing")
        self.assertEqual((await carol.expect("incoming"))["from"]["name"], "Alice")

    async def test_transfer_to_a_phone_that_cannot_take_it(self):
        alice, bob, call_id = await self.connected_pair()
        await bob.send(type="transfer", call_id=call_id, to="999")
        self.assertEqual((await bob.expect("transfer_failed"))["reason"], "not_found")
        await bob.send(type="transfer", call_id=call_id, to=alice.ext)
        self.assertEqual((await bob.expect("transfer_failed"))["reason"], "self")
        self.assertIn(call_id, self.hub.calls)


class Console(AdminTest):
    async def test_sign_in_is_required_and_guesses_are_limited(self):
        self.assertEqual((await self.client.get("/api/orgs/default")).status, 401)
        self.assertEqual((await self.client.get("/api/platform")).status, 401)
        for _ in range(8):
            self.assertEqual(await self.sign_in("wrong"), 401)
        self.assertEqual(await self.sign_in(PASSWORD), 429)

    async def test_changes_need_the_page_header(self):
        auth = await self.sign_in()
        response = await self.client.patch("/api/orgs/default", json={"name": "X"}, headers={"Cookie": auth["Cookie"]})
        self.assertEqual(response.status, 403)
        response = await self.client.patch("/api/orgs/default", json={"name": "X"}, headers={**auth, "Origin": "https://evil.example"})
        self.assertEqual(response.status, 403)

    async def test_manage_phones_and_calls(self):
        auth = await self.sign_in()
        alice, bob, call_id = await self.connected_pair()
        overview = await self.api(auth, "GET", "/api/orgs/default")
        self.assertEqual([(p["ext"], p["status"]) for p in overview["phones"]], [("100", "busy"), ("101", "busy")])
        self.assertEqual(overview["calls"][0]["call_id"], call_id)

        await self.api(auth, "POST", f"/api/orgs/default/calls/{call_id}/hangup")
        self.assertEqual((await alice.expect("ended"))["reason"], "admin")
        history = await self.api(auth, "GET", "/api/orgs/default/history")
        self.assertEqual((history["calls"][0]["from"], history["calls"][0]["reason"]), ("100", "admin"))
        self.assertIn("100,Alice,101,Bob,yes", await self.api(auth, "GET", "/api/orgs/default/history.csv"))

        await self.api(auth, "PATCH", "/api/orgs/default/phones/100", {"name": "Front Desk", "ext": "200", "role": "supervisor"})
        self.assertEqual((await alice.expect("profile"))["me"], {"ext": "200", "name": "Front Desk", "dnd": False})
        self.assertEqual((await alice.expect("role"))["role"], "supervisor")
        await self.api(auth, "PATCH", "/api/orgs/default/phones/200", {"ext": "101"}, expect=400)

        await self.api(auth, "PATCH", "/api/orgs/default/phones/101", {"disabled": True})
        self.assertEqual((await bob.expect("error"))["code"], "disabled")
        again = await self.join("Bob", (bob.device_id, bob.secret))
        self.assertEqual((await again.expect("error"))["code"], "disabled")

        await self.api(auth, "DELETE", "/api/orgs/default/phones/200")
        self.assertEqual((await alice.expect("error"))["code"], "removed")
        self.assertIsNone(self.hub.directory.find_ext("200"))

    async def test_settings_take_effect(self):
        auth = await self.sign_in()
        await self.api(auth, "PATCH", "/api/orgs/default", {"name": "Acme Phones", "pin": "4321", "monitor_notice": False})
        phone = await self.join("Alice")
        self.assertEqual((await phone.expect("error"))["code"], "pin_required")
        await phone.send(type="hello", v=1, device_id=phone.device_id, secret=phone.secret, name="Alice", pin="4321")
        self.assertEqual((await phone.expect("welcome"))["server"]["name"], "Acme Phones")
        self.assertFalse(self.hub.org.monitor_notice)


class Organisations(AdminTest):
    async def join_org(self, slug, name, **extra):
        device_id, secret = secrets.token_urlsafe(18), secrets.token_urlsafe(32)
        phone = Phone(await self.client.ws_connect(f"/o/{slug}/ws"), device_id, secret)
        await phone.send(type="hello", v=1, device_id=device_id, secret=secret, name=name, **extra)
        return phone

    async def test_customers_are_separate(self):
        auth = await self.sign_in()
        await self.api(auth, "POST", "/api/platform/orgs", {"slug": "acme", "name": "Acme", "seats": 1, "pin": "7777", "admin_password": "acme-admin-pw"}, expect=201)
        await self.api(auth, "POST", "/api/platform/orgs", {"slug": "acme", "name": "Again"}, expect=400)
        await self.api(auth, "POST", "/api/platform/orgs", {"slug": "Bad Slug", "name": "x"}, expect=400)

        local = await self.enrol("Local")
        outsider = await self.join_org("acme", "Remote")
        self.assertEqual((await outsider.expect("error"))["code"], "pin_required")
        remote = await self.join_org("acme", "Remote", pin="7777")
        welcome = await remote.expect("welcome")
        self.assertEqual((welcome["me"]["ext"], welcome["server"]["name"]), ("100", "Acme"))
        self.assertEqual(local.ext, "100")  # same number, different phone system
        self.assertEqual([e["name"] for e in (await remote.expect("directory"))["entries"]], ["Remote"])
        await local.send(type="call", to="100")
        self.assertEqual((await local.expect("call_failed"))["reason"], "self")

        full = await self.join_org("acme", "Second", pin="7777")
        self.assertEqual((await full.expect("error"))["code"], "directory_full")

        for path, status in (("/o/acme/", 200), ("/o/acme/app.js", 200), ("/o/acme/admin", 200), ("/o/nobody/", 404), ("/o/nobody/app.js", 404)):
            self.assertEqual((await self.client.get(path)).status, status, path)

        # the customer's own administrator sees only their organisation
        theirs = await self.sign_in("acme-admin-pw", org="acme")
        self.assertEqual((await self.api(theirs, "GET", "/api/orgs/acme"))["phones"][0]["name"], "Remote")
        await self.api(theirs, "GET", "/api/orgs/default", expect=403)
        await self.api(theirs, "GET", "/api/platform", expect=403)
        await self.api(theirs, "PATCH", "/api/platform/orgs/acme", {"seats": 500}, expect=403)
        self.assertEqual(await self.sign_in("acme-admin-pw", org="default"), 401)

        listing = await self.api(auth, "GET", "/api/platform")
        self.assertEqual({o["slug"]: o["phones"] for o in listing["orgs"]}, {"default": 1, "acme": 1})

        await self.api(auth, "PATCH", "/api/platform/orgs/acme", {"suspended": True})
        self.assertEqual((await remote.expect("error"))["code"], "suspended")
        blocked = await self.join_org("acme", "Remote", pin="7777")
        self.assertEqual((await blocked.expect("error"))["code"], "suspended")

        await self.api(auth, "DELETE", "/api/platform/orgs/acme")
        self.assertEqual((await self.client.get("/o/acme/")).status, 404)
        await self.api(auth, "DELETE", "/api/platform/orgs/default", expect=400)

    async def test_organisations_survive_a_restart(self):
        from lancall.server import PLATFORM_KEY
        from lancall.tenants import Platform

        auth = await self.sign_in()
        await self.api(auth, "POST", "/api/platform/orgs", {"slug": "acme", "name": "Acme", "seats": 5}, expect=201)
        remote = await self.join_org("acme", "Remote")
        await remote.expect("welcome")
        self.client.app[PLATFORM_KEY].save()

        again = Platform(self.cfg)
        self.assertEqual((again.hub("acme").name, again.hub("acme").org.seats), ("Acme", 5))
        self.assertEqual([p.name for p in again.hub("acme").directory.phones.values()], ["Remote"])
        self.assertEqual(json.loads((self.cfg.data_dir / "orgs.json").read_text())["version"], 1)


if __name__ == "__main__":
    unittest.main()
