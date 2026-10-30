#!/usr/bin/env python
"""Does the deployment serve what this repo builds? — the question nothing asked.

Every other check in this repo reads one side. `make ci` is hermetic and proves
the source is sound. `verify_live.py` probes the internet and proves a visitor
sees something. **Neither compares the two**, so the repo could gain a feature,
lose one, or sit six weeks behind its own image and nothing would say so.

That is not hypothetical. Measured 2026-10-06, before this script existed:

    repo (HEAD)                    mainnet API (chain 5042)
    /operator/* — the product      ABSENT. Zero routes. `/spend` and
                                   `/traction` therefore render "No businesses
                                   onboarded yet" on a product that has one
    GET /par                       ABSENT, while `lib/endpoints.ts` advertises
                                   it as RUNNABLE, so `/developers` offers a
                                   reader a button that 404s
    /humanid/*, /tca/human         PRESENT on the deployment and, at the time,
                                   deleted from the repo — the docs describing
                                   them were accurate about production and
                                   would have become lies on the next deploy
    44 routes served               ~49 built

All four were found by hand, with curl, because somebody thought to look. Each
would have taken this script seconds. The lesson the repo already wrote down for
a different bug applies exactly: *check what production is SERVING before
reasoning about it.*

BOTH DIRECTIONS MATTER, and they mean opposite things:

    repo-only    an UNDEPLOYED change. The feature exists, the tests pass, and
                 no user can reach it. This is the one that makes a reviewer
                 click a dead link.
    deploy-only  a STALE IMAGE still serving code this repo no longer builds.
                 Harmless until the day it is redeployed, at which point every
                 document describing it becomes wrong at once.

NO ASSERTED TOTALS. The output prints the counts it measured rather than
comparing them against a number written here; a hard-coded route count is a
claim that rots, and this repo has `verify_claims.py` precisely because such
numbers do. What IS asserted is set equality between what is built and what is
served.

Read-only: one GET per host, no writes, no credentials.

    uv run python scripts/verify_deploy_drift.py
    ACR_API_URL=… VERIFY_DRIFT_HOSTS=…,… uv run python scripts/verify_deploy_drift.py

Exit codes: 0 = every host serves exactly what the repo builds (or could not be
reached, which is `verify_live.py`'s question and not this one); 1 = drift.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

#: Both known deployments by default, because the interesting finding was that
#: they disagree with each other as well as with the repo: one is Arc mainnet
#: and the other Arc testnet, and only one of them serves the operator.
DEFAULT_HOSTS = (
    os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com"),
    "https://acr-api-1fto.onrender.com",
)
#: Free-tier hosts sleep. A cold start measured 25s on the testnet service, so a
#: short timeout here would report drift as unreachability.
TIMEOUT_S = float(os.environ.get("VERIFY_TIMEOUT_S", "90"))


def hosts() -> tuple[str, ...]:
    raw = os.environ.get("VERIFY_DRIFT_HOSTS", "")
    picked = tuple(h.strip().rstrip("/") for h in raw.split(",") if h.strip()) or DEFAULT_HOSTS
    return tuple(h.rstrip("/") for h in picked)


def repo_paths() -> set[str]:
    """What this checkout builds, from the same object `gen_openapi_doc.py` reads.

    `app.openapi()` rather than a hand-kept list, for the reason that file gives:
    one source of truth, and a route added without touching anything else still
    shows up here.
    """
    sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..")))
    from index_api.app import app

    return set(app.openapi().get("paths", {}))


def deployed_paths(base: str) -> set[str] | None:
    """What a host serves. `None` means it could not be asked, which is NOT drift.

    A sleeping free-tier service and a service missing half the product look
    identical through a short timeout, and calling the first one "drift" would
    make this script cry wolf on every cold start.
    """
    req = urllib.request.Request(
        f"{base}/openapi.json", headers={"User-Agent": "acr-verify-deploy-drift"}
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:  # noqa: S310
            if r.status != 200:
                return None
            return set(json.loads(r.read()).get("paths", {}))
    except (urllib.error.URLError, urllib.error.HTTPError, ValueError, OSError):
        return None


def _family(paths: set[str]) -> dict[str, int]:
    """Group by first segment, so a missing FEATURE reads as one line rather than
    as nine paths a reader has to pattern-match themselves."""
    out: dict[str, int] = {}
    for p in sorted(paths):
        head = p.strip("/").split("/")[0] or "/"
        out[head] = out.get(head, 0) + 1
    return out


#: Promote "could not read" from a pass to a failure.
#:
#: Unreadable-is-not-drift is the right default for a standing report — a
#: free-tier cold start must not read as a missing feature, which is the whole
#: of `deployed_paths`'s docstring. It is the WRONG default for a gate at the
#: end of a deploy, where the entire job is to witness that the new image is
#: serving: there, "I could not tell" is the one answer that must not pass.
#:
#: Found by running it twice. The first run exited 0 on a host that was 1 route
#: short, because its /openapi.json timed out; the second reported the route.
#: A gate that says yes when it cannot see is the fail-open shape this repo
#: keeps closing — `mainnet_guard` exists because two x402 modes did it.
#: Same `VERIFY_*_STRICT` idiom `verify_live.py` already uses to promote its
#: warn-only checks.
STRICT = os.environ.get("VERIFY_DRIFT_STRICT", "") == "1"


def report(base: str, built: set[str]) -> bool:
    """One host. Returns True when it serves exactly what the repo builds."""
    served = deployed_paths(base)
    print(f"\n  {base}")
    if served is None:
        print("    ! could not read /openapi.json — asleep, down, or not an ACR host.")
        if STRICT:
            print("      VERIFY_DRIFT_STRICT=1: an unreadable host cannot be shown to")
            print("      serve this checkout, so this is a FAILURE rather than a pass.")
            return False
        print("      Not counted as drift: that is verify_live.py's question.")
        return True

    repo_only = built - served
    deploy_only = served - built
    print(f"    built {len(built)} · served {len(served)} · in both {len(built & served)}")

    if not repo_only and not deploy_only:
        print("    ✓ the deployment serves exactly what this repo builds")
        return True

    if repo_only:
        print(f"    ✗ {len(repo_only)} route(s) BUILT BUT NOT SERVED — an undeployed change:")
        for head, n in sorted(_family(repo_only).items()):
            print(f"        /{head}  ({n})")
        for p in sorted(repo_only)[:12]:
            print(f"          {p}")
        if len(repo_only) > 12:
            print(f"          … and {len(repo_only) - 12} more")
    if deploy_only:
        print(f"    ✗ {len(deploy_only)} route(s) SERVED BUT NOT BUILT — a stale image:")
        for head, n in sorted(_family(deploy_only).items()):
            print(f"        /{head}  ({n})")
        for p in sorted(deploy_only)[:12]:
            print(f"          {p}")
        if len(deploy_only) > 12:
            print(f"          … and {len(deploy_only) - 12} more")
    return False


def main() -> int:
    built = repo_paths()
    print(f"route drift — this checkout builds {len(built)} routes")
    clean = [report(h, built) for h in hosts()]
    ok = all(clean)
    print()
    if ok:
        print("✓ no route drift")
    else:
        print("✗ route drift: a deployment disagrees with this checkout.")
        print("  BUILT BUT NOT SERVED  -> redeploy, or stop advertising the route")
        print("  SERVED BUT NOT BUILT  -> the image predates this checkout; the docs")
        print("                           describing those routes are about to go stale")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
