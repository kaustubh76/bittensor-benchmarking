#!/usr/bin/env python3
"""Walk the demo's own path against the LIVE hosts, and say what a viewer sees.

    make rehearse                  # the testnet path, which is the demo
    make rehearse REHEARSE_STRICT=0 # report instead of gating

WHY THIS IS NOT ANOTHER TEST SUITE. `pytest` tells us the code is right;
`verify-drift` tells us the deployment serves what we built. Neither answers the
question that matters on the 15th — *does the thing we are about to show in
front of people actually work right now* — because every suite in this repo runs
against fixtures or against a route set, and a demo is a sequence of screens
with real numbers on them.

So this asks the live hosts for the six things the demo puts on screen, and
prints the VALUE rather than a tick. A step that answers 200 with an empty
basket is a failure here, because "it returned 200" is not what a viewer sees.

STRICT BY DEFAULT, which is the opposite of a report and deliberate: at
rehearsal time "I could not read the host" is the single most important failure,
because a sleeping free-tier press is exactly what will happen on stage. The
same correction `VERIFY_DRIFT_STRICT=1` and `--check-live` exist to make.

IT SPENDS NOTHING AND SIGNS NOTHING. Every chain call is a view, every HTTP call
is a GET, and the rung classification is arithmetic against the live
`perTxLimit` rather than a transaction. Running it twenty times before the demo
costs nothing but time.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

#: The demo is TESTNET ONLY (decided 2026-10-13). The mainnet block below is
#: checked but not demoed, because the demo makes a claim about it.
TESTNET_API = os.environ.get("REHEARSE_API", "https://acr-api-1fto.onrender.com")
TERMINAL = os.environ.get("REHEARSE_TERMINAL", "https://arc-compute-rate.vercel.app")
TESTNET_RPC = "https://rpc.testnet.arc.io"
MAINNET_RPC = "https://rpc.mainnet.arc.io"

POLICY_WALLET = "0xa67F188746f479A9d93D112BeeB7aadB90bE834b"
CATEGORY = "machine-services"
PRESS_WALLET = "0x1547B0d3C334F264D28B087566B512AB5CD85379"

#: The seven mainnet contracts the demo will say are deployed. A claim a judge
#: can test, so it is tested here. Read off `acr-probe-free`'s own env and
#: confirmed on chain 2026-10-12.
MAINNET_CONTRACTS = {
    "oracle": "0xAfe8a941957EbD3C759fEA5365fe4B75f9626D07",
    "oracle_v2": "0xF77E763ef710096c0b9F02Ea16F76e68E0312f81",
    "registry": "0x13CF727d1D25283d58Efe7647dCf30e9cE255096",
    "futures": "0xdb5D7dbB67aD8EC8517982E95c89906abFBC6d3b",
    "attestor": "0xB9508caD4A5C367C3a62B6767C4837a607EaA490",
    "receipt_mirror": "0x1a5390E59d4ce0a386Aa071365A932Dc34d35bE3",
    "humanid_mirror": "0x7134622a01Add334eA306dDa5A5877b418a940B5",
}

STRICT = os.environ.get("REHEARSE_STRICT", "1").strip().lower() not in ("0", "false", "off", "")


def chain_cookie_name() -> str:
    """The cookie the terminal honours, read from the file that declares it.

    `lib/chainChoice.ts` exports `CHAIN_COOKIE`, and the pages honour ONLY that
    cookie — no query parameter, no header. Parsed rather than retyped for the
    reason `printAge.test.ts` reads its thresholds out of `par.py`: a third copy
    of this string is a third thing to keep in step, and the failure if it drifts
    is this gate silently rehearsing the wrong chain. Which is the bug this
    change exists to fix, so hardcoding it here would be a poor joke.
    """
    src = (ROOT / "apps" / "terminal" / "lib" / "chainChoice.ts").read_text(encoding="utf-8")
    import re
    m = re.search(r'CHAIN_COOKIE\s*=\s*"([^"]+)"', src)
    if not m:
        raise SystemExit(
            "cannot find CHAIN_COOKIE in lib/chainChoice.ts — the terminal honours "
            "only that cookie, so this gate cannot choose a chain without it"
        )
    return m.group(1)


COOKIE = chain_cookie_name()

#: Failures that stop the DEMO, and failures a judge sees on the default
#: chain. Kept apart because the first version lumped them and announced
#: "3 steps would not survive the demo" about three mainnet routes the demo
#: never opens -- the same conflation this gate was already fixed for once.
fails: list[str] = []
judge_fails: list[str] = []
notes: list[str] = []


def step(n: str) -> None:
    print(f"\n\033[1m{n}\033[0m" if sys.stdout.isatty() else f"\n{n}")


def ok(msg: str) -> None:
    print(f"  ✓ {msg}")


def bad(msg: str) -> None:
    print(f"  ✗ {msg}")
    fails.append(msg)


def note(msg: str) -> None:
    # DEDUPED. Both chains hit the registry, so the same "acr-operator is not in
    # the live registry" fired twice and the summary listed it twice. A repeated
    # note is noise, and noise is how a reader learns to skip the notes.
    if msg in notes:
        return
    print(f"  · {msg}")
    notes.append(msg)


def get(url: str, timeout: int = 120, chain: str | None = None) -> dict | None:
    """A GET that distinguishes "said no" from "could not be asked".

    `chain` sets the terminal's chain cookie. Without it every terminal read
    lands on the DEFAULT chain, which is how this gate came to report the demo
    as broken while the demo's own network was green.
    """
    headers = {"User-Agent": "acr-rehearse/1"}
    if chain:
        headers["Cookie"] = f"{COOKIE}={chain}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 — our own hosts
            return json.load(r)
    except urllib.error.HTTPError as exc:
        # THE TERMINAL'S PROXY ANSWERS 5xx WITH A MEANINGFUL ENVELOPE. A 503
        # carrying {"upstream":"absent",...} is the route telling us exactly
        # what happened, and the first version of this gate threw it away and
        # printed "-> HTTP 503" instead -- so the actionable message below, the
        # one naming the undeployed commits, could never fire. Read the body.
        try:
            body = json.loads(exc.read().decode("utf-8"))
            if isinstance(body, dict) and ("upstream" in body or "live" in body):
                return body
        except Exception:  # noqa: BLE001 -- a non-JSON 5xx is a real failure
            pass
        bad(f"{url} -> HTTP {exc.code} with no envelope")
    except Exception as exc:  # noqa: BLE001
        bad(f"{url} unreachable ({type(exc).__name__}: {exc})")
    return None


# --- 1. the press the demo talks to ----------------------------------------

def rehearse_press() -> dict | None:
    step("1. The testnet press, which is what the demo talks to")
    spec = get(f"{TESTNET_API}/openapi.json")
    if spec is None:
        return None
    paths = spec.get("paths") or {}
    operator = sorted(k for k in paths if k.startswith("/operator"))
    if len(operator) == 5 and "/par" in paths:
        ok(f"{len(paths)} routes, all five /operator/* and /par")
    else:
        bad(f"{len(paths)} routes but {len(operator)} /operator/* and /par="
            f"{'/par' in paths} — the demo's own pages cannot be served")

    health = get(f"{TESTNET_API}/health")
    if health:
        started = health.get("process_started_at")
        up = f"{(time.time() - started) / 60:.0f} min" if started else "unknown"
        ok(f"health ok · chain {health.get('chain_id')} · signer "
           f"{health.get('signer')} · up {up}")
        if str(health.get("chain_id")) != "5042002":
            bad(f"this is chain {health.get('chain_id')}, not testnet 5042002")
    return health


# --- 2. an invoice, priced ---------------------------------------------------

def rehearse_par() -> None:
    step("2. An invoice priced against published prices (the opening screen)")
    url = (f"{TESTNET_API}/par?unit=%24%2F1k%20tokens"
           f"&billed_usdc=0.02&quantity=10")
    d = get(url)
    if d is None:
        return
    basket = d.get("basket") or {}
    against = d.get("benchmarked_against")
    rate = d.get("over_rate_bp")
    sellers = (d.get("par") or {}).get("sellers")

    if against == "market":
        ok(f"benchmarked against the MARKET · {sellers} sellers · "
           f"{rate:.0f} bp over" if isinstance(rate, (int, float))
           else f"benchmarked against the market · {sellers} sellers")
    else:
        bad(f"benchmarked_against is {against!r}, not 'market' — this prices a "
            "stranger's bill against our own sellers, and the number on screen "
            "would be the 20x-1250x scale gap rather than an overcharge")

    status = basket.get("status")
    age_s = basket.get("age_s")
    rows, req = basket.get("rows"), basket.get("requested")
    if status == "ok":
        ok(f"basket {status} · {rows} of {req} prices")
    elif status == "aging":
        note(f"basket is AGING ({rows} of {req}) — it still prices, and a "
             "viewer will see the amber chip. Re-anchor before the demo")
    else:
        bad(f"basket status {status!r}: the comparison has fallen back to our "
            "own sellers")
    if age_s is None:
        note(f"{TESTNET_API} predates `basket.age_s`, so /check shows a bare date "
             "rather than the clock. The mainnet probe was swapped to "
             "2026-10-13-0541 and has it; the TESTNET press is a different "
             "Render account that this RENDER_API_KEY cannot see (it lists one "
             "service, and the testnet id 404s), so that swap is the owner's.")
    else:
        ok(f"basket states its own age: {age_s / 86_400:.1f}d of "
           f"{basket.get('max_age_s', 0) / 86_400:.0f}d")


# --- 3 + 4. the wallet, and which rung an amount takes ----------------------

def rehearse_wallet() -> None:
    step("3. The PolicyWallet state a viewer will see")
    try:
        from acr_oracle_client.policy import PolicyClient
    except Exception as exc:  # noqa: BLE001
        bad(f"cannot import PolicyClient ({exc})")
        return

    os.environ.setdefault("ACR_ARC_RPC_URL", TESTNET_RPC)
    client = PolicyClient(wallet_address=POLICY_WALLET)
    b = client.budget(CATEGORY)
    if not b:
        bad(f"no budget for {CATEGORY!r} on {POLICY_WALLET} — `spend` would "
            "revert 'no such budget' and the demo stops here")
        return

    held = client.balance_usdc()
    paused = client.paused()
    ok(f"cap {b['cap_usdc']:.6f} · spent {b['spent_usdc']:.6f} · remaining "
       f"{b['remaining_usdc']:.6f} · perTx {b['per_tx_limit_usdc']:.6f}")
    if held is None:
        bad("the wallet's USDC balance could not be read")
    else:
        ok(f"wallet holds {held:.6f} USDC")
        if held < b["remaining_usdc"]:
            bad(f"permission ({b['remaining_usdc']:.6f}) exceeds money "
                f"({held:.6f}) — a payment inside budget would fail on transfer")
    if paused:
        bad("the wallet is PAUSED: every rung refuses, including the owner's")
    elif paused is None:
        bad("could not read `paused`")
    else:
        ok("not paused")

    step("4. Which rung an amount takes, decided by the live perTxLimit")
    limit = b["per_tx_limit_usdc"]
    # The contract's own rule: `require(amount < perTxLimit)` on `spend`, so the
    # limit itself escalates. Arithmetic, not a transaction.
    below, at = round(limit - 0.000001, 6), limit
    ok(f"{below:.6f} -> spend()         (agent alone, {below:.6f} < {limit:.6f})")
    ok(f"{at:.6f} -> ESCALATES        (spendApproved or spendAsOwner)")
    if b["remaining_usdc"] < at:
        bad(f"remaining {b['remaining_usdc']:.6f} is below perTxLimit "
            f"{at:.6f}, so the ESCALATION leg cannot be demonstrated — the cap "
            "would refuse it before the threshold was reached")
    else:
        ok(f"remaining {b['remaining_usdc']:.6f} covers an escalated payment")

    nonce = client.approval_nonce()
    if nonce is None:
        note("approvalNonce unreadable; `spendApproved` needs it to build the digest")
    else:
        ok(f"approvalNonce {nonce} (global and sequential — one approval at a time)")


# --- 5. the live terminal, on the chain the demo uses -----------------------

#: The two undeployed commits behind the default chain's failure. Named in the
#: message because a gate that says what to DO beats one that says what is
#: wrong, and here the whole answer is one `vercel --prod`.
UNDEPLOYED = (
    "9c7033d (the ladder stops on a healthy host's 404 instead of walking to "
    "acr-probe-free) and 4a262c5 (the envelope says 'the press did not answer' "
    "about a press that answered with a 404)"
)

TERMINAL_ROUTES = (
    ("/api/operator/businesses", "/spend  (registry)"),
    ("/api/operator/traction", "/traction"),
    ("/api/par?unit=%24%2F1k%20tokens&billed_usdc=0.02&quantity=10", "/check  (price a bill)"),
)


def first_candidate_health() -> None:
    """Why the default chain's result is whatever it is.

    THIS IS THE FIELD THAT EXPLAINS EVERYTHING. `acr-api-mainnet` is the first
    mainnet candidate and it answers 404 for the operator routes, so the
    deployed ladder stops there WHEN IT IS HEALTHY and walks past it to
    `acr-probe-free` when it is not. Observed on 2026-10-13: restarts at
    18:49:55Z and again at 19:57:55Z, a /health taking 50.4s against the
    probe's 0.45s, and the default chain flipping from broken to working
    between two runs of this gate an hour apart.

    So a bare red or green on the default chain is a mystery without this, and
    the uptime plus the latency is the whole explanation. Reported, never
    asserted on: a flapping press is a fact about today, not a failure of the
    demo.
    """
    import urllib.request as _u

    for host, label in (("acr-api-mainnet", "first candidate"),
                        ("acr-probe-free", "second candidate")):
        url = f"https://{host}.onrender.com/health"
        t0 = time.monotonic()
        try:
            req = _u.Request(url, headers={"User-Agent": "acr-rehearse/1"})
            with _u.urlopen(req, timeout=120) as r:  # noqa: S310 — our own host
                d = json.load(r)
            lat = time.monotonic() - t0
            started = d.get("process_started_at")
            up = (time.time() - started) / 60 if started else None
            flag = ""
            if up is not None and up < 15:
                flag = "  <- RESTARTED RECENTLY, so the ladder's choice may move"
            if lat > 10:
                flag += "  <- SLOW, which is what makes the ladder walk past it"
            print(f"     {label:<17} {host}: up {up:.0f} min, /health in "
                  f"{lat:.1f}s, signer {d.get('signer')!r}{flag}")
        except Exception as exc:  # noqa: BLE001
            print(f"     {label:<17} {host}: unreadable ({type(exc).__name__}) "
                  "<- the ladder will walk past it, which makes the page WORK")


def rehearse_terminal(chain: str, *, is_demo: bool) -> None:
    """The terminal's own pages, on ONE chain.

    THE BUG THIS SIGNATURE FIXES. This used to take no chain, so every read
    landed on the terminal's DEFAULT chain — mainnet. The demo is testnet only,
    so the gate reported the demo as broken on its first run while the demo's
    own network was entirely green. A gate measuring an adjacent thing is worse
    than no gate before a demo: it either sends you chasing a non-problem or
    teaches you to ignore it.
    """
    which = "THE DEMO" if is_demo else "a visitor landing on the default chain"
    step(f"5{'a' if is_demo else 'b'}. The live terminal on {chain} \u2014 {which}")

    if not is_demo:
        # The default chain's verdict is decided by the first candidate's
        # health, so print it beside the verdict rather than leaving a reader
        # to wonder why the same gate said the opposite an hour ago.
        first_candidate_health()

    health = get(f"{TERMINAL}/api/health", chain=chain)
    if health:
        d = health.get("data") or {}
        # `signer` plus `chain_id` names the host: `local` is acr-api-mainnet
        # (it holds a poster key), `none` is acr-probe-free. This is how the
        # 18:49 restart that broke the default chain was traced.
        # THREE presses, three signer values — the testnet one uses Circle
        # custody and reports `circle`, which a two-entry map called "unknown".
        # Keyed on (chain_id, signer) because `signer` alone cannot separate a
        # mainnet press from a testnet one.
        host = {
            ("5042", "local"): "acr-api-mainnet (44 routes, holds a poster key)",
            ("5042", "none"): "acr-probe-free (50 routes, no signer)",
            ("5042002", "circle"): "acr-api-1fto (50 routes, Circle custody)",
        }.get((str(d.get("chain_id")), str(d.get("signer"))), "a host this gate does not know")
        print(f"     answered by: {host} \u00b7 chain {d.get('chain_id')} "
              f"\u00b7 signer {d.get('signer')!r}")

    for path, label in TERMINAL_ROUTES:
        d = get(f"{TERMINAL}{path}", chain=chain)
        if d is None:
            continue
        up, live = d.get("upstream"), d.get("live")
        if live and up in (None, "ok"):
            extra = ""
            data = d.get("data") or {}
            rows = data.get("businesses") if isinstance(data, dict) else None
            # `businesses` IS A LIST OF ROWS on /operator/businesses and a DICT
            # OF COUNTS on /operator/traction — same key, two shapes, and the
            # first version of this gate crashed iterating the counts. Checked
            # by type rather than by route, so adding a route cannot resurrect it.
            if isinstance(rows, list):
                extra = f" \u00b7 {len(rows)} businesses"
                if not any(isinstance(b, dict) and b.get("slug") == "acr-operator"
                           for b in rows):
                    note("`acr-operator` is NOT in the live registry \u2014 the press "
                         "image predates it (swap the image)")
            elif isinstance(rows, dict):
                extra = f" \u00b7 {rows.get('businesses')} counted, {rows.get('spending')} spending"
            ok(f"{label}: live{extra}")
        elif is_demo:
            bad(f"THE DEMO WILL NOT WORK: {label} on {chain} is "
                f"upstream={up!r} live={live!r}. This is a page the demo opens.")
        else:
            # SHORT, and the remedy printed ONCE at the end. Repeating the same
            # two commit hashes on every line is a wall a reader skips, which is
            # the same reason the aging sentence on /check only appears when it
            # is true.
            msg = f"a judge sees {label} as upstream={up!r} on the default chain"
            print(f"  ✗ {msg}")
            judge_fails.append(msg)


# --- 6. the claim the demo makes about mainnet ------------------------------

def rehearse_mainnet_claim() -> None:
    step("6. The mainnet claim the demo makes, tested rather than asserted")
    print("     the demo says: 'mainnet is deployed, the press is unfunded'")
    try:
        from web3 import Web3
    except Exception as exc:  # noqa: BLE001
        bad(f"cannot import web3 ({exc})")
        return
    w = Web3(Web3.HTTPProvider(MAINNET_RPC, request_kwargs={"timeout": 60}))
    try:
        chain = w.eth.chain_id
    except Exception as exc:  # noqa: BLE001
        bad(f"mainnet RPC unreadable ({type(exc).__name__}) — the claim cannot "
            "be checked, so do not make it on stage")
        return
    if chain != 5042:
        bad(f"mainnet RPC reports chain {chain}, not 5042")
        return

    missing = []
    for name, addr in MAINNET_CONTRACTS.items():
        code = w.eth.get_code(Web3.to_checksum_address(addr))
        if len(code) <= 2:
            missing.append(name)
    if missing:
        bad(f"'deployed' is FALSE for: {', '.join(missing)} — no code on 5042")
    else:
        ok(f"all {len(MAINNET_CONTRACTS)} mainnet contracts have code on 5042")

    gas = w.eth.get_balance(Web3.to_checksum_address(PRESS_WALLET)) / 1e18
    if gas < 1.0:
        ok(f"'press is unfunded' is TRUE: {gas:.6f} USDC gas, so it cannot post")
    else:
        bad(f"the press holds {gas:.6f} USDC — it is FUNDED, so saying it is "
            "unfunded would be wrong, and a judge can check this in one call")


def main() -> int:
    print(f"ACR demo rehearsal · {time.strftime('%Y-%m-%d %H:%M')} "
          f"· strict={'on' if STRICT else 'off'}")
    print(f"  press    {TESTNET_API}")
    print(f"  terminal {TERMINAL}")

    rehearse_press()
    rehearse_par()
    rehearse_wallet()
    # THE DEMO'S CHAIN FIRST. Both must be green before the 17th — they are both
    # public — but when the gate is red it matters enormously which one it is,
    # so they are separate steps with separate sentences.
    rehearse_terminal("testnet", is_demo=True)
    rehearse_terminal("mainnet", is_demo=False)
    rehearse_mainnet_claim()

    print("\n" + "-" * 70)
    if notes:
        print(f"{len(notes)} thing(s) worth knowing before the 15th:")
        for n in notes:
            print(f"  · {n}")
    if fails:
        print(f"\n✗ {len(fails)} step(s) WOULD NOT SURVIVE THE DEMO:")
        for f in fails:
            print(f"    {f}")
    else:
        print("✓ every step of the DEMO answered, with real values")

    if judge_fails:
        print(f"\n✗ {len(judge_fails)} thing(s) a JUDGE sees "
              "that the demo does not touch:")
        for f in judge_fails:
            print(f"    {f}")
        print(f"\n  all of it is one deploy: {UNDEPLOYED}")
        print("  -> `vercel --prod` from apps/terminal")

    # Both matter before the 17th -- the demo is the 15th and the submission
    # surface is public either way. They are only REPORTED separately.
    return 1 if (fails or judge_fails) and STRICT else 0


if __name__ == "__main__":
    sys.exit(main())
