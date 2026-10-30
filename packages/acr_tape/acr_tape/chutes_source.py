"""Chutes (Bittensor netuid 64) as an ACR tape source — realized inference prices.

Chutes is the largest inference seller on Bittensor, and it publishes two things
over unauthenticated HTTP that together make a **transacted** price:

* ``/invocations/usage`` — the USD actually billed, per model per day.
* ``/invocations/stats/llm`` — the tokens that bought it, per model per day.

Dividing one by the other gives realized ``$/1k tokens``: not a rate card, but
what buyers paid. That distinction is the whole reason this is a tape source
while Lium's GPU asks are an ``anchors/`` basket — an unexecuted offer on idle
inventory is a reservation price, and `docs/methodology.md` §1 defines the
estimand as what a buyer *would pay*, estimated from what buyers *did* pay.

Three properties of this tape are not negotiable and are stated here so nothing
downstream has to rediscover them:

**It is daily, not hourly.** ``/invocations/usage`` carries about twelve days of
daily aggregates. ACR is specified as an hourly fixing; this source cannot feed
that, and pretending otherwise would deconvolve a batch interval that is not
there. Consume it on a daily clock, or poll forward with
``scripts/bt_snapshot.py`` and grow an intraday series over weeks.

**It is single-venue, so the funding graph is a star.** Every sale is one
aggregate buyer paying many sellers. Louvain returns one community, which breaks
both the sybil test and the cluster cap — measured, with both failure modes, in
``acr_estimator.cleaning.clean``. Windows from this source must be estimated with
``single_venue=True``, which publishes a price and refuses to publish an attack
cost. ``requires_single_venue`` advertises that so a caller cannot forget.

**Its prices are emission-subsidised.** Chutes miners are paid in TAO emission as
well as by buyers, so the observed price is below an unsubsidised cost of
service. ACR measures what a buyer pays, which is the right estimand — but the
market being measured is subsidised, and that belongs in the methodology rather
than in a footnote, because exposing exactly this kind of distortion is what a
reference rate is for.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterator
from pathlib import Path

from acr_core import ModelClass, SellerAttestation, Service, TapeEvent

from .base import TapeSource

#: Committed, deterministic offline input. Refresh with
#: ``uv run python scripts/bt_snapshot.py --capture-fixture``.
_FIXTURE_DIR = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt"
_DEFAULT_FIXTURE = _FIXTURE_DIR / "chutes_usage.json"

#: ``chute_id -> largest observed GPU count``, from ``/chutes/gpu_count_history``.
#: A hardware-footprint covariate: measured, and economically real — an 8-GPU
#: model is a different good from a 1-GPU one. It reaches 19 of the 26 priced
#: chutes (73%), so it CROSS-CHECKS ``model_class_for`` rather than replacing it;
#: the name heuristic covers every row.
_GPU_COUNT_FIXTURE = _FIXTURE_DIR / "chutes_gpu_counts.json"

#: GPU count at or above which a chute is frontier-class hardware. Measured on
#: the live data the split is bimodal — 9 priced chutes on 1 GPU and 10 on 8,
#: with nothing between — so this threshold sits in an empty region rather than
#: cutting through a cluster.
_FRONTIER_GPUS = 4

#: Seconds in the daily bucket the upstream data is aggregated into.
_DAY_S = 86_400.0

#: Parameter-count thresholds, in billions, for mapping a model name onto
#: ``ModelClass``. The hedonic stage needs a quality class and the upstream data
#: gives only a name, so the name is parsed. Declared, not observed.
_FRONTIER_B = 200.0
_MID_B = 20.0

_PARAM_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([BTbt])\b")


def model_class_for(name: str) -> ModelClass:
    """Map a Chutes model name onto an ACR quality class by parameter count.

    ``Qwen/Qwen3-235B-A22B`` → FRONTIER, ``Qwen3.8-27B`` → MID, ``Qwen3-4B`` →
    SMALL, and anything whose size cannot be read → OPEN, which is the hedonic
    regression's omitted baseline and therefore the honest place to put an
    unknown rather than guessing a class that carries a coefficient.

    Reads the LARGEST size token in the name on purpose: a mixture-of-experts
    name like ``Qwen3.5-397B-A17B`` carries both total and active parameters,
    and total parameters is the one that tracks what the model costs to serve.
    """
    if not name:
        return ModelClass.OPEN
    sizes = []
    for num, unit in _PARAM_RE.findall(name):
        try:
            v = float(num)
        except ValueError:  # pragma: no cover - regex guarantees a float
            continue
        sizes.append(v * 1000.0 if unit in "Tt" else v)
    if not sizes:
        return ModelClass.OPEN
    b = max(sizes)
    if b >= _FRONTIER_B:
        return ModelClass.FRONTIER
    if b >= _MID_B:
        return ModelClass.MID
    return ModelClass.SMALL


#: Upper bound on a per-seller latency, in ms. Chutes `average_ttft` is in
#: SECONDS (live p50 is ~1.1s; milliseconds would mean an impossible 1.1ms), but
#: the live MAX is ~1632s -- a 1,632,652ms SLO, log-centered by `hedonic`
#: against REF_LATENCY_MS=250, which makes one dead chute the highest-leverage
#: point in the window. 60s is already 240x the reference: past that the row
#: says "this seller is broken", not "this seller is slow", and clamping keeps
#: it in the sample without letting it set the price.
_MAX_LATENCY_MS = 60_000.0

#: What a row with no usable `average_ttft` is given. It equals
#: `acr_estimator.hedonic.REF_LATENCY_MS` BY CONSTRUCTION, which means such a
#: row lands exactly on the reference seller and contributes zero latency shift
#: -- the neutral choice, and the honest one for "no information". It is
#: duplicated rather than imported because `acr_tape` does not depend on
#: `acr_estimator` and should not: the tape is upstream of the estimator.
#: `ChutesSource.n_latency_imputed` counts how many rows took this path, so the
#: substitution is inspectable instead of silent.
_UNKNOWN_LATENCY_MS = 250.0


def _as_float(v: object) -> float | None:
    """``v`` as a float, or None if it is not a finite number.

    Upstream numerics arrive string-encoded often enough to matter (Taostats
    already does it for ``registration_cost``), and a bare ``v <= 0`` on a str
    raises TypeError out of the per-row loop and takes the whole build with it.
    """
    if isinstance(v, bool) or v is None:
        return None
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _stable_offset(key: str) -> float:
    """A deterministic second-of-day for a seller, in ``[0, 86400)``.

    The upstream rows share one ``date`` and carry no intraday time, so every
    event in a day would land on the same instant. That is not merely
    low-resolution: ``pipeline._bar_series`` computes a bar's duration as
    ``max(ts) - min(ts) + 1e-9``, so identical timestamps give ``1e-9``, and
    ``round(batch_interval / 1e-9)`` is 3e11 — the deconvolution's box filter
    then spans the entire sample. Spreading sellers across the day keeps bar
    durations real.

    Uses a fixed FNV-1a rather than ``hash()``, which is salted per process and
    would make the tape non-reproducible across runs.
    """
    h = 0x811C9DC5
    for ch in key.encode("utf-8"):
        h = ((h ^ ch) * 0x01000193) & 0xFFFFFFFF
    return float(h % int(_DAY_S))


def load_gpu_counts(path: Path | str | None = None) -> dict[str, int]:
    """``chute_id -> gpu_count`` from the committed fixture. Missing file is empty."""
    p = Path(path) if path is not None else _GPU_COUNT_FIXTURE
    if not p.exists():
        return {}
    try:
        rows = json.loads(p.read_text())
    except (OSError, ValueError):  # pragma: no cover - a corrupt fixture
        return {}
    if not isinstance(rows, list):
        return {}
    return {
        str(r["chute_id"]): int(r["gpu_count"])
        for r in rows
        if isinstance(r, dict) and r.get("chute_id") and r.get("gpu_count")
    }


def class_from_gpu_count(n: int | None) -> ModelClass | None:
    """Quality class implied by hardware footprint, or ``None`` when unknown.

    An independent read on the same question ``model_class_for`` answers from the
    model's name. Kept separate so the two can be compared instead of one
    silently overriding the other — they agree on the live data, and a
    disagreement would be worth knowing about rather than averaging away.
    """
    if not n or n <= 0:
        return None
    return ModelClass.FRONTIER if n >= _FRONTIER_GPUS else ModelClass.MID


class ChutesSource(TapeSource):
    """Realized Chutes inference prices as ``TapeEvent``s.

    Offline by default: reads the committed fixture, so tests and
    ``make subnet-demo`` need no network and no API key.
    """

    #: Windows from this source must be estimated with ``single_venue=True``.
    #: See the module docstring; this is advertised rather than documented so a
    #: caller can assert on it.
    requires_single_venue = True

    #: The upstream aggregation period, in seconds. A caller choosing a fixing
    #: window should not choose one shorter than this.
    native_step_s = _DAY_S

    #: The services this source carries any tape for. Chutes sells inference and
    #: nothing else, so ACR-GPU and ACR-DATA have no events here and never will
    #: -- which is a DIFFERENT thing from an index that should have data and
    #: doesn't. Advertised, like the two flags above, so a caller can tell a
    #: permanently-unserved index from a stalled one instead of logging both the
    #: same way. A source that declares nothing is assumed to serve everything.
    covers_services = (Service.INFERENCE,)

    def __init__(
        self,
        path: Path | str | None = None,
        rows: list[dict] | None = None,
        gpu_counts: dict[str, int] | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else _DEFAULT_FIXTURE
        self._rows = rows
        self._gpu_counts = load_gpu_counts() if gpu_counts is None else gpu_counts
        self._events: list[TapeEvent] | None = None
        self._attest: list[SellerAttestation] | None = None
        #: Rows whose latency was imputed to `_UNKNOWN_LATENCY_MS` because
        #: `average_ttft` was missing or unusable. Set by `_build`.
        self.n_latency_imputed = 0

    # -- loading ---------------------------------------------------------
    def _load(self) -> list[dict]:
        if self._rows is not None:
            return self._rows
        if not self._path.exists():
            return []
        try:
            text = self._path.read_text()
        except OSError:  # pragma: no cover - unreadable file
            return []
        if self._path.suffix == ".jsonl":
            # Per LINE, not per file: this path now reads the cumulative archive
            # (`services/index_api/index_api/bt_tape/`), which is append-only and
            # grows forever, so one truncated write must not cost every day
            # before it. Same tolerance `load_gpu_counts` already applies.
            out: list[dict] = []
            for ln in text.splitlines():
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    row = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
            return out
        try:
            data = json.loads(text)
        except ValueError:  # pragma: no cover - a corrupt fixture
            return []
        return data if isinstance(data, list) else []

    def _build(self) -> None:
        rows = self._load()
        self.n_latency_imputed = 0
        dates = sorted({str(r.get("date")) for r in rows if r.get("date")})
        day_index = {d: i for i, d in enumerate(dates)}

        events: list[TapeEvent] = []
        attest: dict[str, SellerAttestation] = {}
        for r in rows:
            # Coerce before comparing. A string-encoded price passes `not price`
            # (truthy) and then raises TypeError on `price <= 0`, killing the
            # whole build -- and upstream already string-encodes numerics
            # elsewhere (Taostats `registration_cost`), so this is not
            # hypothetical.
            price = _as_float(r.get("price_per_1k_tokens"))
            toks = _as_float(r.get("tokens_total"))
            date = str(r.get("date") or "")
            if price is None or toks is None or price <= 0 or toks <= 0:
                continue
            if date not in day_index:
                continue
            seller = str(r.get("name") or r.get("chute_id") or "unknown")
            # The name heuristic answers for every row; the GPU count answers for
            # 73% of them. So the name leads, and the hardware footprint fills in
            # only where the name carries no size at all — a chute called
            # "[private]" is OPEN by absence of information, and knowing it runs
            # on 8 GPUs is strictly better information than that.
            mc = model_class_for(seller)
            gpus = self._gpu_counts.get(str(r.get("chute_id") or ""))
            if mc is ModelClass.OPEN:
                mc = class_from_gpu_count(gpus) or mc
            ts = day_index[date] * _DAY_S + _stable_offset(str(r.get("chute_id") or seller))
            events.append(
                TapeEvent(
                    event_id=f"chutes:{date}:{str(r.get('chute_id'))[:12]}",
                    ts=ts,
                    service=Service.INFERENCE,
                    seller=seller,
                    # One aggregate buyer, which is the literal truth of the
                    # upstream data and the reason this tape is single-venue.
                    buyer="chutes:aggregate",
                    price=float(price),
                    size=float(toks) / 1000.0,
                    settled_ts=ts,
                    model_class=mc,
                )
            )
            if seller not in attest:
                ttft = _as_float(r.get("average_ttft"))
                if not ttft or ttft <= 0:
                    self.n_latency_imputed += 1
                attest[seller] = SellerAttestation(
                    seller=seller,
                    service=Service.INFERENCE,
                    model_class=mc,
                    # Real measured latency, in ms. Time-to-first-token is not a
                    # latency SLO, but it is an observed per-seller latency, and
                    # it is the first non-simulated value this field has carried.
                    # `average_ttft` is in SECONDS -- pinned by
                    # `test_ttft_is_seconds_not_ms`, because a silent unit flip
                    # here moves every inference print. Clamped at both ends: the
                    # live max is ~1632s, which unclamped is a 1.6M-ms SLO and the
                    # single highest-leverage point in the window.
                    latency_slo_ms=(
                        min(_MAX_LATENCY_MS, max(1.0, ttft * 1000.0))
                        if ttft and ttft > 0
                        else _UNKNOWN_LATENCY_MS
                    ),
                    schema_id="chutes/invocations.v1",
                    ts=0.0,
                )
        events.sort(key=lambda e: (e.ts, e.event_id))
        self._events = events
        self._attest = list(attest.values())

    # -- TapeSource ------------------------------------------------------
    def stream(self) -> Iterator[TapeEvent]:
        if self._events is None:
            self._build()
        yield from (self._events or [])

    def attestations(self) -> list[SellerAttestation]:
        if self._attest is None:
            self._build()
        return list(self._attest or [])

    # `range` and `collect` come from TapeSource and need no override here.
