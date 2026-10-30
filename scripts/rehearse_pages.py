#!/usr/bin/env python3
"""Drive the demo's actual PAGES and report the text a viewer would read.

    make rehearse-pages
    make rehearse-pages REHEARSE_CHAIN=mainnet   # the control: the empty state

WHY THIS EXISTS BESIDE `make rehearse`. That one opens by promising to say "what
a VIEWER sees" and then reads JSON envelopes off the `/api/*` proxies. The demo
shows pages, and the two are not the same thing — every page in this terminal is
client-rendered, and `/check`'s Source panel does not exist in the server HTML at
all. So a hydration error, a thrown component, a `className` that no longer
matches the CSS, or a changed SWR key all leave the API green and the screen
blank, and nothing in the repo would notice: `coverage.test.ts` counts `<Ed>`
markers, which answers a different question.

TWO ASSERTIONS PER PAGE, BECAUSE ONE IS NOT ENOUGH. The `<Ed x="…">` labels
render even when the data is empty, so finding a label proves hydration and
nothing more. Each page therefore needs a label (it rendered) AND a
data-bearing string that cannot appear without real rows (it rendered DATA),
plus the absence of `WhyEmpty`'s empty-state sentence.

WAIT ON REAL TEXT, NEVER A TIMEOUT. A fixed sleep passes on a blank page, which
is the failure this is built to catch. This polls `document.body.innerText`
until the anchor appears, the method that worked for the Source panel.

Skips cleanly when Chrome is absent: a gate that breaks on a machine without a
browser is a gate people delete. Deliberately outside `make ci`, for the same
reason `npm run smoke` and `anchors-live` are — it needs a browser and a live
host, and neither belongs in a hermetic job.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

# The cookie name and the terminal URL come from the sibling gate, which parses
# the cookie out of `lib/chainChoice.ts`. A fourth copy of that string is a
# fourth thing to keep in step, and a stale one would silently rehearse the
# wrong chain — the bug `2a7b5d4` fixed.
from rehearse_demo import COOKIE, TERMINAL  # noqa: E402

CHAIN = os.environ.get("REHEARSE_CHAIN", "testnet")
PORT = int(os.environ.get("REHEARSE_CDP_PORT", "9344"))

CHROME_CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
]

#: Per page: the label that proves it RENDERED, and a string that cannot be on
#: screen without real rows, which proves it rendered DATA. The second is the
#: one that matters — `/spend` shows its table labels perfectly well while
#: telling a visitor the press has no route to ask.
#: `data` is a REGEX, not a substring. The first version looked for the slug
#: `acr-fleet` and failed both pages, because they render the business LABEL
#: ("ACR buyer-agent fleet") and never the slug -- a false failure, and exactly
#: the sort this gate exists to prevent rather than produce. A pattern states
#: what is actually being asserted: a FIGURE beside a label, which no empty
#: state can fake.
PAGES = [
    {
        "path": "/spend",
        "name": "/spend",
        "anchor": "Waiting on you",
        "data": r"Decided\s+\d+",
        "data_why": "a decision count beside its label",
    },
    {
        "path": "/traction",
        "name": "/traction",
        "anchor": "Businesses",
        "data": r"testnet\s+[\d.]+",
        "data_why": "a per-chain row with real figures in it",
    },
    {
        "path": "/check?unit=%24%2F1k%20tokens&billed_usdc=0.02&quantity=10",
        "name": "/check",
        "anchor": "Basket",
        "data": r"ACR-INF",
        "data_why": "the basket's index id, which only the press can supply",
    },
]

#: Not failures, but things a viewer sees, each with its remedy. Found by
#: running this: /spend's Cash block reads a bare marker and "the chain could
#: not be reached", because `balance_usdc()` needs the `usdc()` getter that
#: `54df1cc` added and the running image predates. "A cap is permission; this
#: is money" is that block's whole argument, so unknown there undercuts it.
SCREEN_NOTES = [
    ("the chain could not be reached",
     "/spend's Cash block shows the missing marker for HELD. Measured "
     "2026-10-13, and it needs the TESTNET press swapped -- which is a "
     "different Render account this key cannot see. Three-sided: the testnet "
     "press has the PolicyWallets but an image without the usdc() getter "
     "(54df1cc), so liquidity.held_usdc is None; the mainnet probe now HAS "
     "that getter but the wallets are testnet-only, so there is nothing for "
     "it to read; and acr-operator does not exist on the testnet press at all, "
     "so its liquidity block is empty. Swapping the mainnet probe did not fix "
     "this and could not have."),
]

#: What `WhyEmpty` writes when the route is absent or the press is unreachable.
#: Its presence is the empty state, whatever else rendered around it.
EMPTY_MARKERS = ("predates", "did not answer", "has no route to ask")

fails: list[str] = []
notes: list[str] = []


def ok(m: str) -> None:
    print(f"  ✓ {m}")


def bad(m: str) -> None:
    print(f"  ✗ {m}")
    fails.append(m)


def note(m: str) -> None:
    print(f"  · {m}")
    notes.append(m)


def find_chrome() -> str | None:
    """An explicit CHROME_PATH is an OVERRIDE, not a suggestion.

    It used to be the first CANDIDATE, so a wrong path fell through to whatever
    browser happened to be installed — which meant the skip path could not be
    exercised, and someone pointing this at a specific build would silently get
    a different one.
    """
    explicit = os.environ.get("CHROME_PATH", "").strip()
    if explicit:
        return explicit if Path(explicit).exists() else None
    for c in CHROME_CANDIDATES:
        if c and Path(c).exists():
            return c
    return None


class Browser:
    """The smallest CDP client that can answer the question."""

    def __init__(self, chrome: str) -> None:
        self.profile = tempfile.mkdtemp(prefix="acr-rehearse-")
        self.proc = subprocess.Popen(
            [chrome, "--headless=new", f"--remote-debugging-port={PORT}",
             f"--user-data-dir={self.profile}", "--no-first-run",
             "--no-default-browser-check", "--disable-gpu", "about:blank"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.ws = None
        self._n = 0

    def connect(self, timeout: int = 40) -> bool:
        import websockets.sync.client as wsc

        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                tabs = json.load(urllib.request.urlopen(
                    f"http://127.0.0.1:{PORT}/json", timeout=3))
                pages = [t for t in tabs if t.get("type") == "page"]
                if pages:
                    self.ws = wsc.connect(pages[0]["webSocketDebuggerUrl"],
                                          max_size=40_000_000)
                    self.send("Page.enable")
                    self.send("Runtime.enable")
                    self.send("Network.enable")
                    return True
            except Exception:  # noqa: BLE001 — chrome is still starting
                pass
            time.sleep(0.5)
        return False

    def send(self, method: str, params: dict | None = None) -> dict:
        self._n += 1
        self.ws.send(json.dumps({"id": self._n, "method": method,
                                 "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self._n:
                return msg

    def set_chain(self, chain: str) -> None:
        """The pages honour ONLY this cookie — no query parameter, no header."""
        host = TERMINAL.split("://", 1)[-1].split("/")[0]
        self.send("Network.setCookie",
                  {"name": COOKIE, "value": chain, "domain": host, "path": "/"})

    def text_when(self, url: str, want: str, timeout: int = 45) -> str:
        """Navigate, then wait for REAL TEXT rather than for a clock."""
        self.send("Page.navigate", {"url": url})
        deadline, text = time.time() + timeout, ""
        while time.time() < deadline:
            time.sleep(0.75)
            r = self.send("Runtime.evaluate", {
                "expression": "document.body ? document.body.innerText : ''",
                "returnByValue": True})
            text = ((r.get("result") or {}).get("result") or {}).get("value") or ""
            if want in text:
                return text
        return text

    def close(self) -> None:
        try:
            if self.ws:
                self.ws.close()
        finally:
            self.proc.terminate()
            shutil.rmtree(self.profile, ignore_errors=True)


def main() -> int:
    chrome = find_chrome()
    if not chrome:
        want = os.environ.get("CHROME_PATH", "").strip()
        why = f"CHROME_PATH={want} does not exist" if want else "no Chrome or Chromium found"
        print(f"acr page rehearsal: SKIPPED — {why}.")
        print("  Set CHROME_PATH to run it. Skipping rather than failing on "
              "purpose: a gate that breaks without a browser gets deleted.")
        return 0

    print(f"ACR page rehearsal · {time.strftime('%Y-%m-%d %H:%M')}")
    print(f"  terminal {TERMINAL}")
    print(f"  chain    {CHAIN}  (cookie {COOKIE})")

    b = Browser(chrome)
    try:
        if not b.connect():
            print("  ✗ Chrome started but never offered a CDP page target")
            return 1
        b.set_chain(CHAIN)

        for page in PAGES:
            print(f"\n{page['name']}")
            text = b.text_when(f"{TERMINAL}{page['path']}", page["anchor"])

            # ONE RETRY, and only for the unread case. Measured 2026-10-13:
            # /traction came back with 680 chars once, right after a deploy,
            # while its API was `ok` and the page itself was 200 at 120KB --
            # a navigation raced the poll, not a broken page. A gate that cries
            # wolf the day before a demo gets ignored, and "I did not manage to
            # read it" is a different claim from "it did not render". A page
            # showing the EMPTY STATE is NOT retried: that answer is stable and
            # retrying it would just hide it.
            if page["anchor"] not in text and not any(m in text for m in EMPTY_MARKERS):
                note(f"{page['name']}: first read returned {len(text)} chars "
                     "without the anchor; re-reading once before calling it broken")
                text = b.text_when(f"{TERMINAL}{page['path']}", page["anchor"])

            empty = next((m for m in EMPTY_MARKERS if m in text), None)

            if page["anchor"] not in text:
                # ORDER MATTERS HERE. `WhyEmpty` replaces the whole panel, so an
                # empty state takes the anchor label with it — and the first
                # version blamed "a hydration error" for all three pages on the
                # default chain when every one of them had rendered correctly.
                # A gate that misdiagnoses is worse than one that stays quiet.
                if empty:
                    bad(f"{page['name']}: rendered the EMPTY STATE ({empty!r}), "
                        "so the label is not on screen. The page works; the "
                        "data did not arrive.")
                else:
                    bad(f"{page['name']}: {page['anchor']!r} never appeared in "
                        f"{len(text)} chars, and no empty-state sentence either. "
                        "The page did not render — a hydration error, a thrown "
                        "component or a changed SWR key all look like this, and "
                        "the API can be green throughout.")
                continue
            ok(f"rendered: {page['anchor']!r} is on screen")

            if empty:
                bad(f"{page['name']}: showing the EMPTY STATE ({empty!r}) beside "
                    "its labels. The page works; the data did not arrive.")
                continue

            m = re.search(page["data"], text)
            if m:
                ok(f"has data: {m.group(0)!r} — {page['data_why']}")
            else:
                bad(f"{page['name']}: rendered but nothing matches "
                    f"{page['data']!r} ({page['data_why']}). Labels without "
                    "figures is what an empty queue looks like to a viewer.")

            for marker, remedy in SCREEN_NOTES:
                if marker in text:
                    note(remedy)

        print("\n" + "-" * 70)
        if notes:
            for n in notes:
                print(f"  · {n}")
        if fails:
            print(f"✗ {len(fails)} page(s) would not show what the demo needs:")
            for f in fails:
                print(f"    {f}")
            return 1
        print(f"✓ all {len(PAGES)} demo pages rendered WITH data on {CHAIN}")
        return 0
    finally:
        b.close()


if __name__ == "__main__":
    sys.exit(main())
