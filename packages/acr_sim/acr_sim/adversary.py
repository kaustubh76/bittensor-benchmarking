"""Adversarial flow — a first-class input, not an afterthought.

The estimator is designed against contaminated data. The adversary prints wash
trades at an off-market price to drag the index, using a dense cluster of sybil
identities. To exercise the *whole* cleaning stack (not just sybil-community
detection), the emitted tape deliberately includes three shapes:

  * **wash trades** among a dense pool of sybil buyers/sellers → the trades that
    move naive VWAP, caught by Louvain sybil-community detection + cluster caps;
  * **reverse funding legs** — the seller paying the buyer back near latent
    price, emitted *on the tape* so the reciprocal (A→B and B→A) wash-cycle
    detector sees a real edge, not an aspirational one;
  * **self-deals** — the occasional buyer == seller print, caught by the
    self-dealing filter.

Every print (including the funding legs) burns USDC fees, so a fixed attack
*budget* buys a bounded amount of manipulation — the economic fact Pillar 3
turns into the "attack cost per bp" number.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from acr_core import ModelClass, Service, TapeEvent

from .sellers import Seller


@dataclass
class AttackConfig:
    """Parameters of a wash-flow attack."""

    #: Total USDC the adversary is willing to burn on fees (a spend cap).
    budget_usdc: float = 5_000.0
    #: Hard cap on the number of wash trades (the "50k-tx bot" knob). Keeps the
    #: attack economically *and* computationally bounded — without it a tiny
    #: per-trade fee lets a modest budget imply millions of trades.
    n_wash_trades: int = 12_000
    #: Target price = latent × this multiplier (>1 pumps, <1 dumps).
    target_multiplier: float = 1.5
    #: Number of sybil sellers in the ring.
    n_sybil_sellers: int = 8
    #: Number of colluding buyer identities.
    n_sybil_buyers: int = 8
    #: USDC notional per wash trade.
    trade_notional: float = 50.0
    #: Window over which the attack runs (economic seconds); None = full horizon.
    t_start: float | None = None
    t_end: float | None = None
    #: Fraction of prints that are self-deals (buyer == seller) — exercises the
    #: self-dealing filter.
    p_self_deal: float = 0.03
    #: Probability a wash print is followed by its reverse funding leg on the
    #: tape (seller pays buyer back near latent) — exercises the reciprocal
    #: wash-cycle detector.
    p_reciprocal_funding: float = 0.5
    #: Draw buyer/seller uniformly from the sybil pools (a dense community)
    #: rather than in fixed pairs.
    randomize_pairs: bool = True


def make_sybil_sellers(cfg: AttackConfig, service: Service) -> list[Seller]:
    """Create the sybil seller ring (all flagged ``is_sybil``)."""
    return [
        Seller(
            seller_id=f"0xsybilS{i:03d}",
            service=service,
            model_class=ModelClass.OPEN,
            latency_slo_ms=300.0,
            schema_id=f"{service.value}.sybil",
            log_bias=0.0,
            weight=0.0,
            is_sybil=True,
        )
        for i in range(cfg.n_sybil_sellers)
    ]


def generate_wash_flow(
    cfg: AttackConfig,
    service: Service,
    latent_price_at,
    unit_price_to_size,
    fee_bps: float,
    fee_flat: float,
    horizon: float,
    rng: np.random.Generator,
    start_event_idx: int = 0,
) -> tuple[list[TapeEvent], float]:
    """Emit wash TapeEvents until the USDC budget (or trade cap) is exhausted.

    ``latent_price_at(t)`` returns the true unit price; the adversary quotes wash
    trades at ``target_multiplier`` of it and funding legs near 1×. Every emitted
    print (wash, funding leg, self-deal) burns one ``per_trade_fee``, so the fee
    budget bounds the whole campaign.

    Returns (events, usdc_spent).
    """
    seller_ids = [s.seller_id for s in make_sybil_sellers(cfg, service)]
    buyer_ids = [f"0xsybilB{i:03d}" for i in range(cfg.n_sybil_buyers)]
    t0 = 0.0 if cfg.t_start is None else cfg.t_start
    t1 = horizon if cfg.t_end is None else cfg.t_end
    # An empty window means no attack, not an attack of zero width.
    #
    # `t = rng.uniform(t0, t1)` returns t0 for EVERY draw when t0 == t1, so a
    # caller asking for a quiet scenario the obvious way — attack_from ==
    # attack_to — got the full 36,000 wash prints stacked on a single instant,
    # and every hour of it labelled `attack=False`. The most adversarial window
    # in the repo, silently pinned as the clean baseline.
    if t1 <= t0:
        return [], 0.0

    # Two disjoint sybil sub-clusters so the whole stack is exercised, not just
    # one filter: the "cycle" cluster does reciprocal 2-cycles (caught by the
    # wash-cycle detector); the "ring" cluster washes one-directionally among
    # itself (caught by sybil-community detection). Disjoint address sets ⇒
    # separate Louvain communities.
    hs = max(1, len(seller_ids) // 2)
    hb = max(1, len(buyer_ids) // 2)
    cyc_sellers, ring_sellers = seller_ids[:hs], seller_ids[hs:] or seller_ids[:hs]
    cyc_buyers, ring_buyers = buyer_ids[:hb], buyer_ids[hb:] or buyer_ids[:hb]

    events: list[TapeEvent] = []
    spent = 0.0
    k = start_event_idx

    per_trade_fee = fee_flat + fee_bps * 1e-4 * cfg.trade_notional
    if per_trade_fee <= 0:  # pragma: no cover - defensive
        per_trade_fee = 1e-9
    # Bounded by whichever binds first: the trade-count cap or the fee budget.
    max_trades = min(cfg.n_wash_trades, int(cfg.budget_usdc / per_trade_fee))

    def _emit(seller: str, buyer: str, mult: float, t: float) -> None:
        nonlocal k, spent
        unit_price = latent_price_at(t) * mult
        events.append(
            TapeEvent(
                event_id=f"wash-{k}",
                ts=t,
                service=service,
                seller=seller,
                buyer=buyer,
                price=unit_price,
                size=unit_price_to_size(unit_price, cfg.trade_notional),
                model_class=ModelClass.OPEN,
                is_adversarial=True,
            )
        )
        spent += per_trade_fee
        k += 1

    def _budget_left() -> bool:
        return len(events) < max_trades and spent < cfg.budget_usdc

    def _pick(pool: list[str], j: int) -> str:
        return pool[int(rng.integers(0, len(pool)))] if cfg.randomize_pairs else pool[j % len(pool)]

    j = 0
    while _budget_left():
        t = float(rng.uniform(t0, t1))
        if rng.random() < cfg.p_self_deal:
            # Self-deal: same address buys from itself (defense 1).
            s = _pick(seller_ids, j)
            _emit(s, s, cfg.target_multiplier, t)
            j += 1
            continue

        if rng.random() < 0.5:
            # Cycle cluster: pumped wash + reverse funding leg → reciprocal pair
            # (defense 2). The leg pays back near latent so net flow ~ zero.
            seller, buyer = _pick(cyc_sellers, j), _pick(cyc_buyers, j)
            _emit(seller, buyer, cfg.target_multiplier, t)
            if _budget_left() and rng.random() < cfg.p_reciprocal_funding:
                fund_mult = max(1e-6, 1.0 + float(rng.normal(0.0, 0.01)))
                _emit(buyer, seller, fund_mult, t)
        else:
            # Ring cluster: one-directional pumped wash within a self-contained
            # community (defense 3), backstopped by cluster caps (defense 4).
            seller, buyer = _pick(ring_sellers, j), _pick(ring_buyers, j)
            _emit(seller, buyer, cfg.target_multiplier, t)
        j += 1

    events.sort(key=lambda e: e.ts)
    return events, spent
