#!/usr/bin/env python
"""Does either Bittensor upstream still look like the thing we parse?

Every SN51/SN64 gate in CI reads a COMMITTED file. That is deliberate — the
serving path is fixture-pinned so the demo runs offline and the estimator is
deterministic — but it has one consequence nobody was covering: CI cannot
notice a field rename, an added auth requirement, or an endpoint disappearing.
`grep -ri 'subnet|bittensor|chutes|lium' .github/workflows/*.yml` returned zero
hits, and both captures are documented "network, manual, never CI". So the first
sign of an upstream break would have been a re-anchor months later, by hand.

This is the missing half: a canary over the five live endpoints, asserting the
contract each CONSUMER actually depends on — shape, a row-count floor, and the
keys that are read by name. It is allowed to fail without blocking a deploy,
because a third-party outage is not a reason to stop shipping; it is a reason
for somebody to know.

**What it deliberately does not do.** It never writes a fixture and never
re-anchors. A canary that edits the inputs the gates read would turn an upstream
change into a silently-green test run, which is the failure mode it exists to
catch.

The floors are set WELL below the measured population (2026-10-30, named per
contract) so the signal is "this endpoint broke", never "the market moved" — a
canary that cries on a normal day gets muted, and a muted canary is worse than
none.

    uv run python scripts/bt_canary.py              # all five, live
    uv run python scripts/bt_canary.py --skip-heavy # drop the 21 MB stats fetch
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from bt_snapshot import (  # noqa: E402 - stdlib-only sibling, reused rather than re-written
    CHUTES_API,
    CHUTES_LLM,
    LIUM_API,
    fetch,
    join_chutes,
)


class Contract:
    """One endpoint, and what the code that reads it assumes about the answer.

    `required` is the honest list: the keys this project reads BY NAME, so a
    rename is caught at the field rather than at the row count. They are checked
    against a sample of rows rather than all of them, because an upstream that
    adds a sparse optional key to one row of 96,890 has not broken anything and
    a canary that says it did will be ignored.
    """

    def __init__(
        self,
        name: str,
        url: str,
        *,
        shape: type,
        min_rows: int,
        required: tuple[str, ...] = (),
        measured: str = "",
        consumer: str = "",
        heavy: bool = False,
        extra: Callable[[Any], list[str]] | None = None,
    ) -> None:
        self.name, self.url, self.shape = name, url, shape
        self.min_rows, self.required = min_rows, required
        self.measured, self.consumer, self.heavy = measured, consumer, heavy
        self.extra = extra


def check(c: Contract, payload: Any, sample: int = 25) -> list[str]:
    """Breaches of `c` in `payload`, as human sentences. Empty means healthy.

    Pure, so the canary's own logic is tested offline in
    `tests/test_bt_canary.py` against synthetic breaches — a canary nothing
    exercises is indistinguishable from a canary that always passes.
    """
    if not isinstance(payload, c.shape):
        return [
            f"{c.name}: expected {c.shape.__name__}, got {type(payload).__name__} "
            f"— {c.consumer} parses this positionally and would read zero rows"
        ]
    out: list[str] = []
    n = len(payload)
    if n < c.min_rows:
        out.append(
            f"{c.name}: {n} row(s), below the floor of {c.min_rows} "
            f"(measured {c.measured}) — not a market move, a break"
        )
    if c.required:
        rows = payload if isinstance(payload, list) else [payload]
        seen: set[str] = set()
        for r in rows[:sample]:
            if isinstance(r, dict):
                seen |= set(r)
        if rows and not seen:
            out.append(f"{c.name}: no object in the first {sample} row(s) to check keys against")
        missing = [k for k in c.required if k not in seen] if seen else []
        if missing:
            out.append(
                f"{c.name}: {', '.join(missing)} absent from the first {sample} row(s) "
                f"— {c.consumer} reads {'them' if len(missing) > 1 else 'it'} by name"
            )
    if c.extra is not None:
        out += c.extra(payload)
    return out


def _pricing_has_tao_usd(payload: Any) -> list[str]:
    """The TAO/USD corroboration `acr_subnet.denomination.cross_check_tao_usd`
    calls load-bearing reads exactly this one key, and it is the independent half
    of the pair — Taostats being the other. Losing it would not crash anything;
    it would silently stop the cross-check, which is how it came to never run
    once already."""
    v = (payload or {}).get("tao_usd") if isinstance(payload, dict) else None
    if not isinstance(v, (int, float)) or v <= 0:
        return [
            f"chutes /pricing: tao_usd is {v!r} — `cross_check_tao_usd` has no "
            f"second feed, so the TAO/USD figure goes uncorroborated"
        ]
    return []


def _models_envelope(payload: Any) -> list[str]:
    """`/v1/models` is the one endpoint that is an ENVELOPE, not an array, and
    `bt_snapshot` reads `models.get("data")` — which yields `[]` for any reshape
    instead of raising."""
    data = (payload or {}).get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data:
        return [
            "chutes /v1/models: `data` is not a non-empty array, so the model "
            "price fixture would capture zero rows with no error"
        ]
    return []


CONTRACTS: tuple[Contract, ...] = (
    Contract(
        "chutes /invocations/usage",
        f"{CHUTES_API}/invocations/usage",
        shape=list,
        min_rows=400,
        required=("chute_id", "date", "usd_amount"),
        measured="1,649 rows over 12 dates, 2026-10-30",
        consumer="bt_snapshot.join_chutes",
    ),
    Contract(
        "chutes /invocations/stats/llm",
        f"{CHUTES_API}/invocations/stats/llm",
        shape=list,
        min_rows=5_000,
        required=(
            "chute_id",
            "date",
            "total_input_tokens",
            "total_output_tokens",
            "average_ttft",
        ),
        measured="96,890 rows / 21.4 MB in 13 s, 2026-10-30",
        consumer="bt_snapshot.join_chutes and ChutesSource's latency covariate",
        heavy=True,
    ),
    Contract(
        "lium /executors",
        f"{LIUM_API}/executors",
        shape=list,
        min_rows=40,
        required=("id", "machine_name", "price_per_gpu", "tier"),
        measured="152 executors, 2026-10-30 (160 the day before)",
        consumer="anchors.lium_medians, which groups on machine_name and filters on tier",
    ),
    Contract(
        "chutes /pricing",
        f"{CHUTES_API}/pricing",
        shape=dict,
        min_rows=1,
        measured="tao_usd 248.9581, 2026-10-30",
        consumer="acr_subnet.cross_check_tao_usd",
        extra=_pricing_has_tao_usd,
    ),
    Contract(
        "chutes /v1/models",
        f"{CHUTES_LLM}/v1/models",
        shape=dict,
        min_rows=1,
        measured="17 priced models, 2026-10-30",
        consumer="bt_snapshot's model-price fixture",
        extra=_models_envelope,
    ),
    Contract(
        "chutes /miner/metagraph",
        f"{CHUTES_API}/miner/metagraph",
        shape=list,
        min_rows=64,
        required=("hotkey", "coldkey", "netuid", "stake"),
        measured="256 neurons, 2026-10-30",
        consumer="acr_tape.chutes_metagraph.concentration",
    ),
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--skip-heavy",
        action="store_true",
        help="skip /invocations/stats/llm (21 MB, ~13 s) and the join that needs it",
    )
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    fails: list[str] = []
    payloads: dict[str, Any] = {}
    for c in CONTRACTS:
        if c.heavy and args.skip_heavy:
            print(f"  – {c.name:<30} skipped (--skip-heavy)")
            continue
        try:
            payload = fetch(c.url)
        except RuntimeError as exc:
            # An unreachable endpoint IS the finding. Reported as a breach rather
            # than a crash so one dead upstream still leaves the other four
            # measured — the same isolation `anchors --fetch` needed.
            fails.append(f"{c.name}: unreachable — {exc}")
            print(f"  ✗ {c.name:<30} unreachable")
            continue
        payloads[c.name] = payload
        bad = check(c, payload)
        fails += bad
        n = len(payload) if isinstance(payload, (list, dict)) else "?"
        print(f"  {'✗' if bad else '✓'} {c.name:<30} {n} row(s) — {c.consumer}")
        for b in bad:
            print(f"      {b}")

    # The end-to-end contract, which no single endpoint expresses: the two Chutes
    # payloads have to JOIN. They can each pass their own shape check and still
    # share no (chute_id, date) key, which would empty the ACR-INF tape — the
    # exact break this whole audit started from.
    usage = payloads.get("chutes /invocations/usage")
    stats = payloads.get("chutes /invocations/stats/llm")
    if isinstance(usage, list) and isinstance(stats, list):
        joined = join_chutes(usage, stats)
        dates = sorted({str(r.get("date")) for r in joined})
        ok = len(joined) >= 50
        print(
            f"  {'✓' if ok else '✗'} {'the join (usage x stats)':<30} "
            f"{len(joined)} priceable row(s) over {len(dates)} date(s) — the ACR-INF tape"
        )
        if not ok:
            fails.append(
                f"the join: {len(joined)} priceable rows, below 50 (measured 219 over "
                f"12 dates, 2026-10-30) — both payloads parsed but they no longer "
                f"agree on (chute_id, date), which empties the ACR-INF tape"
            )
    elif not args.skip_heavy:
        fails.append("the join could not be attempted — one of the two Chutes payloads is absent")

    if fails:
        print(f"\n  ✗ {len(fails)} upstream contract breach(es):")
        for f in fails:
            print(f"    {f}")
        print(
            "\n  This does NOT break the product: the serving path reads committed\n"
            "  fixtures on purpose. It means the next `make anchors-fetch` or\n"
            "  `make subnet-tape` would capture something we no longer parse."
        )
        return 1
    print("\n  ✓ every Bittensor upstream still matches the contract we parse")
    return 0


if __name__ == "__main__":
    sys.exit(main())
