#!/usr/bin/env python3
"""Pull the operator's live decision log into the committed archive.

    uv run python scripts/archive_decisions.py                  # append what production decided since the last archive
    uv run python scripts/archive_decisions.py --check          # exit 1 if production holds rows the archive does not
    uv run python scripts/archive_decisions.py --business acme  # one business rather than every registered one

THE SAME DISK THAT IS NOT THERE. `scripts/archive_receipts.py` exists because
the free tier has no persistent volume, so every Gateway settlement the deployed
seller records lives in container memory and a JSONL the next restart erases.
The operator writes its decisions to `data/operator_decisions.jsonl` on exactly
that disk, and nothing was pulling them in — so every decision made during the
hackathon would vanish on the next redeploy and the traction page would quietly
revert to the nine rows that ship inside the image.

Quietly is the word that matters. The page would not break; it would just report
a smaller number, which is the one failure shape nobody investigates.

HOW ROWS ARE IDENTIFIED. Receipts dedupe on `tx_ref`. A decision has no such
key: the log is append-only and the SAME obligation legitimately appears twice —
once escalated, once resolved — so the identity is the whole of
`(obligation_id, at, intent)`. Keying on `obligation_id` alone would drop every
resolution, which is precisely the half of the record that proves a human was
involved.

TRUNCATION IS A FAILURE, NOT A RESULT. The statement caps its rows, so this asks
for a cap far above the record and refuses to write if production returns
exactly that many — a full page means there may be more behind it, and an
archiver that silently stops at the cap is how a record of 400 decisions becomes
a record of 50 with nobody the wiser.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

# What makes two decision rows the same row — defined in the package that ships
# to production, not here, because `/ops` now reports how many live decisions a
# redeploy would destroy and that figure is only true when it is computed with
# the rule THIS script will apply. See `statement.decision_key`.
from index_api.statement import decision_key as _key

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "services" / "index_api" / "index_api" / "operator_decisions.jsonl"
API = os.environ.get("ACR_API_URL", "https://acr-api-mainnet.onrender.com").rstrip("/")

#: Far above any plausible record, and the server clamps it to 1000. If a
#: business ever really has 1000 decisions in a window this script says so and
#: stops rather than writing a truncated archive over a complete one.
LIMIT = 1000
#: A year. The archive is cumulative, so the window only has to cover the gap
#: since the last run; a wide one costs a single read and removes a whole class
#: of "we redeployed late and lost a week".
DAYS = 365




def _archived(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out


class NoOperatorThere(RuntimeError):
    """The press answered, and it has no operator surfaces.

    A legitimate state, and the one this script meets on the FIRST deploy: the
    running image predates the operator, so there are no decisions on it to lose.
    Distinguished from a network failure because the two call for opposite
    actions — deploy over it, or stop and investigate — and a traceback said
    neither.
    """


class NotOurPress(RuntimeError):
    """The host answered, and it is not an ACR press at all.

    A typo'd or decommissioned Render hostname returns a plain 404 — the same
    status an OLDER ACR image returns for ``/operator/businesses``, because that
    route did not exist yet. One of those means "nothing to lose, deploy over
    it" and the other means "you are pointed at the wrong machine", and the
    preflight read both as the first: measured against a hostname that has never
    existed, it printed "nothing to archive" and passed.

    Which is the failure this file opens by describing — a smaller number, not a
    broken page — arrived at from the other end.
    """


def _is_our_press(api: str) -> dict | None:
    """The control probe. ``/health`` is on every ACR image ever deployed.

    Runs only after a 404, and it is the cheapest question that the 404 itself
    cannot answer: a press that is ours answers it with JSON carrying a
    ``status``, and a hostname that is not ours has nothing on that path either.
    """
    try:
        with urllib.request.urlopen(f"{api}/health", timeout=30) as r:
            body = json.load(r)
    except Exception:
        return None
    return body if isinstance(body, dict) and "status" in body else None


def _businesses(api: str) -> list[str]:
    """Every registered business, SANDBOXES EXCLUDED — and not optionally.

    This took an ``include_sandbox`` flag that nothing ever passed, so the
    branch was unreachable. Removed rather than wired to a CLI switch: the only
    thing that flag could do is merge the demonstration fixtures into the record
    of what the agent did for real businesses, which the paragraph below exists
    to forbid. An option whose only use is the forbidden one should not exist.

    A sandbox's decisions are hand-written fixtures that already ship in their
    own archive (`operator_decisions.sandbox.jsonl`), kept apart so they can be
    excluded from every traction figure. Appending them to the real archive
    would merge a demonstration into the record of what the agent actually did
    for actual businesses — the one mixture this product cannot afford.
    """
    try:
        with urllib.request.urlopen(f"{api}/operator/businesses", timeout=60) as r:
            body = json.load(r)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            health = _is_our_press(api)
            if health is None:
                raise NotOurPress(
                    f"{api} returned 404 for /operator/businesses AND has no /health: "
                    "this is not an ACR press. Check the hostname — a wrong host "
                    "cannot be shown to be safe to erase"
                ) from e
            raise NoOperatorThere(
                f"{api} (chain {health.get('chain_id')}) has no /operator/businesses: "
                "the running image predates the spend operator, so it holds no "
                "decisions to archive"
            ) from e
        raise
    rows = body.get("data", body).get("businesses") or []
    return [
        str(b["slug"])
        for b in rows
        if b.get("slug") and not b.get("sandbox")
    ]


def _live(api: str, slug: str) -> tuple[list[dict], bool]:
    """One business's decisions, newest first, and whether the page was full."""
    url = f"{api}/operator/statement/{slug}?days={DAYS}&limit={LIMIT}"
    with urllib.request.urlopen(url, timeout=120) as r:
        body = json.load(r)
    body = body.get("data", body)
    rows = list(body.get("recent") or [])
    return rows, len(rows) >= LIMIT


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--api", default=API)
    ap.add_argument("--archive", default=str(ARCHIVE))
    ap.add_argument("--business", default="", help="one slug; default is every registered one")
    ap.add_argument(
        "--check", action="store_true",
        help="report, do not write; exit 1 when production holds rows the archive does not",
    )
    a = ap.parse_args()
    path = Path(a.archive)

    have = _archived(path)
    seen = {_key(r) for r in have}
    rel = path.relative_to(ROOT) if path.is_relative_to(ROOT) else path
    print(f"archive   {rel}: {len(have)} row(s)")

    try:
        slugs = [a.business] if a.business else _businesses(a.api)
    except NoOperatorThere as e:
        # Nothing to lose, so nothing to refuse. A preflight that blocked here
        # would make the first deploy of the operator impossible.
        print(f"live      {e}")
        print("nothing to archive")
        return 0
    except NotOurPress as e:
        print(f"\nREFUSING: {e}", file=sys.stderr)
        return 2
    except OSError as e:
        # PRODUCTION DID NOT ANSWER, which is not the same as having nothing.
        #
        # A free-tier host asleep behind a cold start takes longer than the
        # timeout, and this used to leave an unhandled traceback — which exits
        # 1, which is EXACTLY the code `--check` uses for "production is ahead
        # of the archive". `deploy/redeploy-render.sh` then printed "Production
        # holds operator decisions this repo does not" and told the operator to
        # run the archiver, which would fail the same way. Measured against the
        # sleeping mainnet press on 2026-10-03.
        #
        # 2, like the truncation refusal below: a distinct code for "I could not
        # tell", so a caller can say the true sentence.
        print(f"\nREFUSING: {a.api} did not answer ({type(e).__name__}: {e}).", file=sys.stderr)
        print(
            "  A host that cannot be read cannot be shown to be safe to erase. If it is "
            "asleep, wake it (curl its /health) and re-run.",
            file=sys.stderr,
        )
        return 2
    print(f"live      {a.api}: {len(slugs)} real business(es), sandboxes excluded")

    new: list[dict] = []
    truncated: list[str] = []
    for slug in slugs:
        try:
            rows, full = _live(a.api, slug)
        except OSError as e:
            # Same refusal, one business in. Reading some of production and
            # reporting on it as though it were all of production is the shape
            # that loses rows quietly.
            print(f"\nREFUSING: {a.api} stopped answering at {slug} "
                  f"({type(e).__name__}: {e}).", file=sys.stderr)
            return 2
        if full:
            truncated.append(slug)
        fresh = [r for r in reversed(rows) if _key(r) not in seen]
        for r in fresh:
            seen.add(_key(r))
        new += fresh
        print(f"  {slug:<18} {len(rows):>4} row(s), {len(fresh)} the archive does not hold")

    if truncated:
        # Refusing is the whole point. Writing what we got would leave an
        # archive that looks complete and is not, and nothing downstream could
        # tell the difference.
        print(
            f"\nREFUSING: {', '.join(truncated)} returned the full {LIMIT}-row page, "
            "so there may be more behind it. Raise LIMIT or narrow DAYS and re-run.",
            file=sys.stderr,
        )
        return 2

    print(f"missing   {len(new)} row(s) production holds that the archive does not")
    for r in new[:10]:
        print(
            f"  + {r.get('obligation_id','')[:38]:<38} {r.get('intent',''):<9} "
            f"{float(r.get('billed_usdc') or 0.0):.6f} "
            f"{'owner' if r.get('actor') == 'owner' else 'agent'}"
        )
    if len(new) > 10:
        print(f"  … and {len(new) - 10} more")

    if a.check:
        return 1 if new else 0
    if not new:
        print("nothing to do")
        return 0

    with path.open("a", encoding="utf-8") as f:
        for r in new:
            f.write(json.dumps(r, separators=(",", ":")) + "\n")
    print(
        f"wrote     {len(new)} row(s); archive now {len(have) + len(new)}. "
        "Commit it, then rebuild the image."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
