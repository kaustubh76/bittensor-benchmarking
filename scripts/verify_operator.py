#!/usr/bin/env python3
"""Prove the deployed operator surfaces — one exit code.

    uv run python scripts/verify_operator.py
    VERIFY_TERMINAL_URL=http://127.0.0.1:3000 uv run python scripts/verify_operator.py

`verify_live.py` proves the seller and `verify_loop.py` proves the /loop page's
instruments. This proves the three surfaces a Tameion reviewer will open: the
business list, one business's Spend Statement, and its beancount ledger.

IT CHECKS THE HONESTY PROPERTIES, not just the status codes, because a 200 on a
page that quietly says the wrong thing is the failure that matters here:

  * the statement never carries `overpaid_usdc` — the index's dollar figure,
    which `anchors/GAP.md` puts 10x to 1250x off market and which would read as
    money on an owner's page;
  * the traction payload reports mainnet and testnet apart, with no field
    adding them;
  * `moved_usdc` and `priced_usdc` are both present, because collapsing them
    into one number is the most tempting lie on a traction page;
  * every served ledger balances — each transaction's postings sum to zero;
  * an unconsented business is never named, and an unknown one 404s rather
    than returning an empty statement that reads as "has spent nothing".

Nothing here spends and nothing here needs a key: every surface it touches is a
free read, which is the point of them.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

# The host that actually serves, kept in step with verify_live.py and
# verify_loop.py. Was a custom domain that never resolved, so `make
# verify-operator` probed all three reviewer surfaces on a parked page.
TERMINAL = os.environ.get(
    "VERIFY_TERMINAL_URL", "https://arc-compute-rate.vercel.app"
).rstrip("/")
TIMEOUT_S = 45.0

_failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {label}")
    if not ok:
        _failures.append(label)
    return ok


#: A deliberately wrong key, shaped so it is FORWARDED rather than rejected.
#:
#: The Next proxy refuses a missing or malformed token itself — 401, locally,
#: without ever contacting the press — so probing with no header proved only
#: that the proxy exists. Against the default target this check passed
#: unconditionally while claiming to have learned something about the press.
#:
#: With a well-formed key the proxy forwards, and the PRESS answers: 404 when no
#: ACR_OPS_TOKEN is configured (it does not admit the console exists), 401 when
#: one is and this is not it. That difference is the whole point, and it is the
#: same answer through either target.
#:
#: It is a wrong key on purpose, and the press's bad-key limiter counts it. One
#: per run is the cost of knowing; 429 is accepted below as "configured" because
#: a limiter that answers is a console that exists.
PROBE_KEY = "verify-operator-probe-not-a-real-key"


def _fetch(url: str, headers: dict | None = None) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={"accept": "*/*", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as exc:
        print(f"    ({exc})")
        return 0, ""


def get(path: str, headers: dict | None = None) -> tuple[int, str]:
    """Fetch one surface from whichever host is being verified.

    The Next app serves `/api/operator/...` and the press serves
    `/operator/...`, so the same script has to work against either: pointed at
    the deployed terminal it goes through the proxy, pointed at a press it goes
    direct. Tries the given path, and on a 404 tries the other prefix — which is
    also the honest way to tell "this surface is missing" from "I asked the
    wrong host".
    """
    status, body = _fetch(TERMINAL + path, headers)
    if status == 404:
        alt = path[4:] if path.startswith("/api/") else "/api" + path
        # A press answers `/operator/statement/{slug}`; the proxy takes a query.
        alt = alt.replace("/operator/statement?business=", "/operator/statement/")
        # Same for the ledger, which grew a proxy once /traction's link to it
        # turned out to point at the press's path space from the terminal origin.
        alt = alt.replace("/operator/ledger?business=", "/operator/ledger/")
        alt = alt.replace("/operator/audit?business=", "/operator/audit/")
        s2, b2 = _fetch(TERMINAL + alt, headers)
        if s2 != 404:
            return s2, b2
    return status, body


def balance_problems(text: str) -> list[str]:
    """Re-implemented here on purpose.

    Importing the exporter's own checker would let a bug in it pass its own
    output. This reads the SERVED file with an independent pair of eyes, which
    is the only version of the check worth running against a deployment.
    """
    import re

    problems: list[str] = []
    header: str | None = None
    postings: list[float] = []

    def close() -> None:
        if header is None:
            return
        if not postings:
            problems.append(f"{header}: no postings")
        elif round(sum(postings), 6) != 0:
            problems.append(f"{header}: sums to {sum(postings):+.6f}")

    for line in text.splitlines():
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\*", line):
            close()
            header, postings = line.strip(), []
            continue
        if re.match(r"^\d{4}-\d{2}-\d{2}\s+\S", line) or line.startswith("option"):
            close()
            header, postings = None, []
            continue
        if header is not None and line.startswith("  "):
            m = re.search(r"(-?\d+\.\d+)\s+USDC\s*$", line)
            if m:
                postings.append(float(m.group(1)))
    close()
    return problems


def operator_clock() -> None:
    """What `ACR_OPERATOR_AUTORUN` is set to on the deployment, and what that means.

    `docs/TAMEION.md` presents the autorun table as *"what makes 'settled without a
    human touching them' a thing the record shows rather than a thing the design
    permits"* — and `render.yaml` ships `off` on BOTH services. `off` is the
    default a checkout, a laptop and a CI run get; on a deployment it means the
    decision log can only grow when somebody types `make operator-run`.

    REPORTED, not failed. `off` is a legitimate configuration and this script's
    other checks are all hard failures, so turning a deliberate setting red would
    make the gate something to mute. What was wrong was that nothing SAID it: the
    autonomy figures are true about who signed, and silent about who invoked. A
    control nobody audits is a control nobody can show you, which is this repo's
    own argument for the audit endpoint.
    """
    print("\nthe operator's own clock")
    status, body = get("/api/health")
    if not check(status == 200, f"/api/health answers ({status})"):
        return
    env = json.loads(body) if body else {}
    data = env.get("data") if isinstance(env.get("data"), dict) else env
    op = data.get("operator") or {}
    mode = str(op.get("mode") or "")
    checked = op.get("checked_at")

    # THREE states, and the first one is why this needed a second pass. An absent
    # `operator` key is "this press does not report one" — an older image, or a URL
    # that is not the terminal — and my first version let it fall through to the
    # armed branch, where `checked_at: null` hard-failed as though a live loop had
    # stalled. Same mistake the counterparty screen exists to refuse: "we could not
    # ask" is not "we asked and the answer was bad".
    if not mode:
        check(True, "this press reports no operator loop — nothing to say about its clock")
        return
    if mode == "off":
        check(True, f"autorun: {mode} — the log grows only when somebody runs "
                    "`make operator-run`, so every figure below is a human's invocation "
                    "of the agent's authority, not the loop ticking")
    else:
        age = op.get("checked_age_s")
        when = "never" if checked is None else f"{age}s ago"
        check(True, f"autorun: {mode}, every {op.get('every_s')}s, last checked {when}")
        # An ARMED loop that has never checked is exactly the state `dry` exists to
        # rule out: the first question about any new loop is whether it ticks at
        # all on that host, and that should be answerable before a payment depends
        # on it. The deployed press reported `checked_at: null` for all three of
        # the venue keeper's chores, which is how this failure looks.
        check(checked is not None, "an armed loop has actually ticked (checked_at is not null)")


def main() -> int:
    print(f"operator surfaces on {TERMINAL}\n")

    print("the business list")
    status, body = get("/api/operator/businesses")
    # THREE STATES, and the middle one is new. This was `if not check(status ==
    # 200): return _done()` — a hard early exit that skipped traction, the
    # clock, the statement, the ledger, the audit and the refusal block.
    #
    # It had to change together with the proxy. `app/api/operator/businesses/
    # route.ts` used to answer 200 with `data: null` when the press was
    # unreachable, so a press with no operator routes produced a GREEN run of
    # this script over a page that said "No businesses onboarded yet". The proxy
    # now refuses with 503, and a checker that read 503 as failure would have
    # turned the honest answer into the red one — punishing exactly the fix.
    #
    # Same shape as `operator_clock()`, and the same principle the counterparty
    # screen is built on: "we could not ask" is not "we asked and it was bad".
    refused = status == 503
    rows: list = []
    counts: dict = {}
    if refused:
        check(True, "the business list refuses rather than lies (503)")
        # The STATUS is not the whole claim. A 503 with a figure in it would be
        # a page that renders something while telling a client to ignore it.
        env = json.loads(body) if body else {}
        check(
            env.get("data") is None and env.get("live") is False,
            "the refusal carries no figures: {live:false, data:null}",
        )
    elif not check(status == 200, f"/api/operator/businesses answers ({status})"):
        return _done()
    else:
        env = json.loads(body) if body else {}
        # The proxy wraps its payload in {live, data}; the press returns it bare.
        # Unwrapping defensively lets one script verify both.
        data = env.get("data") if isinstance(env.get("data"), dict) else env
        rows = data.get("businesses") or []
        counts = data.get("counts") or {}
        if "live" in env:
            check(env.get("live") is True, "it is answering live, not from a bundle")
        # NOT len(rows). The list carries every row; the count deliberately drops
        # the sandbox ones, because a business we invented to demonstrate the agent
        # must never appear in a figure a reviewer reads as adoption. The asymmetry
        # IS the property, so this asserts it rather than asserting it away.
        real = [r for r in rows if not r.get("sandbox")]
        check(
            counts.get("businesses") == len(real),
            f"the count is the real businesses, sandboxes excluded "
            f"({counts.get('businesses')} = {len(real)} of {len(rows)})",
        )
        check(
            all(r.get("sandbox") is not None for r in rows),
            "every row says whether it is a sandbox, so nothing counts by omission",
        )
        check(
            all(("name" in r) == bool(r.get("consented")) for r in rows),
            "a business is named only where it consented",
        )

    print("\nthe traction figures")
    status, body = get("/api/operator/traction")
    if status == 503:
        check(True, "the traction figures refuse rather than lie (503)")
        env = json.loads(body) if body else {}
        check(
            env.get("data") is None and env.get("live") is False,
            "the refusal carries no figures: {live:false, data:null}",
        )
    elif check(status == 200, f"/api/operator/traction answers ({status})"):
        raw = json.loads(body) or {}
        t = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        by_chain = t.get("by_chain") or {}
        work = t.get("work") or {}
        check("total" not in by_chain and "all" not in by_chain,
              "no field adds mainnet and testnet together")
        check(
            all({"moved_usdc", "priced_usdc"} <= set(v) for v in by_chain.values()),
            "moved and priced are both reported, never collapsed into one",
        )
        check("decided" in work and "escalated" in work,
              "decided and escalated are two numbers, not a ratio")

    operator_clock()

    # The refusal is checked BEFORE the empty list, because `rows` is empty in
    # both cases and the sentence below is a claim about the registry. Printing
    # "that is a true state rather than a broken one" at a press that never
    # answered would be this script telling the same lie the page used to.
    if refused:
        print("\nthe press is not answering, so nothing further can be proved about")
        print("its contents. What IS proved is the part that was broken: both")
        print("surfaces said so, rather than rendering a zero that reads as fact.")
        return _done()

    if not rows:
        print("\nno businesses onboarded yet — nothing further to prove, and that")
        print("is a true state rather than a broken one.")
        return _done()

    slug = rows[0]["slug"]
    print(f"\nthe statement for {slug}")
    st: dict = {}
    status, body = get(f"/api/operator/statement?business={slug}")
    if check(status == 200, f"/api/operator/statement answers ({status})"):
        blob = body
        raw = json.loads(body) or {}
        st = raw.get("data") if isinstance(raw.get("data"), dict) else raw
        check("overpaid_usdc" not in blob,
              "the index's dollar figure is absent (anchors/GAP.md puts it 20-1250x off market)")
        ctx = st.get("market_context") or {}
        if ctx.get("available"):
            check("bp only" in str(ctx.get("basis", "")),
                  "the index appears as basis points, labelled as such")
        check("escalations" in st and isinstance(st["escalations"], list),
              "the queue a person has to act on is present")
        check(bool((st.get("spend") or {}).get("decisions") is not None),
              "the period summary is present")

    print(f"\nthe ledger for {slug}")
    # Asked in the TERMINAL's shape, with `get` falling back to the press's.
    # This used to be a press-only surface, and the note here said so — while
    # /traction linked to `/operator/ledger/{slug}`, a press path that resolved
    # against the terminal origin and 404ed. The double-entry file was the one
    # artifact a reader could not open, so its absence is a failure, not a note.
    status, text = get(f"/api/operator/ledger?business={slug}")
    if check(status == 200, f"the ledger is served to a reader ({status})"):
        check('option "operating_currency" "USDC"' in text, "it is a beancount file")
        problems = balance_problems(text)
        check(not problems, f"every transaction balances ({len(problems)} problem(s))")
        for p in problems[:3]:
            print(f"      {p}")
        check("Income" not in text,
              "no invented credit: a reroute moved no money and is not a transaction")
        check("1970-01-01" not in text, "no account opened at the Unix epoch")

    print(f"\nthe six errors a trial balance cannot see, for {slug}")
    # The ledger check above is the WEAK one, and saying so is the point. Every
    # error below sums to zero: *Agents and Ledgers* — "nearly every mistake an
    # LLM can make with money passes it" — names six, and a product whose only
    # ledger claim is "it balances" has claimed almost nothing.
    status, body = get(f"/api/operator/audit?business={slug}")
    if check(status == 200, f"the audit is served to a reader ({status})"):
        a = json.loads(body)
        a = a.get("data") if isinstance(a.get("data"), dict) else a
        checks = a.get("checks") or []
        names = [c.get("error") for c in checks]
        check(
            names[:6] == [
                "omission", "commission", "principle",
                "original entry", "compensating", "complete reversal",
            ],
            f"all six are searched for, by name ({len(names)} reported)",
        )
        # The seventh is not one of the six. It is the fictitious entry the
        # essay says nobody can disprove, and the one this product's own write
        # path could once produce.
        check(
            "phantom payment" in names,
            "and the phantom payment is looked for too, beside them",
        )
        check(
            all(isinstance(c.get("searched"), int) for c in checks),
            "each one says what it searched, so 'found 0' means something",
        )
        check(
            "unattributable_settlements" in a,
            "a settlement with no payee is reported apart, never counted clean",
        )
        for c in checks:
            mark = "·" if not c.get("found") else "✗"
            print(f"    {mark} {c.get('error'):<18} "
                  f"found {c.get('found')} of {c.get('searched')} searched")
        for f in (a.get("findings") or [])[:5]:
            print(f"      {f.get('error')}: {f.get('detail')}")

    print("\nthe escalation path, without using a key")
    # THE ONE SURFACE THIS SCRIPT NEVER TOUCHED. Its scope is free reads, so it
    # proved the whole operator except the thing the operator is for: a person
    # clearing the queue. Two states are distinguishable without any key at all,
    # and they are exactly the two a reviewer needs to tell apart —
    #   404  no ACR_OPS_TOKEN on this deployment, so the buttons are dead
    #   401  a console IS configured and refused us, which is the healthy answer
    status, _ = get("/api/ops/actions", headers={"X-ACR-Ops-Token": PROBE_KEY})
    check(
        status in (401, 429),
        "the operator console is configured, so the queue can actually be cleared "
        f"({status}; 404 would mean no key is set on the press)",
    )

    # And WHICH screen is answering. A verdict of "clear" from a sanctions
    # dataset and one from a local list of zero addresses are not the same
    # assurance, and the record carries the backend so a reader can tell.
    backends = {
        str(d.get("screen_backend") or "")
        for d in (st.get("recent") or [])
        if d.get("screen_backend")
    }
    if backends:
        check(
            "off" not in backends,
            f"the counterparty screen names a real backend ({', '.join(sorted(backends))})",
        )
    else:
        print("    (no decision in this window records which screen answered)")

    print("\nthe refusals")
    # The press 404s an unknown business. The terminal's statement proxy answers
    # 200 with a {live:false, data:null} envelope, so the page can render the
    # absence instead of a stale figure. Both are refusals; neither is an empty
    # statement reading as "has spent nothing".
    #
    # THE CONVENTION IS NOW SPLIT, and the split is the whole point of it. A
    # 200/null means the press ANSWERED AND THERE IS NOTHING — which is true
    # here: "has this business spent anything" has the honest answer "there is
    # no such business". `/businesses` and `/traction` return null for a
    # different reason, that we could not ask at all, and they now say 503. The
    # same envelope was carrying both meanings, and the page could not tell them
    # apart because nothing in the response did.
    status, body = get("/api/operator/statement?business=definitely-not-a-business")
    unknown_refused = status in (404, 422, 503)
    if status == 200 and body:
        env = json.loads(body)
        unknown_refused = env.get("data") is None
    check(unknown_refused, f"an unknown business is refused, not answered empty ({status})")
    status, _ = get("/api/operator/statement?business=../etc/passwd")
    check(status in (400, 404, 422), f"a path-shaped business is rejected ({status})")

    return _done()


def _done() -> int:
    print()
    if _failures:
        print(f"{len(_failures)} FAILED:")
        for f in _failures:
            print(f"  ✗ {f}")
        return 1
    print("every operator surface holds")
    return 0


if __name__ == "__main__":
    sys.exit(main())
