"""Real-browser check: two isolated Chrome profiles place a call through a real server.

Chrome runs headless with a synthetic microphone, so this proves the whole
path (page, signalling, WebRTC audio in both directions) without hardware.

    .venv/Scripts/python tests/e2e_browser.py        (Windows)
    .venv/bin/python tests/e2e_browser.py            (Linux, macOS)

Not part of the unit test run because it needs Chrome or Edge installed.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

import aiohttp

ROOT = Path(__file__).resolve().parent.parent
BROWSERS = [
    os.environ.get("CHROME", ""),
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
    shutil.which("chromium-browser") or "",
]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Browser:
    def __init__(self, ws):
        self.ws = ws
        self.ids = itertools.count(1)
        self.waiting: dict[int, asyncio.Future] = {}
        self.problems: list[str] = []
        self.reader = asyncio.create_task(self._read())

    async def _read(self):
        async for frame in self.ws:
            message = json.loads(frame.data)
            if "id" in message:
                future = self.waiting.pop(message["id"], None)
                if future and not future.done():
                    future.set_result(message)
            elif message.get("method") == "Runtime.exceptionThrown":
                details = message["params"]["exceptionDetails"]
                self.problems.append("exception: " + json.dumps(details.get("exception", details))[:400])
            elif message.get("method") == "Log.entryAdded":
                entry = message["params"]["entry"]
                if entry["level"] == "error" and "favicon" not in entry.get("url", ""):
                    self.problems.append(f"log: {entry['text']} {entry.get('url', '')}")
            elif message.get("method") == "Runtime.consoleAPICalled" and message["params"]["type"] == "error":
                self.problems.append("console.error: " + json.dumps(message["params"]["args"])[:400])

    async def call(self, method, params=None, session=None):
        n = next(self.ids)
        future = asyncio.get_running_loop().create_future()
        self.waiting[n] = future
        payload = {"id": n, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        await self.ws.send_str(json.dumps(payload))
        reply = await asyncio.wait_for(future, 20)
        if "error" in reply:
            raise RuntimeError(f"{method}: {reply['error']}")
        return reply["result"]

    async def new_page(self, url):
        context = (await self.call("Target.createBrowserContext"))["browserContextId"]
        target = (await self.call("Target.createTarget", {"url": "about:blank", "browserContextId": context}))["targetId"]
        session = (await self.call("Target.attachToTarget", {"targetId": target, "flatten": True}))["sessionId"]
        page = Page(self, session)
        for domain in ("Runtime", "Log", "Page"):
            await self.call(f"{domain}.enable", session=session)
        await self.call("Page.navigate", {"url": url}, session)
        return page


class Page:
    def __init__(self, browser, session):
        self.browser, self.session = browser, session

    async def js(self, expression):
        result = await self.browser.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            self.session,
        )
        if "exceptionDetails" in result:
            raise RuntimeError(f"{expression!r} threw: {result['exceptionDetails'].get('exception', {}).get('description')}")
        return result["result"].get("value")

    async def until(self, expression, what, timeout=15.0):
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            value = await self.js(expression)
            if value:
                return value
            if asyncio.get_running_loop().time() > deadline:
                state = await self.js("JSON.stringify({conn: state.conn, phase: state.call && state.call.phase, status: document.getElementById('call-status').textContent, banner: document.getElementById('banner-text').textContent})")
                raise AssertionError(f"timed out waiting for: {what}\n  page state: {state}\n  problems: {self.browser.problems}")
            await asyncio.sleep(0.1)

    async def click(self, selector):
        await self.js(f"document.querySelector({selector!r}).click()")

    async def enrol(self, name):
        # The element check matters: the blank start page is also "complete" for a moment.
        await self.until("document.readyState === 'complete' && !!document.getElementById('view-onboarding') && !document.getElementById('view-onboarding').hidden", "onboarding screen")
        await self.js(f"document.getElementById('onboarding-name').value = {name!r}; document.getElementById('onboarding-form').requestSubmit()")
        return await self.until("state.conn === 'online' && document.getElementById('my-ext').textContent", f"{name} online")

    def phase(self, value):
        return f"state.call !== null && state.call.phase === {value!r} && !document.getElementById('view-call').hidden"


AUDIO_FLOWING = """
(async () => {
  const stats = await state.call.pc.getStats();
  let packets = 0, energy = 0;
  stats.forEach((r) => { if (r.type === 'inbound-rtp' && r.kind === 'audio') { packets = r.packetsReceived || 0; energy = r.totalAudioEnergy || 0; } });
  return packets > 20 && energy > 0;
})()
"""


async def scenario(browser: Browser, base: str) -> None:
    alice, bob = await browser.new_page(base), await browser.new_page(base)
    alice_ext, bob_ext = await alice.enrol("Alice"), await bob.enrol("Bob")
    assert alice_ext != bob_ext, "two browsers must get two extensions"
    print(f"  enrolled: Alice={alice_ext} Bob={bob_ext}")

    # --- a full call, dialled from the directory
    await alice.until("document.querySelectorAll('#directory .call-btn').length === 1", "Bob in Alice's directory")
    await alice.click("#directory .call-btn")
    await alice.until(alice.phase("ringing"), "Alice hears ringback")
    await bob.until(bob.phase("incoming"), "Bob's phone rings")
    assert await bob.js("document.getElementById('call-name').textContent") == "Alice"
    await bob.click("#btn-accept")
    await alice.until(alice.phase("active"), "Alice connected")
    await bob.until(bob.phase("active"), "Bob connected")
    await alice.until(AUDIO_FLOWING, "Alice receives Bob's audio")
    await bob.until(AUDIO_FLOWING, "Bob receives Alice's audio")
    print("  call connected, audio flowing both ways")

    await alice.click("#btn-mute")
    assert await alice.js("state.call.stream.getAudioTracks()[0].enabled") is False
    await alice.click("#btn-mute")
    await bob.click("#btn-hold")
    await alice.until("state.call.peerHeld === true", "Alice sees she is on hold")
    await bob.click("#btn-hold")
    await alice.until("state.call.peerHeld === false", "hold released")
    print("  mute and hold work")

    # --- signalling drops mid-call; the audio must survive and the call resume
    await alice.js("ws.close()")
    await alice.until("state.conn === 'online'", "Alice's signalling reconnects")
    assert await alice.js(alice.phase("active")), "call must survive a signalling reconnect"
    assert await bob.js(bob.phase("active"))
    print("  call survived a signalling reconnect")

    await bob.click("#btn-hangup")
    await alice.until("state.call === null || state.call.phase === 'ended'", "Alice sees the call end")
    await alice.until("state.call === null && !document.getElementById('view-main').hidden", "Alice back on the main screen")
    for page, who in ((alice, "Alice"), (bob, "Bob")):
        entry = await page.js("JSON.stringify(state.history[0])")
        assert json.loads(entry)["answered"] is True, f"{who} history: {entry}"
    print("  hang-up and call history work")

    # --- keypad dial, caller gives up: Bob gets a missed call
    await bob.until("state.call === null", "Bob idle")
    await alice.js(f"document.getElementById('dial-input').value = {bob_ext!r}; document.getElementById('dial-form').requestSubmit()")
    await bob.until(bob.phase("incoming"), "Bob rings again")
    await alice.click("#btn-hangup")
    await bob.until("state.call === null && state.history[0].dir === 'missed'", "Bob has a missed call")
    assert await bob.js("document.getElementById('missed-badge').textContent") == "1"
    print("  missed call recorded")

    # --- decline, busy and unknown numbers
    await alice.until("state.call === null", "Alice idle")
    await alice.js(f"startCall({bob_ext!r})")
    await bob.until(bob.phase("incoming"), "Bob rings a third time")
    await bob.click("#btn-decline")
    await alice.until("state.call !== null && state.call.endText === 'Call declined'", "Alice told the call was declined")
    await alice.click("#btn-done")
    await alice.js("startCall('999')")
    await alice.until("state.call !== null && state.call.endText === 'No phone has that extension'", "unknown extension reported")
    print("  decline and unknown extension handled")

    await alice.click("#btn-done")
    await supervision_and_transfer(browser, base, alice, bob, alice_ext, bob_ext)
    await consoles(browser, base)
    assert not browser.problems, "browser reported problems:\n  " + "\n  ".join(browser.problems)


ADMIN_PASSWORD = "e2e-admin-password"
ADMIN_HEADERS = {"X-Requested-With": "lancall"}


def admin_session() -> aiohttp.ClientSession:
    return aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), headers=ADMIN_HEADERS)


def heard(link: str) -> str:
    """Audio energy received so far on a supervision connection (0 while the far microphone is closed)."""
    return f"""
(async () => {{
  const pc = {link};
  if (!pc) return -1;
  let energy = 0;
  (await pc.getStats()).forEach((r) => {{ if (r.type === 'inbound-rtp' && r.kind === 'audio') energy = r.totalAudioEnergy || 0; }});
  return energy;
}})()
"""


async def supervision_and_transfer(browser, base, alice, bob, alice_ext, bob_ext) -> None:
    boss = await browser.new_page(base)
    boss_ext = await boss.enrol("Boss")
    assert await boss.js("document.getElementById('tab-calls').hidden") is True, "ordinary phones have no Live tab"
    async with admin_session() as http:
        async with http.post(base + "/api/admin/login", json={"password": ADMIN_PASSWORD, "org": "default"}) as reply:
            assert reply.status == 200, await reply.text()
        async with http.patch(f"{base}/api/orgs/default/phones/{boss_ext}", json={"role": "supervisor"}) as reply:
            assert reply.status == 200, await reply.text()
    await boss.until("!document.getElementById('tab-calls').hidden", "Boss gets the Live tab")

    await alice.js(f"startCall({bob_ext!r})")
    await bob.until(bob.phase("incoming"), "Bob rings")
    await bob.click("#btn-accept")
    await alice.until(alice.phase("active"), "Alice connected")
    await bob.until(bob.phase("active"), "Bob connected")
    await boss.until("document.querySelectorAll('#live-calls button').length === 1", "Boss sees the call")
    await boss.click("#live-calls button")
    await boss.until("state.sup !== null && state.sup.id && supConnected(state.sup)", "Boss connected to both phones", 25)

    alice_hears, bob_hears = heard("state.call && state.call.sup && state.call.sup.link.pc"), heard("state.call && state.call.sup && state.call.sup.link.pc")
    for leg in "ab":
        await boss.until(heard(f"state.sup.legs.{leg}.link.pc") + ".then((e) => e > 0.0001)", f"Boss hears leg {leg}")
    await alice.until("document.getElementById('call-sup').textContent.includes('listening')", "Alice is told about the supervisor")
    await asyncio.sleep(1.5)
    assert await alice.js(alice_hears) < 0.00001 and await bob.js(bob_hears) < 0.00001, "monitoring must be silent"
    print("  monitor: supervisor hears both, neither hears the supervisor")

    await boss.click("#sup-whisper-a")
    await alice.until(alice_hears + ".then((e) => e > 0.0001)", "Alice hears the whisper")
    await alice.until("document.getElementById('call-sup').textContent.includes('only to you')", "Alice sees who whispers")
    await asyncio.sleep(1)
    assert await bob.js(bob_hears) < 0.00001, "a whisper must not reach the other person"
    print("  whisper: only the chosen person hears the supervisor")

    await boss.click("#sup-barge")
    await bob.until(bob_hears + ".then((e) => e > 0.0001)", "Bob hears the supervisor after barge")
    await bob.until("document.getElementById('call-sup').textContent.includes('joined the call')", "Bob sees the supervisor joined")
    print("  barge: both hear the supervisor")

    await boss.click("#sup-stop")
    await alice.until("state.call !== null && state.call.sup === null && document.getElementById('call-sup').hidden", "supervision ended for Alice")
    assert await alice.js(alice.phase("active")) and await bob.js(bob.phase("active")), "the call goes on after the supervisor leaves"
    await boss.until("state.sup === null && !document.getElementById('view-main').hidden", "Boss back on the main screen")

    # --- Alice transfers Bob to Boss
    await alice.click("#btn-transfer")
    await alice.js(f"document.getElementById('transfer-ext').value = {boss_ext!r}; document.getElementById('transfer-form').requestSubmit()")
    await boss.until(boss.phase("incoming"), "Boss rings after the transfer")
    assert await boss.js("document.getElementById('call-name').textContent") == "Bob"
    await boss.click("#btn-accept")
    await bob.until(bob.phase("active") + f" && state.call.peer.ext === {boss_ext!r}", "Bob connected to Boss")
    await alice.until("state.call === null", "Alice is free again", 8)
    await bob.click("#btn-hangup")
    await boss.until("state.call === null", "Boss idle", 8)
    print("  transfer works")


async def wait_js(page, expression, what, timeout=15.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while not await page.js(expression):
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out waiting for: {what}\n  problems: {page.browser.problems}")
        await asyncio.sleep(0.1)


async def consoles(browser, base) -> None:
    sign_in = f"document.getElementById('login-password').value = {ADMIN_PASSWORD!r}; document.getElementById('login-form').requestSubmit()"

    console = await browser.new_page(base + "/admin")
    await wait_js(console, "!!document.getElementById('view-login') && !document.getElementById('view-login').hidden", "sign-in form")
    await console.js(sign_in)
    await wait_js(console, "document.querySelectorAll('#phone-rows tr').length === 3", "three phones listed")
    await wait_js(console, "document.querySelectorAll('#history-rows tr').length >= 5", "call history listed")
    assert "Boss" in await console.js("document.getElementById('phone-rows').textContent")
    assert "barge" in await console.js("document.getElementById('history-rows').textContent"), "supervision is in the call record"

    platform = await browser.new_page(base + "/platform")
    await wait_js(platform, "!!document.getElementById('view-login') && !document.getElementById('view-login').hidden", "platform sign-in")
    await platform.js(sign_in)
    await wait_js(platform, "document.querySelectorAll('#org-rows tr').length === 1", "one customer listed")
    await platform.click("#org-add")
    await platform.js(
        "document.getElementById('org-name').value = 'Acme Ltd'; document.getElementById('org-name').dispatchEvent(new Event('input'));"
        "document.getElementById('org-seats').value = '5'; document.getElementById('org-form').requestSubmit()"
    )
    await wait_js(platform, "document.querySelectorAll('#org-rows tr').length === 2", "new customer listed")
    assert "/o/acme-ltd/" in await platform.js("document.getElementById('org-rows').textContent")

    remote = await browser.new_page(base + "/o/acme-ltd/")
    assert await remote.enrol("Remote") == "100", "a new customer starts at its own first extension"
    await wait_js(remote, "state.directory.length === 1", "the customer sees only its own phones")
    print("  admin console, platform console and a second customer work")


async def main() -> int:
    chrome = next((b for b in BROWSERS if b and Path(b).exists()), None)
    if not chrome:
        print("SKIP: no Chrome or Edge found (set CHROME=/path/to/browser)")
        return 0
    port, debug_port = free_port(), free_port()
    work = Path(tempfile.mkdtemp(prefix="lancall-e2e-"))
    server = subprocess.Popen(
        [sys.executable, "-m", "lancall", "--no-tls", "--host", "127.0.0.1", "--port", str(port), "--data-dir", str(work / "data"),
         "--admin-password", ADMIN_PASSWORD],
        cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    browser_process = subprocess.Popen(
        [chrome, "--headless=new", f"--remote-debugging-port={debug_port}", f"--user-data-dir={work / 'profile'}",
         "--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream",
         "--autoplay-policy=no-user-gesture-required", "--no-first-run", "--disable-gpu", "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        async with aiohttp.ClientSession() as http:
            for _ in range(100):
                try:
                    async with http.get(f"http://127.0.0.1:{debug_port}/json/version") as reply:
                        ws_url = (await reply.json())["webSocketDebuggerUrl"]
                    async with http.get(f"http://127.0.0.1:{port}/api/health") as reply:
                        assert reply.status == 200
                    break
                except (aiohttp.ClientError, AssertionError):
                    await asyncio.sleep(0.2)
            else:
                raise RuntimeError("server or browser did not start")
            async with http.ws_connect(ws_url, max_msg_size=0) as ws:
                browser = Browser(ws)
                print(f"Browser test against http://127.0.0.1:{port} using {Path(chrome).name}")
                await scenario(browser, f"http://127.0.0.1:{port}")
                browser.reader.cancel()
        print("PASS")
        return 0
    finally:
        browser_process.terminate()
        server.terminate()
        for process in (browser_process, server):
            try:
                process.wait(5)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except AssertionError as failure:
        print(f"FAIL: {failure}")
        sys.exit(1)
