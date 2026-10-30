#!/usr/bin/env python
"""Snapshot Bittensor subnet price data — subnet 64 (Chutes) and subnet 51 (Lium).

Two subnets sell exactly what ACR measures, and both publish prices over
unauthenticated HTTP:

  * **Chutes (netuid 64)** serves inference. `/invocations/usage` reports the USD
    actually billed per model per day and `/invocations/stats/llm` reports the
    tokens that bought it, so dividing one by the other gives a **realized**
    $/1k-token price — a transacted number, not a rate card. That is the only
    genuinely transacted series either subnet exposes, and it is what feeds the
    ACR-INF tape.
  * **Lium (netuid 51)** rents GPUs. `/api/executors` lists an **ask** per GPU on
    inventory that is mostly idle, which is a reservation price rather than a
    traded one. It is captured here for the `anchors/` basket — where external
    list prices belong — and deliberately NOT turned into tape. See
    `docs/BITTENSOR.md` for why that distinction is load-bearing.

Stdlib only, on purpose: `acr_tape` depends on acr-core, acr-sim,
acr-oracle-client and web3, and a snapshot tool is not a reason to add an HTTP
client to that list.

    uv run python scripts/bt_snapshot.py --dry-run           # shapes only, no writes
    uv run python scripts/bt_snapshot.py --capture-fixture   # refresh tests/fixtures/bt/
    uv run python scripts/bt_snapshot.py                     # append to the archive

`--capture-fixture` writes the committed, deterministic offline inputs that the
tape sources and their tests read, so the whole subnet path runs with no network
and no API key. Plain invocation appends to the CUMULATIVE ARCHIVE under
`services/` instead (see `TAPE_DIR`) — the forward-polling path which, over
weeks, is the only way either subnet grows a real series. It used to write under
`data/`, which is in both .gitignore and .dockerignore on a host with no
persistent disk, so every observation it had ever made was discarded; against a
~12-day upstream retention window that made each lost day permanent.

`scripts/bt_canary.py` watches the same upstreams for a shape change, and
`.github/workflows/bittensor-refresh.yml` runs both on a schedule — this script
was "network, manual, never CI" for its whole life, which is why the archive was
empty and the anchor 24 days old when anyone looked.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "tests" / "fixtures" / "bt"

#: The cumulative forward-poll archive. It lives under ``services/`` for exactly
#: the reason the x402 receipt archive does (see ``scripts/x402_capture.py`` and
#: the ``x402-capture`` target): ``data/`` is in BOTH .gitignore and
#: .dockerignore, so a tape written there reaches neither the repo nor the image,
#: and production has no persistent disk. That matters more here than for
#: receipts, because upstream Chutes usage only retains ~12 days -- a gap longer
#: than that is a PERMANENT hole in the history of a continuous series. Override
#: with ``ACR_BT_TAPE_DIR`` for a test or a one-off capture.
TAPE_DIR = Path(
    os.environ.get(
        "ACR_BT_TAPE_DIR",
        ROOT / "services" / "index_api" / "index_api" / "bt_tape",
    )
)

CHUTES_API = "https://api.chutes.ai"
CHUTES_LLM = "https://llm.chutes.ai"
LIUM_API = "https://lium.io/api"

#: Fields kept from a Lium executor. The endpoint returns ~45 keys per executor,
#: most of them operational (bookmarks, scheduled notices). These are the price
#: and the quality covariates a hedonic adjustment could actually use.
LIUM_KEEP = (
    "id",
    "machine_name",
    "price_per_gpu",
    "gpu_count",
    "available_gpu_count",
    "miner_hotkey",
    "validator_hotkey",
    "gpu_class",
    "tier",
    "reliability_score",
    "uptime_in_minutes",
    "location",
    "interconnect",
    "nvlink",
    "is_slow_machine",
    "active",
    "is_responsive",
    "effective_upload_speed_mbps",
    "effective_download_speed_mbps",
)

#: Fields kept from a joined Chutes row.
CHUTES_KEEP = (
    "chute_id",
    "date",
    "name",
    "usd_amount",
    "invocation_count",
    "total_requests",
    "total_input_tokens",
    "total_output_tokens",
    "average_tps",
    "average_ttft",
)


def fetch(url: str, timeout: float = 180.0, attempts: int = 3) -> Any:
    """GET JSON, retrying a transient failure.

    These are third-party services with no SLA to us, and a single timeout on one
    of five endpoints used to discard the other four. Retry briefly, then let the
    caller decide whether this particular endpoint was essential.

    **`HTTPException` is in the retry list for a measured reason.** On
    2026-10-30 a live run of `/invocations/stats/llm` died with
    ``IncompleteRead(13469752 bytes read, 7978473 more expected)``. That is
    ``http.client.IncompleteRead``, whose MRO is ``HTTPException -> Exception``:
    it is NOT an ``OSError`` and NOT a ``URLError``, so it walked straight
    through the handler below, past `try_fetch` (which only catches
    ``RuntimeError``), and out of `main` as a traceback. On the 21 MB endpoint —
    the largest payload, the likeliest to truncate, and the one that feeds the
    whole ACR-INF tape. A truncated body also decodes as broken JSON, so
    ``ValueError`` belongs here for the same read.

    The point is not the crash, it is WHICH crash: a transient half-read is
    exactly what the three attempts exist for, and it was the one transport
    fault that skipped them.
    """
    req = urllib.request.Request(
        url, headers={"accept": "application/json", "user-agent": "acr-bt-snapshot/0.1"}
    )
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed hosts
                return json.loads(resp.read().decode("utf-8"))
        except (
            urllib.error.URLError,
            urllib.error.HTTPError,
            TimeoutError,
            OSError,
            http.client.HTTPException,  # IncompleteRead on the 21 MB payload
            ValueError,  # a truncated body is also broken JSON
        ) as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"{url} failed after {attempts} attempts: {last!r}") from last


def try_fetch(url: str, label: str, timeout: float = 180.0) -> Any | None:
    """Fetch an endpoint whose absence degrades the snapshot but does not void it."""
    try:
        return fetch(url, timeout=timeout)
    except RuntimeError as exc:
        print(f"  ! {label} unavailable, continuing without it: {exc}")
        return None


def join_chutes(usage: list[dict], stats: list[dict]) -> list[dict]:
    """Join billed USD to the tokens that bought it, on (chute_id, date).

    Only rows with both a positive dollar amount and positive tokens survive: a
    row with tokens but no dollars is a free/subsidised model, and a row with
    dollars but no tokens is a non-LLM chute (image, embedding). Neither has a
    $/1k-token price, and inventing one would be the whole problem.
    """
    by_key = {(r.get("chute_id"), r.get("date")): r for r in stats}
    out: list[dict] = []
    for ur in usage:
        key = (ur.get("chute_id"), ur.get("date"))
        sr = by_key.get(key)
        if sr is None:
            continue
        toks = (sr.get("total_input_tokens") or 0) + (sr.get("total_output_tokens") or 0)
        usd = ur.get("usd_amount") or 0.0
        if toks <= 0 or usd <= 0:
            continue
        merged = {**ur, **sr}
        row = {k: merged.get(k) for k in CHUTES_KEEP}
        row["tokens_total"] = toks
        row["price_per_1k_tokens"] = usd / (toks / 1000.0)
        out.append(row)
    # A total order, so the fixture is byte-stable across runs.
    out.sort(key=lambda r: (str(r["date"]), str(r["chute_id"])))
    return out


def trim_lium(executors: list[dict]) -> list[dict]:
    rows = [{k: e.get(k) for k in LIUM_KEEP} for e in executors]
    rows.sort(key=lambda r: str(r.get("id")))
    return rows


def describe(chutes: list[dict], lium: list[dict], pricing: dict) -> None:
    """Print the two numbers that decide whether the tape path is viable."""
    dates = sorted({r["date"] for r in chutes})
    print("  chutes (netuid 64, inference)")
    print(f"    priceable rows      : {len(chutes)}")
    if dates:
        print(f"    dates               : {dates[0]} .. {dates[-1]}  ({len(dates)} days)")
        last = [r for r in chutes if r["date"] == dates[-1]]
        sellers = len({r["chute_id"] for r in last})
        px = sorted(r["price_per_1k_tokens"] for r in last)
        print(f"    last day            : {len(last)} rows, {sellers} distinct sellers")
        if px:
            mid = px[len(px) // 2]
            print(f"    last-day $/1k median: {mid:.8f}   (min {px[0]:.8f} max {px[-1]:.8f})")
        # The cleaning stage runs Louvain over buyer/seller nodes. One aggregate
        # buyer plus N sellers is a star, and a star is one community.
        nodes = sellers + 1
        verdict = (
            "ALL FLOW ZEROED (single community <= SYBIL_MAX_SIZE)"
            if nodes <= 40
            else "CAP COLLAPSE (single community, every weight x cluster_cap)"
        )
        print(f"    graph nodes         : {nodes}  -> {verdict}")

    print("  lium (netuid 51, GPU rental) — anchors only, not tape")
    hotkeys = {r.get("miner_hotkey") for r in lium if r.get("miner_hotkey")}
    gpus = sum(r.get("gpu_count") or 0 for r in lium)
    avail = sum(r.get("available_gpu_count") or 0 for r in lium)
    px = sorted(r["price_per_gpu"] for r in lium if r.get("price_per_gpu"))
    print(f"    executors           : {len(lium)}   distinct miner_hotkey: {len(hotkeys)}")
    print(f"    GPUs                : {gpus} total, {avail} idle, {gpus - avail} rented")
    if px:
        print(f"    $/GPU-hour          : min {px[0]} median {px[len(px) // 2]} max {px[-1]}")
    print(f"    distinct GPU models : {len({r.get('machine_name') for r in lium})}")

    if pricing:
        print("  chutes /pricing")
        print(f"    tao_usd             : {pricing.get('tao_usd')}")
        print(f"    gpu estimates       : {len(pricing.get('gpu_price_estimates') or {})} models")


def fold_gpu_counts(rows: list[dict]) -> dict[str, int]:
    """chute_id -> its largest observed GPU count.

    The endpoint is named `_history` and returns one row per observation, so a
    chute that was resized appears more than once. The MAXIMUM is the right fold:
    the hedonic question is what hardware footprint this model needs to serve,
    and a model that ever required 8 GPUs is an 8-GPU model.
    """
    out: dict[str, int] = {}
    for r in rows:
        cid = str(r.get("chute_id") or "")
        n = int(r.get("gpu_count") or 0)
        if cid and n > out.get(cid, 0):
            out[cid] = n
    return out


def describe_metagraph(rows: list[dict]) -> None:
    """Seller concentration — the diagnostic that stands in for an attack cost.

    `ChutesSource` publishes a price from single-venue flow but refuses an attack
    cost, because the funding graph is a star and the cleaning stage has nothing
    to compare against. This is the thing an attack cost is ultimately a claim
    about and which IS directly observable: how many independent sellers the
    venue actually has.
    """
    if not rows:
        return
    from collections import Counter

    print("  chutes /miner/metagraph — venue concentration, not an attack cost")
    netuids = Counter(r.get("netuid") for r in rows)
    hot = {r.get("hotkey") for r in rows if r.get("hotkey")}
    per_ck = Counter(r.get("coldkey") for r in rows if r.get("coldkey"))
    counts = list(per_ck.values())
    ips = [str(r.get("ip")) for r in rows if r.get("ip") is not None]
    real = {i for i in ips if i not in ("0", "0.0.0.0", "", "None", "null")}
    print(f"    netuids             : {dict(netuids)}")
    print(f"    hotkeys / coldkeys  : {len(hot)} / {len(per_ck)}")
    print(
        f"    multi-hotkey coldkeys: {sum(1 for c in counts if c > 1)}"
        f"   largest: {max(counts) if counts else 0}"
    )
    print(f"    hotkeys per coldkey : {dict(sorted(Counter(counts).items()))}")
    staked = sum(1 for r in rows if (r.get("stake") or 0) > 0)
    print(f"    hotkeys with stake  : {staked} of {len(rows)}")
    # Said plainly rather than quietly averaged into a score: IP co-location is a
    # standard second sybil signal and it is not available here.
    print(
        f"    ip usable           : {len(real) > 1 and len(real) >= 0.5 * len(hot)}"
        f"   ({len(real)} real of {len(ips)} rows)"
    )


def describe_gpu_counts(gpu_by_chute: dict[str, int], chutes: list[dict]) -> None:
    """How far the GPU-count covariate actually reaches into the priced rows."""
    if not gpu_by_chute:
        return
    from collections import Counter

    priced = {r["chute_id"] for r in chutes}
    hit = priced & set(gpu_by_chute)
    print("  chutes /chutes/gpu_count_history — a hedonic covariate")
    print(f"    chutes with a count : {len(gpu_by_chute)}")
    pct = (100.0 * len(hit) / len(priced)) if priced else 0.0
    print(f"    priced chutes joined: {len(hit)} of {len(priced)}  ({pct:.0f}%)")
    dist = Counter(gpu_by_chute[c] for c in hit)
    print(f"    GPU count of priced : {dict(sorted(dist.items()))}")


def read_taostats(chutes_pricing: dict) -> dict | None:
    """Bittensor chain state, and the two cross-checks worth printing.

    Optional throughout: no key means one line saying so and nothing else. The
    adapter enforces its own per-cycle credit budget, so this cannot run away
    with the month's 10,000 requests.
    """
    from acr_tape import TaostatsClient
    from acr_tape.taostats_client import capture

    c = TaostatsClient()
    print("  taostats (chain state)")
    if c.offline:
        print("    no ACR_TAOSTATS_API_KEY — skipped. Everything above still works.")
        return None

    fx = capture(c)
    px = fx.get("tao_price_usd")
    if px:
        print(f"    TAO/USD             : {px:,.4f}")
        # Chutes prices in TAO as well as USD, so it carries its own rate. Two
        # independent sources for the same number is worth a line.
        chutes_tao = (chutes_pricing or {}).get("tao_usd")
        if chutes_tao:
            drift = abs(px - chutes_tao) / chutes_tao
            print(f"    chutes /pricing says: {chutes_tao:,.4f}   ({drift * 100:.2f}% apart)")
    reg = fx.get("registration") or {}
    if reg.get("rao"):
        print(
            f"    subnet lock cost    : {reg['rao'] / 1e9:,.2f} TAO"
            f"   (block {reg.get('block_number')})"
        )
    for nid, pool in sorted((fx.get("pools") or {}).items()):
        a_in = pool.get("alpha_in_pool") or 0
        t_in = pool.get("tao_in_pool") or 0
        price = (t_in / a_in) if a_in else None
        label = {"64": "chutes", "51": "lium"}.get(nid, f"netuid {nid}")
        print(
            f"    netuid {nid} ({label}) alpha: "
            f"{'—' if price is None else f'{price:.6f} TAO'}"
            f"   (tao_in_pool {t_in:,.0f} / alpha_in_pool {a_in:,.0f})"
        )
    for nid, rec in sorted((fx.get("coldkeys") or {}).items()):
        print(
            f"    netuid {nid} coldkeys  : {rec.get('n_coldkeys')} behind "
            f"{rec.get('n_hotkeys')} hotkeys"
        )
    if c.rate_limited:
        print("    ! rate-limited mid-run (5/min free tier) — some rows may be absent")
    return fx


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"  ✓ wrote {path.relative_to(ROOT)}  ({path.stat().st_size:,} bytes)")


def append_tape(chutes: list[dict], lium: list[dict], captured_on: str | None = None) -> None:
    """Append an observation to the cumulative archive, skipping what is there.

    Both feeds OVERLAP what is already archived -- Chutes usage returns a rolling
    ~12-day window on every call and Lium returns the whole fleet -- so a plain
    append double-counts every day after the first run. Dedupe on a natural key,
    the same read-then-append-only-fresh shape `scripts/x402_capture.py` uses,
    which makes a same-day re-run a no-op instead of a corruption.
    """
    TAPE_DIR.mkdir(parents=True, exist_ok=True)
    # Lium executors carry a price but no date, so an un-stamped archive cannot
    # answer "what did this cost on day X" -- the one question a forward-poll
    # tape exists to answer. Stamp the capture day and key on it.
    stamp = captured_on or datetime.now(UTC).strftime("%Y-%m-%d")
    lium = [{**r, "captured_on": r.get("captured_on") or stamp} for r in lium]

    feeds = (
        ("chutes_usage", chutes, lambda r: (r.get("date"), r.get("chute_id"))),
        ("lium_executors", lium, lambda r: (r.get("captured_on"), r.get("id"))),
    )
    for name, rows, key in feeds:
        path = TAPE_DIR / f"{name}.jsonl"
        seen: set = set()
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    seen.add(key(json.loads(line)))
                except Exception:
                    continue  # a corrupt line must not cost us the whole archive

        fresh = []
        for r in rows:
            k = key(r)
            if k in seen:
                continue
            seen.add(k)
            fresh.append(r)

        with path.open("a", encoding="utf-8") as fh:
            for r in fresh:
                fh.write(json.dumps(r, sort_keys=True) + "\n")
        try:
            shown = path.relative_to(ROOT)
        except ValueError:  # ACR_BT_TAPE_DIR pointed outside the repo
            shown = path
        skipped = len(rows) - len(fresh)
        note = f" ({skipped} already archived)" if skipped else ""
        print(f"  ✓ appended {len(fresh)} of {len(rows)} rows to {shown}{note}")


def seed_from_fixture() -> int:
    """Fold the committed fixtures into the archive. Offline, idempotent.

    The archive starts empty, but the fixtures already hold days that upstream
    has since dropped -- Chutes usage retains only ~12 days, so the oldest
    fixture date is unrecoverable from the network. Seeding once means the
    cumulative series starts at the fixture's first day instead of today.
    """
    try:
        chutes = json.loads((FIXTURE_DIR / "chutes_usage.json").read_text())
        lium = json.loads((FIXTURE_DIR / "lium_executors.json").read_text())
    except (OSError, ValueError) as exc:
        print(f"  ✗ could not read fixtures from {FIXTURE_DIR}: {exc}")
        return 1
    dates = sorted({str(r.get("date")) for r in chutes if r.get("date")})
    print(
        f"seeding archive from {FIXTURE_DIR.name}/ "
        f"({len(chutes)} usage rows, {dates[0] if dates else '?'} -> "
        f"{dates[-1] if dates else '?'}; {len(lium)} executors)"
    )
    # The fixture's Lium snapshot was captured with its newest usage date.
    append_tape(chutes, lium, captured_on=dates[-1] if dates else None)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="fetch and describe, write nothing")
    ap.add_argument(
        "--capture-fixture",
        action="store_true",
        help="refresh the committed offline fixtures under tests/fixtures/bt/",
    )
    ap.add_argument(
        "--seed-from-fixture",
        action="store_true",
        help="fold the committed fixtures into the cumulative archive and exit. "
        "Offline and idempotent; run once so the series starts at the fixture's "
        "oldest day rather than today (upstream only retains ~12 days).",
    )
    ap.add_argument(
        "--taostats",
        action="store_true",
        help="also read Bittensor chain state (TAO/USD, pool reserves, registration "
        "cost). Needs ACR_TAOSTATS_API_KEY; the free tier is 5 requests/minute, so "
        "this spends a handful of credits and may be rate-limited mid-run.",
    )
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    # Before any network call: this path reads only committed fixtures.
    if args.seed_from_fixture:
        return seed_from_fixture()

    # Essential: without both of these there is no realized price and nothing to do.
    try:
        print("fetching chutes /invocations/usage ...")
        usage = fetch(f"{CHUTES_API}/invocations/usage")
        print(f"  {len(usage)} rows")
        print("fetching chutes /invocations/stats/llm (large payload) ...")
        stats = fetch(f"{CHUTES_API}/invocations/stats/llm")
        print(f"  {len(stats)} rows")
    except RuntimeError as exc:
        print(f"  ✗ {exc}")
        print("    These are live third-party services. The offline path reads")
        print("    tests/fixtures/bt/ and does not need this command to succeed.")
        return 1

    # Degradable: each of these enriches the snapshot; none of them voids it.
    print("fetching chutes /pricing ...")
    pricing = try_fetch(f"{CHUTES_API}/pricing", "chutes /pricing") or {}
    print("fetching chutes /v1/models ...")
    models = try_fetch(f"{CHUTES_LLM}/v1/models", "chutes /v1/models") or {}
    print("fetching chutes /miner/metagraph ...")
    metagraph = try_fetch(f"{CHUTES_API}/miner/metagraph", "chutes /miner/metagraph") or []
    print(f"  {len(metagraph)} neurons")
    print("fetching chutes /chutes/gpu_count_history ...")
    gpu_counts = (
        try_fetch(f"{CHUTES_API}/chutes/gpu_count_history", "chutes /chutes/gpu_count_history")
        or []
    )
    print(f"  {len(gpu_counts)} rows")
    print("fetching lium /executors ...")
    executors = try_fetch(f"{LIUM_API}/executors", "lium /executors") or []
    print(f"  {len(executors)} executors")

    chutes = join_chutes(usage, stats)
    lium = trim_lium(executors)
    gpu_by_chute = fold_gpu_counts(gpu_counts)

    print("\n--- measured ---")
    describe(chutes, lium, pricing)
    describe_metagraph(metagraph)
    describe_gpu_counts(gpu_by_chute, chutes)

    taostats: dict | None = None
    if args.taostats:
        taostats = read_taostats(pricing)

    if args.dry_run:
        print("\n  (--dry-run: nothing written)")
        return 0

    if args.capture_fixture:
        print(f"\nwriting fixtures to {FIXTURE_DIR.relative_to(ROOT)}/")
        write_json(FIXTURE_DIR / "chutes_usage.json", chutes)
        write_json(FIXTURE_DIR / "lium_executors.json", lium)
        write_json(FIXTURE_DIR / "chutes_pricing.json", pricing)
        if metagraph:
            # Only the fields the concentration diagnostic reads. The payload
            # carries 20 keys per neuron, most of them operational.
            keep = (
                "hotkey",
                "coldkey",
                "netuid",
                "ip",
                "stake",
                "alpha_stake",
                "tao_stake",
                "incentive",
                "trust",
                "vtrust",
            )
            mg = [{k: r.get(k) for k in keep} for r in metagraph]
            mg.sort(key=lambda r: str(r.get("hotkey")))
            write_json(FIXTURE_DIR / "chutes_metagraph.json", mg)
        if taostats:
            # Response data only — never the key that fetched it. Pinned by
            # test_taostats.py::test_the_fixture_contains_no_credential.
            write_json(FIXTURE_DIR / "taostats.json", taostats)
        if gpu_by_chute:
            # Only the priced chutes. The endpoint knows 19,069 of them and 26
            # carry a price, so keeping all of them is a 1.6 MB committed file to
            # serve 26 useful rows.
            priced_ids = {r["chute_id"] for r in chutes}
            write_json(
                FIXTURE_DIR / "chutes_gpu_counts.json",
                [
                    {"chute_id": k, "gpu_count": v}
                    for k, v in sorted(gpu_by_chute.items())
                    if k in priced_ids
                ],
            )
        model_px = [
            {
                "id": m.get("id"),
                "input_usd_per_1m": (m.get("price") or {}).get("input", {}).get("usd"),
                "output_usd_per_1m": (m.get("price") or {}).get("output", {}).get("usd"),
                "input_tao_per_1m": (m.get("price") or {}).get("input", {}).get("tao"),
                "output_tao_per_1m": (m.get("price") or {}).get("output", {}).get("tao"),
            }
            for m in (models.get("data") or [])
        ]
        model_px.sort(key=lambda r: str(r["id"]))
        write_json(FIXTURE_DIR / "chutes_models.json", model_px)
        return 0

    # Forward-polling path: append today's observation to the committed tape.
    append_tape(chutes, lium)
    return 0


if __name__ == "__main__":
    sys.exit(main())
