"""The payment-exhaust simulator — data source #1 for ACR.

Ties together latent price processes, a heterogeneous seller population, honest
Poisson-arrival trade flow, the Gateway batching operator, and (optionally) an
adversarial wash-flow injector. Produces a fully reproducible ``SimResult``:
the observed tape the estimator consumes, plus the ground-truth latent paths the
evaluation harness scores against.

Everything is seeded through a single ``numpy`` Generator, so a given
``SimConfig`` always yields byte-identical output.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from acr_core import (
    ALL_INDEX_IDS,
    SellerAttestation,
    Service,
    TapeEvent,
    index_for_service,
    spec_for,
)

from .adversary import AttackConfig, generate_wash_flow
from .batching import assign_batches
from .processes import LatentPath, OUParams, simulate_ou
from .sellers import make_seller_population


@dataclass
class SimConfig:
    seed: int = 7
    #: Economic horizon in seconds (default 24h).
    horizon: float = 86_400.0
    #: Latent-process grid step (seconds).
    dt: float = 60.0
    #: Gateway batch width (seconds). settlement smears economic time by this.
    batch_interval: float = 300.0
    #: Honest sellers per service.
    n_sellers_per_service: int = 25
    #: Expected honest trades per service over the horizon.
    events_per_service: int = 6_000
    #: Median honest trade notional (USDC), lognormal.
    median_notional: float = 40.0
    services: tuple[Service, ...] = tuple(Service)
    attack: AttackConfig | None = None


@dataclass
class SimResult:
    events: list[TapeEvent]
    attestations: list[SellerAttestation]
    #: Ground-truth latent (constant-quality) price path, keyed by index id.
    latent: dict[str, LatentPath] = field(default_factory=dict)
    usdc_attacked: float = 0.0
    n_adversarial: int = 0
    config: SimConfig | None = None

    def events_for(self, index_id: str) -> list[TapeEvent]:
        svc = spec_for(index_id).service
        return [e for e in self.events if e.service == svc]

    def true_price(self, index_id: str, ts: float) -> float:
        """Persistent latent level (μ) at a single instant."""
        return self.latent[index_id].level_at(ts)

    def true_window_level(self, index_id: str, events: list[TapeEvent] | None = None) -> float:
        """Notional-weighted median persistent level over a window's honest flow.

        The airtight evaluation target: exactly the quantity a perfect estimator
        recovers from *this* window, robust to drift of μ inside the window.
        """
        from acr_core import weighted_median

        spec = spec_for(index_id)
        evs = events if events is not None else self.events
        honest = [e for e in evs if e.service == spec.service and not e.is_adversarial]
        if not honest:
            raise ValueError(f"no honest events for {index_id}")
        levels = np.array([self.latent[index_id].level_at(e.ts) for e in honest])
        weights = np.array([e.notional for e in honest])
        return float(weighted_median(levels, weights))


def _ou_params_for(index_id: str) -> OUParams:
    # theta/sigma in per-second units on the dt grid. Chosen so the transient
    # around the persistent level μ is small (~1-2%) inside an hour, while the
    # level itself drifts across the day via occasional regime shifts.
    ref = spec_for(index_id).reference_level
    return OUParams(
        mu=float(np.log(ref)),
        theta=0.003,
        sigma=0.0015,
        regime_prob=0.0015,
        regime_size=0.05,
    )


def simulate(config: SimConfig | None = None) -> SimResult:
    cfg = config or SimConfig()
    rng = np.random.default_rng(cfg.seed)

    all_events: list[TapeEvent] = []
    all_attest: list[SellerAttestation] = []
    latent: dict[str, LatentPath] = {}
    usdc_attacked = 0.0
    n_adv = 0

    for service in cfg.services:
        index_id = index_for_service(service).id
        path = simulate_ou(_ou_params_for(index_id), cfg.horizon, cfg.dt, rng)
        latent[index_id] = path

        # Service-specific prefix so seller IDs are globally unique — otherwise
        # attestations collide across services in the hedonic feature lookup.
        sellers = make_seller_population(
            service, cfg.n_sellers_per_service, rng, prefix=f"{service.value[:3]}-s"
        )
        for s in sellers:
            all_attest.append(s.to_attestation(ts=0.0))

        weights = np.array([s.weight for s in sellers])
        weights = weights / weights.sum()

        # Honest flow: Poisson count, uniform economic timestamps.
        n_events = int(rng.poisson(cfg.events_per_service))
        ev_ts = np.sort(rng.uniform(0.0, cfg.horizon, size=n_events))
        seller_idx = rng.choice(len(sellers), size=n_events, p=weights)
        notionals = cfg.median_notional * np.exp(rng.normal(0.0, 0.6, size=n_events))
        price_noise = rng.normal(0.0, 0.02, size=n_events)

        for i in range(n_events):
            s = sellers[seller_idx[i]]
            t = float(ev_ts[i])
            true_unit = path.price_at(t)
            unit_price = true_unit * s.quality_premium() * float(
                np.exp(s.log_bias + price_noise[i])
            )
            size = float(notionals[i]) / unit_price
            all_events.append(
                TapeEvent(
                    event_id=f"{index_id}-{i}",
                    ts=t,
                    service=service,
                    seller=s.seller_id,
                    buyer=f"0xbuyer{rng.integers(0, 500):04d}",
                    price=unit_price,
                    size=size,
                    model_class=s.model_class,
                )
            )

        # Adversarial flow for this service (if configured).
        if cfg.attack is not None:
            def _latent_at(t: float, _p=path) -> float:
                return _p.price_at(t)

            def _size(unit_price: float, notional: float) -> float:
                return notional / unit_price

            from acr_core import get_settings

            settings = get_settings()
            wash, spent = generate_wash_flow(
                cfg.attack,
                service,
                _latent_at,
                _size,
                fee_bps=settings.usdc_fee_bps,
                fee_flat=settings.usdc_fee_flat,
                horizon=cfg.horizon,
                rng=rng,
                start_event_idx=len(all_events),
            )
            all_events.extend(wash)
            usdc_attacked += spent
            n_adv += len(wash)

    # Apply the batching operator to the full combined tape.
    all_events.sort(key=lambda e: e.ts)
    if all_events:
        ts_arr = np.array([e.ts for e in all_events])
        batch_id, settled = assign_batches(ts_arr, cfg.batch_interval)
        for e, b, st in zip(all_events, batch_id, settled, strict=True):
            e.batch_id = int(b)
            e.settled_ts = float(st)

    return SimResult(
        events=all_events,
        attestations=all_attest,
        latent=latent,
        usdc_attacked=usdc_attacked,
        n_adversarial=n_adv,
        config=cfg,
    )


def default_indices() -> tuple[str, ...]:
    return ALL_INDEX_IDS
