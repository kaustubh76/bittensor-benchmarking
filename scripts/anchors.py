#!/usr/bin/env python
"""Real public prices, dated and cited — and the gap they open.

`IndexSpec.reference_level` calls itself "nominal" and carries no citation. It
seeds the simulator's central level, the fleet's quotes and the tape's price pin,
so it has been the scale of everything this project publishes, resting on three
numbers nobody sourced.

This measures them against real public prices. It does **not** change them:
`--fetch` writes `anchors/` and never touches `indices.py`. Re-anchoring moves
the tape sources' price pin and breaks comparability with the prints already on
chain, so it is a separate decision that deserves its own analysis — and, usefully,
it is one this harness is immune to: the simulator draws notional first and
derives size, so every bp-denominated quality metric is exactly scale-invariant.

    uv run python scripts/anchors.py --fetch     # network; manual, never CI
    uv run python scripts/anchors.py --report    # regenerate anchors/GAP.md
    uv run python scripts/anchors.py --check     # offline; CI-safe

**What --check asserts is that the gap is DECLARED, not that it is small.** A
test that fails on a 1000x gap is red forever and teaches its operator to ignore
it. So it checks five things that can each go wrong silently: that every index
has an anchor at all; that the `reference_level` the anchor was measured against
is still the one in `indices.py` (this goes red the moment someone edits that
file without re-anchoring); that the declared ratio recomputes from the file's
own rows; that enough of the basket actually PRICED; and that the anchor is not
old enough for `/par` to stop trusting it. The size of the gap is held by a
ratchet — widening fails, narrowing is re-blessed by lowering the ceiling.

The last two were added after the first three stayed green through a real
regression. ACR-INF had lost five of its ten rows — every Anthropic, Google and
Mistral model in the basket — and the gap was still declared and still
recomputed, because both of those are properties of the rows that remain. What
makes coverage and age load-bearing rather than tidy is `par.Basket.usable`:
when a basket is unusable or stale, `/par` prices a stranger's bill against the
FLEET's own quotes instead, a scale this file's own GAP.md puts 10x to 1250x off
market. The failure is not a missing number. It is a confident wrong one, on the
endpoint whose entire claim is that it compares you to somebody else.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

from acr_core import ALL_INDEX_IDS, spec_for

# The two numbers the SERVING path uses, imported rather than restated. `par`
# decides what counts as a market and when a basket stops being a price; a
# second copy of either here would be free to drift from the one that acts.
#
# AGE_WARN_FRACTION used to be DECLARED here, and that is why `/par` spent the
# whole six-day warning window answering "ok": the number that decides when a
# basket is worth mentioning lived in a script, so the only thing that ever
# consulted it was `make ci` — against the anchors in the REPO, while the press
# reads the ones baked into its image. It now lives beside the cliff it divides
# and is imported, on the same argument the line below already makes.
from index_api.par import AGE_WARN_FRACTION, ANCHOR_MAX_AGE_S, MIN_SELLERS

ANCHOR_SCHEMA = "acr.anchor/1"
#: Bumped when the aggregation RULE changes, like POLICY_VERSION in cleaning.py.
#: One idiom in this repo for "the rule moved", not two.
#:
#: 2 (2026-10-29): ACR-GPU became a MIXED basket. Rows declaring a
#: `machine_name` are priced live from Lium (Bittensor subnet 51) as the
#: median over that accelerator class on the on-demand tier; the rate-card
#: rows stay curated. The basket's old note claimed no GPU cloud published a
#: clean unauthenticated price JSON, which Lium falsifies.
TOOL_VERSION = 2

ROOT = Path(__file__).resolve().parents[1]
ANCHOR_DIR = ROOT / "anchors"
BASKET_DIR = ANCHOR_DIR / "_basket"

OPENROUTER_URL = "https://openrouter.ai/api/v1/models"


# ── fetch ───────────────────────────────────────────────────────────────────


def fetch_openrouter(timeout: float = 30.0) -> tuple[list[dict], str]:
    """The catalogue, and a digest of the exact bytes it came from.

    The digest is what makes the fetch auditable without committing 431 models:
    two anchors claiming the same source on the same day should agree on it.
    """
    req = urllib.request.Request(OPENROUTER_URL, headers={"User-Agent": "acr-anchors"})
    with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
        raw = r.read()
    sha = "0x" + hashlib.sha256(raw).hexdigest()
    return list(json.loads(raw).get("data") or []), sha


def load_basket(index_id: str) -> dict:
    return json.loads((BASKET_DIR / f"{index_id}.json").read_text())


def _blend(pricing: dict, blend: dict) -> float | None:
    """USD per token under the basket's prompt:completion split."""
    try:
        p = float(pricing.get("prompt") or 0.0)
        c = float(pricing.get("completion") or 0.0)
    except (TypeError, ValueError):
        return None
    if p <= 0:
        return None
    return blend["prompt"] * p + blend["completion"] * c


def aggregate(values: list[float], rule: str = "median", trim: float = 0.0) -> dict:
    """The aggregate, with its dispersion beside it.

    Dispersion is not decoration. A basket whose rows span 13x has no single
    price, and a bare median over it is an average of two different markets — so
    the file states the spread on its face rather than letting one number imply
    a consensus that is not there.
    """
    vs = sorted(values)
    if not vs:
        return {"value": None, "n": 0}
    if trim > 0 and len(vs) > 2:
        k = int(len(vs) * trim)
        vs = vs[k : len(vs) - k] or vs
    value = statistics.median(vs) if rule == "median" else statistics.fmean(vs)
    return {
        "value": value,
        "n": len(values),
        "min": vs[0],
        "p25": vs[len(vs) // 4],
        "p75": vs[(3 * len(vs)) // 4],
        "max": vs[-1],
        "spread_ratio": (vs[-1] / vs[0]) if vs[0] > 0 else None,
    }


def gap_vs_reference(index_id: str, value: float | None) -> dict:
    """How far the nominal reference sits from the measured market."""
    ref = spec_for(index_id).reference_level
    if not value:
        return {"ratio": None, "direction": "unmeasured"}
    ratio = ref / value
    return {
        "ratio": ratio,
        "direction": "reference_above_market" if ratio >= 1 else "reference_below_market",
    }


def build_live_anchor(index_id: str, models: list[dict], sha: str) -> dict:
    """ACR-INF: named rows priced from the live catalogue."""
    basket = load_basket(index_id)
    by_id = {m.get("id"): m for m in models}
    now = datetime.now(UTC).isoformat(timespec="seconds")
    rows, missing = [], []
    for want in basket["rows"]:
        m = by_id.get(want["id"])
        price = _blend(m.get("pricing") or {}, basket["rule"]["blend"]) if m else None
        if price is None:
            # Recorded, never dropped. A basket that quietly shrinks re-medians a
            # different population and reports it as a market move.
            missing.append({"id": want["id"], "reason": "absent from catalogue or unpriced"})
            continue
        rows.append(
            {
                "id": want["id"],
                "label": want["label"],
                "price_usd_per_unit": price * 1000.0,
                # The vendor's own strings. If OpenRouter ever moves from $/token to
                # $/Mtok, a numeric-only file is silently wrong by 1e6 and this makes
                # the change visible in the diff.
                "raw": dict(m.get("pricing") or {}),
                "quality": {"model_class": want.get("model_class")},
                "source": {
                    "kind": "http",
                    "url": OPENROUTER_URL,
                    "json_path": f"data[id={want['id']}].pricing",
                    "retrieved_at": now,
                },
            }
        )
    return _assemble(
        index_id,
        basket,
        rows,
        missing,
        now,
        {
            "response_sha256": sha,
            "n_models_in_catalogue": len(models),
            "n_models_priced": sum(1 for m in models if (m.get("pricing") or {}).get("prompt")),
        },
    )


LIUM_URL = "https://lium.io/api/executors"


def fetch_lium(timeout: float = 30.0, attempts: int = 3) -> tuple[list[dict], str]:
    """Bittensor subnet 51 GPU rentals, and a digest of the bytes they came from.

    Retries, unlike ``fetch_openrouter``, because this is a second network
    dependency and one flaky endpoint should not void a whole re-anchor. Same
    sha256-of-raw-bytes provenance contract: two anchors claiming this source on
    the same day should agree on the digest.
    """
    req = urllib.request.Request(LIUM_URL, headers={"User-Agent": "acr-anchors"})
    last: Exception | None = None
    for i in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
                raw = r.read()
            payload = json.loads(raw)
            if not isinstance(payload, list):
                # A reshaped 200 (an envelope, an error body) used to yield zero
                # rows with a perfectly VALID sha256, and `--fetch` would write a
                # 4-row anchor that only `--check` later rejected. Raising puts it
                # inside the retry below, where every other upstream fault lives.
                raise ValueError(
                    f"expected a JSON array of executors, got {type(payload).__name__}"
                )
            return payload, "0x" + hashlib.sha256(raw).hexdigest()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
            last = exc
            if i < attempts - 1:
                time.sleep(2.0 * (i + 1))
    raise RuntimeError(f"{LIUM_URL} failed after {attempts} attempts: {last}") from last


def lium_medians(executors: list[dict], rule: dict) -> tuple[dict[str, dict], dict[str, int]]:
    """``machine_name -> {usd_per_gpu_hour, n}`` for the declared tier only.

    Groups rather than emitting one row per executor, and that is the whole
    design decision. 94 raw rows would give the basket a 44x spread — an
    aggregate over two different markets, which `aggregate`'s own docstring warns
    is not a price — and would turn every `/par` response into a 94-quote payload
    whose `best_seller` is an opaque executor UUID. One row per accelerator class
    keeps the basket comparable, the payload readable, and each row's dispersion
    recorded in `raw`.
    """
    tier = rule.get("lium_tier", "secure")
    want = tuple(rule.get("lium_accelerators") or ())
    counts = {"total": len(executors), "wrong_tier": 0, "other_accelerator": 0, "unpriced": 0}
    groups: dict[str, list[float]] = {}
    for e in executors:
        name = str(e.get("machine_name") or "")
        if e.get("tier") != tier:
            counts["wrong_tier"] += 1
            continue
        if want and not any(k in name for k in want):
            counts["other_accelerator"] += 1
            continue
        price = e.get("price_per_gpu")
        if not price or float(price) <= 0:
            counts["unpriced"] += 1
            continue
        groups.setdefault(name, []).append(float(price))
    out = {
        name: {
            "usd_per_gpu_hour": statistics.median(px),
            "n": len(px),
            "min": min(px),
            "max": max(px),
        }
        for name, px in groups.items()
    }
    return out, counts


def build_curated_anchor(index_id: str) -> dict:
    """ACR-GPU / ACR-DATA: published rate cards, each row carrying its URL.

    ACR-GPU is now mixed: rows declaring a ``machine_name`` are priced live from
    Lium (Bittensor subnet 51), the rest stay curated rate cards. A row the fetch
    cannot price becomes a ``missing`` entry rather than being dropped — a basket
    that quietly shrinks re-medians a different population and reports it as a
    market move.
    """
    basket = load_basket(index_id)
    now = datetime.now(UTC).isoformat(timespec="seconds")
    field = "usd_per_gpu_hour" if index_id == "ACR-GPU" else "usd_per_gb"
    divisor = 3600.0 if index_id == "ACR-GPU" else 1000.0

    live: dict[str, dict] = {}
    counts: dict[str, int] = {}
    sha = ""
    wants_lium = any(r.get("machine_name") for r in basket["rows"])
    if wants_lium:
        executors, sha = fetch_lium()
        live, counts = lium_medians(executors, basket.get("rule") or {})
    min_obs = int((basket.get("rule") or {}).get("lium_min_executors") or 0)

    rows: list[dict] = []
    missing: list[dict] = []
    thin: list[str] = []
    for r in basket["rows"]:
        machine = r.get("machine_name")
        if machine:
            got = live.get(machine)
            if got is None:
                missing.append(
                    {"id": r["id"], "reason": f"no priced {machine!r} on the declared tier today"}
                )
                continue
            # A median over one executor is that executor's ASK, not a price,
            # and SN51 inventory is ~93% idle. `NVIDIA H100 80GB HBM3` went 6
            # secure executors to 1 inside 24h on 2026-10-30, so this is the
            # normal state of a thin class rather than an outage.
            #
            # It is still PRICED, and that is the deliberate half of the
            # decision. Dropping it would put the basket under `rule.min_priced`
            # (10 of 10 today), which flips `par.Basket.usable` to false and
            # hands a stranger's bill to the FLEET's own quotes -- a scale
            # anchors/GAP.md puts 10x to 1250x off market. One disclosed thin
            # row inside a ten-row median is strictly better than no benchmark.
            # What was missing was the DISCLOSURE, so the row says so, the
            # provenance counts them, and `_check_thin` ratchets the count.
            is_thin = bool(min_obs) and got["n"] < min_obs
            if is_thin:
                thin.append(r["id"])
            rows.append(
                {
                    "id": r["id"],
                    "label": r["label"],
                    "price_usd_per_unit": got["usd_per_gpu_hour"] / divisor,
                    "raw": {
                        field: got["usd_per_gpu_hour"],
                        "n_executors": got["n"],
                        "min_usd_per_gpu_hour": got["min"],
                        "max_usd_per_gpu_hour": got["max"],
                    },
                    "quality": (
                        {"machine_name": machine, "thin": True, "min_executors": min_obs}
                        if is_thin
                        else {"machine_name": machine}
                    ),
                    "source": {
                        "kind": "http",
                        "url": LIUM_URL,
                        "json_path": f"[machine_name={machine}].price_per_gpu (median)",
                        "retrieved_at": now,
                    },
                }
            )
            continue
        rows.append(
            {
                "id": r["id"],
                "label": r["label"],
                "price_usd_per_unit": r[field] / divisor,
                "raw": {field: r[field]},
                "quality": {},
                "source": {
                    "kind": "curated",
                    "url": r["url"],
                    "quoted": r["quoted"],
                    "retrieved_at": now,
                },
            }
        )

    provenance: dict = {}
    if wants_lium:
        provenance = {
            "lium_response_sha256": sha,
            "n_executors": counts.get("total", 0),
            "n_excluded_wrong_tier": counts.get("wrong_tier", 0),
            "n_excluded_other_accelerator": counts.get("other_accelerator", 0),
            "n_excluded_unpriced": counts.get("unpriced", 0),
            "n_accelerator_classes_priced": len(live),
            # Counted and NAMED. A count alone tells a reader the basket
            # thinned; the ids tell them which market did, which is the only
            # version of this a human can act on.
            "n_accelerator_classes_thin": len(thin),
            "thin_classes": thin,
            "min_executors": min_obs,
        }
    return _assemble(index_id, basket, rows, missing, now, provenance)


def _assemble(index_id, basket, rows, missing, now, provenance) -> dict:
    agg = aggregate(
        [r["price_usd_per_unit"] for r in rows],
        basket["rule"]["aggregate"],
        basket["rule"].get("trim", 0.0),
    )
    agg["n_requested"] = len(basket["rows"])
    gap = gap_vs_reference(index_id, agg["value"])
    ratio = gap["ratio"]
    return {
        "schema": ANCHOR_SCHEMA,
        "tool_version": TOOL_VERSION,
        "index_id": index_id,
        "unit": basket["unit"],
        "fetched_at": now,
        "basket_version": basket["version"],
        "selection": basket["rule"],
        "rows": rows,
        "missing": missing,
        "aggregate": agg,
        # Copied in, so the file stays self-contained evidence of the gap AT
        # THIS DATE and does not become a lie after a later re-anchor.
        "reference_level": spec_for(index_id).reference_level,
        "gap": gap,
        # The ratchet: widening fails --check, narrowing is re-blessed by
        # lowering this. 15% of headroom so a normal market move is not an alarm.
        "ceiling": {
            "ratio": round(ratio * 1.15, 1) if ratio else None,
            "set_at": now[:10],
            "note": "measured value plus 15%",
        },
        "provenance": provenance,
    }


# ── read back ───────────────────────────────────────────────────────────────


def latest_anchor(index_id: str) -> dict | None:
    d = ANCHOR_DIR / index_id
    files = sorted(d.glob("*.json")) if d.exists() else []
    return json.loads(files[-1].read_text()) if files else None


def recompute_gap(doc: dict) -> float | None:
    """The gap, recomputed from the file's own rows — catches a hand-edited
    aggregate, which is the one way a committed anchor can lie."""
    agg = aggregate(
        [r["price_usd_per_unit"] for r in doc["rows"]],
        doc["selection"]["aggregate"],
        doc["selection"].get("trim", 0.0),
    )
    return gap_vs_reference(doc["index_id"], agg["value"])["ratio"]


C_HUMAN_BASKET = ROOT / "anchors" / "_basket" / "C-HUMAN.json"


def check_c_human() -> list[str]:
    """Validate the C_human basket, which is NOT an index and so is not enumerated.

    It has to be checked explicitly, and that is the whole point. `--check` and
    `--report` both walk ALL_INDEX_IDS, so a file sitting in anchors/ that is not
    an index is invisible to them: you could add it, run `make anchors-check`,
    get a clean green, and have nothing validate the number that scales the
    entire human-denominated bound. Green means "everything I enumerate is
    fine", never "everything is fine" — the same shape as a validator that
    checks the first match of a pattern and reports on the file.
    """
    fails: list[str] = []
    if not C_HUMAN_BASKET.exists():
        return [
            f"C-HUMAN: {C_HUMAN_BASKET.name} is missing — the human bound would "
            f"fall back to a constant, which is what anchors/ exists to prevent"
        ]
    try:
        basket = json.loads(C_HUMAN_BASKET.read_text())
    except Exception as exc:
        return [f"C-HUMAN: basket does not parse ({type(exc).__name__})"]

    # The quantity sentence is load-bearing, not decoration: the natural reading
    # of "cost of a verified human" is the price of Orb verification, which is
    # free and is the wrong number by orders of magnitude.
    if not str(basket.get("quantity") or "").strip():
        fails.append("C-HUMAN: basket does not say WHICH quantity it measures")

    rule = basket.get("rule") or {}
    rows = basket.get("rows") or []
    if rule.get("aggregate") == "floor":
        if not isinstance(rule.get("floor_usd"), int | float) or rule["floor_usd"] <= 0:
            fails.append("C-HUMAN: floor aggregate with no usable floor_usd")
        if (
            str(basket.get("status")) == "unsourced"
            and not str(basket.get("finding") or "").strip()
        ):
            fails.append("C-HUMAN: status is 'unsourced' but no finding explains why")
    elif not rows:
        fails.append("C-HUMAN: no floor declared and no rows to aggregate")

    for row in rows:
        if not row.get("url") or not row.get("quoted"):
            fails.append(f"C-HUMAN: row {row.get('id')!r} lacks a url or a quoted figure")

    # Parity: the estimator must price from THIS file, not from a constant that
    # happens to agree with it today.
    try:
        from acr_estimator.human_caps import load_cost_per_human

        priced, status, _ = load_cost_per_human(C_HUMAN_BASKET)
    except Exception as exc:
        fails.append(f"C-HUMAN: human_caps cannot price from the basket ({exc})")
    else:
        want = rule.get("floor_usd") if rule.get("aggregate") == "floor" else None
        if want is not None and abs(priced - float(want)) > 1e-9:
            fails.append(f"C-HUMAN: human_caps prices at {priced} but the basket says {want}")
        if status != str(basket.get("status")):
            fails.append("C-HUMAN: human_caps reports a different status than the basket")
    return fails


def check_anchors(indices=ALL_INDEX_IDS) -> list[str]:
    fails: list[str] = check_c_human()
    for index_id in indices:
        doc = latest_anchor(index_id)
        if doc is None:
            fails.append(f"{index_id}: no anchor — run `make anchors-fetch`")
            continue
        live_ref = spec_for(index_id).reference_level
        if doc["reference_level"] != live_ref:
            fails.append(
                f"{index_id}: indices.py reference_level is {live_ref} but the anchor "
                f"was measured against {doc['reference_level']} — re-anchor, or the "
                f"published gap describes a level that no longer exists"
            )
        declared = doc["gap"]["ratio"]
        actual = recompute_gap(doc)
        if declared is not None and actual is not None and abs(declared - actual) > 1e-6:
            fails.append(
                f"{index_id}: declared gap {declared:.1f} does not recompute "
                f"from its own rows ({actual:.1f})"
            )
        ceiling = (doc.get("ceiling") or {}).get("ratio")
        if declared is not None and ceiling is not None and declared > ceiling:
            fails.append(
                f"{index_id}: gap {declared:.1f} widened past its ceiling "
                f"{ceiling} — re-anchor, or raise the ceiling deliberately"
            )
        fails += _check_coverage(index_id, doc)
        fails += _check_thin(index_id, doc)
        fails += _check_age(index_id, doc)
    return fails


def _check_coverage(index_id: str, doc: dict) -> list[str]:
    """How much of the basket actually priced — which nothing asserted.

    Measured 2026-10-07: ACR-INF carried 5 priced rows of 10 requested, down
    from 6, and every one of the five that dropped out was an Anthropic, Google
    or Mistral model. `--check` was green throughout. It checks that the gap is
    DECLARED and that it RECOMPUTES, and both of those are true of a basket
    that has quietly lost half its market.

    This is not cosmetic, because of a coupling in the serving path:
    `par.Basket.usable` requires an empty status, and `/par` hands a stranger's
    bill to the FLEET's own quotes the moment the basket stops being usable. So
    a thinning basket does not fail — it reprices an outsider's invoice against
    ourselves, on a scale `anchors/GAP.md` puts 10x to 1250x off market. The
    whole claim of that endpoint is that it benchmarks against observed
    third-party prices.

    Two thresholds, and they answer different questions. `min_priced` lives in
    the basket's own `rule` block because a minimum coverage IS a selection
    rule, and it ratchets exactly like `ceiling`. `MIN_SELLERS` is imported from
    `par` rather than restated: it is the number below which that module refuses
    to call anything benchmarked at all, and a basket under it is not a market.
    """
    agg = doc.get("aggregate") or {}
    n = agg.get("n")
    requested = agg.get("n_requested")
    if not isinstance(n, int):
        return [
            f"{index_id}: the anchor declares no row count — `aggregate.n` is "
            f"{n!r}, so coverage cannot be checked and neither can the gap"
        ]

    out: list[str] = []
    if n < MIN_SELLERS:
        out.append(
            f"{index_id}: {n} priced row(s) is below par.MIN_SELLERS ({MIN_SELLERS}); "
            f"that is not a market, and /par would compare a price to itself"
        )
    floor = (load_basket(index_id).get("rule") or {}).get("min_priced")
    if not isinstance(floor, int):
        out.append(
            f"{index_id}: its basket declares no `rule.min_priced`, so the "
            f"coverage ratchet is not holding anything"
        )
    elif n < floor:
        out.append(
            f"{index_id}: {n} of {requested} rows priced, below the declared floor of "
            f"{floor} — re-fetch, or lower `rule.min_priced` in "
            f"anchors/_basket/{index_id}.json on purpose"
        )
    return out


def thin_classes(index_id: str, doc: dict) -> list[str] | None:
    """Which accelerator classes in ``doc`` priced off too few sellers.

    ``None`` when the basket declares no floor, which is every basket but
    ACR-GPU. Reads `provenance.thin_classes` when the anchor carries it and
    falls back to recomputing from `rows[].raw.n_executors`, which every Lium
    row has carried since the first fetch — so an anchor written before this
    existed is re-read rather than waved through as zero.
    """
    rule = load_basket(index_id).get("rule") or {}
    floor = rule.get("lium_min_executors")
    if not floor:
        return None
    prov = doc.get("provenance") or {}
    named = prov.get("thin_classes")
    if isinstance(named, list):
        return [str(x) for x in named]
    return [
        str(r.get("id"))
        for r in (doc.get("rows") or [])
        if isinstance(r, dict)
        and isinstance((r.get("raw") or {}).get("n_executors"), int)
        and r["raw"]["n_executors"] < int(floor)
    ]


def _check_thin(index_id: str, doc: dict) -> list[str]:
    """How many accelerator classes priced off too few sellers to be a market.

    `_check_coverage` above answers "how many rows priced". This answers the
    question one level down, which that check cannot see: a row can price and
    still be one executor's ask. `NVIDIA H100 80GB HBM3` carried 6 secure
    executors when this basket was first fetched and 1 a day later, and
    `aggregate.n` read 10 of 10 the whole time.

    A ratchet, like `ceiling` and `rule.min_priced`, and for the same reason:
    the number is allowed to be above zero because SN51 inventory genuinely is
    thin and ~93% idle, but it is not allowed to GROW without somebody deciding
    that it may. A basket with no `lium_min_executors` is not checked at all,
    which leaves every non-Lium basket exactly as it was.
    """
    names = thin_classes(index_id, doc)
    if names is None:
        return []
    rule = load_basket(index_id).get("rule") or {}
    limit = rule.get("lium_max_thin")
    n_thin = len(names)
    if not isinstance(limit, int):
        return [
            f"{index_id}: its basket declares `lium_min_executors` but no "
            f"`lium_max_thin`, so {n_thin} thin class(es) are measured and "
            f"nothing holds the number down"
        ]
    if n_thin > limit:
        named = ", ".join(names) or "see rows[].quality.thin"
        return [
            f"{index_id}: {n_thin} accelerator class(es) priced off fewer than "
            f"{rule['lium_min_executors']} executors ({named}), above the declared "
            f"ceiling of {limit} — a median over one executor is that executor. "
            f"Re-fetch, or raise `rule.lium_max_thin` in "
            f"anchors/_basket/{index_id}.json on purpose"
        ]
    return []


def _age_days(doc: dict) -> float:
    """Days since the anchor was measured, or `inf` when it cannot be read.

    Infinity rather than 0.0 on an unparseable date, because 0.0 is the value a
    FRESH anchor has: a clock that reads "brand new" when it has in fact
    stopped is the shape of `time.monotonic()` being boot-relative, and this
    repo has already been bitten by a "never" that arrived as 0.0.
    """
    try:
        fetched = datetime.fromisoformat(str(doc.get("fetched_at"))).timestamp()
    except ValueError:
        return float("inf")
    return (datetime.now(UTC).timestamp() - fetched) / 86_400


def _check_age(index_id: str, doc: dict) -> list[str]:
    """Fail BEFORE the product starts rerouting, not after.

    `par.ANCHOR_MAX_AGE_S` is the cliff: one second past it `market_basket`
    returns status STALE, `usable` goes false, and `/par` silently switches a
    stranger's bill onto the fleet scale. Nothing was watching that clock, and
    `--fetch` is documented as "network; manual, never CI", so nothing refreshes
    these on a timer either.

    So the threshold here is a FRACTION of the cliff — red with about six days
    of warning, which is the difference between a gate that tells you to
    re-anchor and a gate that tells you the last fortnight of answers were
    measured against the wrong market. Imported rather than restated: a second
    copy of "30 days" would be free to drift from the one that does the work.
    """
    try:
        fetched = datetime.fromisoformat(str(doc.get("fetched_at"))).timestamp()
    except ValueError:
        return [
            f"{index_id}: `fetched_at` is not a timestamp ({doc.get('fetched_at')!r}), "
            f"so its staleness is unknowable"
        ]
    age = datetime.now(UTC).timestamp() - fetched
    limit = AGE_WARN_FRACTION * ANCHOR_MAX_AGE_S
    if age > limit:
        return [
            f"{index_id}: the anchor is {age / 86_400:.1f} days old and /par stops "
            f"trusting it at {ANCHOR_MAX_AGE_S / 86_400:.0f} — re-fetch. Past that "
            f"point a bill from outside the fleet is priced against the fleet."
        ]
    return []


def render_gap_report(docs: list[dict]) -> str:
    lines = [
        "# The anchor gap",
        "",
        "*Generated by `make anchors-report`. Do not hand-edit.*",
        "",
        '`IndexSpec.reference_level` describes itself as "nominal" and cites nothing.',
        "It sets the scale of every price this project publishes. These are the real",
        "public prices it sits against, and the distance between them.",
        "",
        "**The reference levels are not being changed.** They seed the simulator, the",
        "fleet's quotes and the tape's price pin, so moving them would break",
        "comparability with the prints already on chain. That is a separate decision.",
        "What this file does is stop the gap being invisible.",
        "",
        "The quality metrics are immune to it either way: the simulator draws notional",
        "first and derives size, so every bp-denominated figure is exactly",
        "scale-invariant — measured, by re-anchoring ACR-INF 667x and watching",
        "`attack_acr_err_bp` not move at all.",
        "",
        "| index | reference | market anchor | gap | rows | spread | as of |",
        "|---|---|---|---|---|---|---|",
    ]
    for d in docs:
        a, g = d["aggregate"], d["gap"]
        spread = a.get("spread_ratio")
        lines.append(
            f"| `{d['index_id']}` | {d['reference_level']:g} {d['unit']} | "
            f"{a['value']:.6g} | **{g['ratio']:.0f}x** | {a['n']}/{a['n_requested']} | "
            f"{spread:.1f}x | {d['fetched_at'][:10]} |"
        )
    lines += [
        "",
        "## Why the gap is not one number",
        "",
        "It is a function of the aggregation rule, and the rule swings it several-fold.",
        "For ACR-INF the same catalogue gives roughly 1000x on a prompt-only median,",
        "588x on the 75/25 blend this basket uses, and 335x on a trimmed mean over",
        "every priced model. A gap quoted without its rule is not a measurement, so",
        "each anchor records its `selection` and every row's own source.",
        "",
        "## Per index",
        "",
    ]
    for d in docs:
        lines += [f"### {d['index_id']} — {d['unit']}", ""]
        b = load_basket(d["index_id"])
        lines += [b["note"], "", "| row | price | source |", "|---|---|---|"]
        for r in d["rows"]:
            src = r["source"].get("quoted") or r["source"].get("json_path") or ""
            lines.append(
                f"| {r['label']} | {r['price_usd_per_unit']:.6g} | [{src}]({r['source']['url']}) |"
            )
        if d["missing"]:
            lines += [
                "",
                "Requested but absent on this date: "
                + ", ".join(f"`{m['id']}`" for m in d["missing"])
                + ". A basket that shrinks silently re-medians a different population.",
            ]
        lines.append("")
    return "\n".join(lines) + "\n"


def check_live(host: str, unit: str = "$/1k tokens") -> list[str]:
    """Ask a RUNNING PRESS how old the basket it is actually serving is.

    WHY THIS EXISTS, AND WHY `--check` CANNOT ANSWER IT. `--check` reads the
    anchors in this working tree. The press reads the ones COPYd into its image
    at build time (`Dockerfile`), so the two are the same file only until
    somebody re-anchors — and nothing couples them. On 2026-10-12 production
    ran images dated 10-08 and 10-09 while CI was green on `main`: identical
    today, free to diverge tomorrow, and the divergence would be invisible
    because the gate was asking the wrong artifact. A gate's witness has to be
    the thing that serves.

    Deliberately NOT in CI, which is why it reads a host rather than a fixture:
    the presses are free-tier, they cold-start, and a gate that goes red
    because a service was asleep teaches its operator to ignore it. It is a
    `make` target an owner runs, like `make verify-drift`.

    STRICT ON AN UNREADABLE HOST. "I could not ask" is a failure here, not a
    pass — the opposite default to a report, and the same correction
    `VERIFY_DRIFT_STRICT=1` exists to make.
    """
    base = host.rstrip("/")
    url = f"{base}/par?unit={urllib.parse.quote(unit)}&billed_usdc=1&quantity=1000"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "acr-anchors/1"})
        with urllib.request.urlopen(req, timeout=90) as r:  # noqa: S310 — our own host
            doc = json.loads(r.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return [
            f"{base}: could not be read ({exc}), so its basket cannot be "
            f"shown to be fresh. Wake it (curl {base}/health) and re-run."
        ]

    b = doc.get("basket") or {}
    age_s, max_age_s = b.get("age_s"), b.get("max_age_s")
    warn_s = b.get("warn_after_s")
    if age_s is None or not max_age_s:
        return [
            f"{base}: serves no `basket.age_s`/`max_age_s`, so it predates "
            f"the fields that state its own clock. Its image is older than "
            f"2026-10-12 and nothing here can date the basket it is using."
        ]

    days, limit = age_s / 86_400, max_age_s / 86_400
    print(f"  {base}")
    print(
        f"    basket  {b.get('index_id')} · {b.get('rows')}/{b.get('requested')} rows "
        f"· {days:.1f}d old of {limit:.0f}d · status {b.get('status')!r}"
    )
    print(f"    priced  against {doc.get('benchmarked_against')!r}")

    if age_s > max_age_s:
        return [
            f"{base}: the SERVED basket is {days:.1f} days old against its own "
            f"{limit:.0f}-day limit, so this host is already pricing bills from "
            f"outside the fleet against the fleet. Rebuild its image."
        ]
    if warn_s and age_s > warn_s:
        return [
            f"{base}: the SERVED basket is {days:.1f} days old and this host stops "
            f"trusting it at {limit:.0f}. Re-anchor and rebuild the image before "
            f"then, or every answer silently moves onto our own scale."
        ]
    return []


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--fetch", action="store_true", help="hit the network and write today's anchors"
    )
    ap.add_argument("--report", action="store_true", help="regenerate anchors/GAP.md")
    ap.add_argument("--check", action="store_true", help="offline assertions (CI-safe)")
    ap.add_argument(
        "--check-live",
        metavar="HOST",
        default=None,
        help="ask a running press how old the basket it SERVES is (network; manual, never CI)",
    )
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.check_live:
        fails = check_live(args.check_live)
        if fails:
            print("  ✗ served anchors:")
            for f in fails:
                print(f"    {f}")
            return 1
        print("  ✓ the basket this host SERVES is inside its own window")
        return 0

    if args.fetch:
        models, sha = fetch_openrouter()
        print(f"  openrouter: {len(models)} models, sha {sha[:18]}…")
        # Isolated per index: ACR-GPU reaches Lium over the network, and an
        # outage there used to abort the loop AFTER writing ACR-INF and BEFORE
        # ACR-DATA -- a half-written anchor set, which is worse than none.
        failed: list[str] = []
        for index_id in ALL_INDEX_IDS:
            try:
                doc = (
                    build_live_anchor(index_id, models, sha)
                    if index_id == "ACR-INF"
                    else build_curated_anchor(index_id)
                )
            except Exception as exc:  # noqa: BLE001 — one index must not void the rest
                failed.append(index_id)
                print(f"  ✗ {index_id:<9} not written: {str(exc)[:140]}")
                continue
            out = ANCHOR_DIR / index_id / f"{doc['fetched_at'][:10]}.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n")
            a, g = doc["aggregate"], doc["gap"]
            print(
                f"  {index_id:<9} anchor {a['value']:.6g} {doc['unit']:<14} "
                f"reference {doc['reference_level']:g}  gap {g['ratio']:.0f}x  "
                f"({a['n']}/{a['n_requested']} rows)"
            )
        print("\n  reference_level was NOT changed. Re-anchoring is a separate decision.")
        if failed:
            print(f"  ✗ {len(failed)} of {len(ALL_INDEX_IDS)} not written: {', '.join(failed)}")
            return 1
        return 0

    if args.report:
        docs = [d for d in (latest_anchor(i) for i in ALL_INDEX_IDS) if d]
        if not docs:
            print("  ✗ no anchors to report — run `make anchors-fetch`")
            return 1
        (ANCHOR_DIR / "GAP.md").write_text(render_gap_report(docs))
        print(f"  wrote anchors/GAP.md ({len(docs)} index(es))")
        return 0

    fails = check_anchors()
    if fails:
        print("  ✗ anchors:")
        for f in fails:
            print(f"    {f}")
        return 1
    for index_id in ALL_INDEX_IDS:
        d = latest_anchor(index_id)
        agg = d.get("aggregate") or {}
        # Coverage and age are PRINTED, not merely passed, for the reason the
        # C-HUMAN line below gives: a check whose success is silent cannot be
        # told apart from a check that never ran. ACR-INF reads 5/10 today, and
        # a reader should see that number rather than infer it from a tick.
        # The thin count rides on the SAME line as coverage because they are
        # one fact read at two depths: 10/10 rows priced says nothing about a
        # row that priced off one seller, and that was precisely the blind spot.
        names = thin_classes(index_id, d)
        thin_note = (
            f", {len(names)} thin of "
            f"{(d.get('provenance') or {}).get('n_accelerator_classes_priced')} classes"
            if names is not None
            else ""
        )
        print(
            f"  ✓ {index_id:<9} gap {d['gap']['ratio']:.0f}x declared and reproducible "
            f"(ceiling {d['ceiling']['ratio']}, as of {d['fetched_at'][:10]}); "
            f"{agg.get('n')}/{agg.get('n_requested')} rows priced{thin_note}, "
            f"{_age_days(d):.1f}d old of {ANCHOR_MAX_AGE_S / 86_400:.0f}d"
        )
    # Printed explicitly. A check whose success is silent cannot be distinguished
    # from a check that never ran, and this one is not enumerated by the loop
    # above — which is exactly how it would come to not run.
    cb = json.loads(C_HUMAN_BASKET.read_text())
    rule = cb.get("rule") or {}
    priced = rule.get("floor_usd") if rule.get("aggregate") == "floor" else "from rows"
    print(
        f"  ✓ {'C-HUMAN':<9} ${priced}/human, status '{cb.get('status')}' "
        f"(a floor, not an estimate — see the basket)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
