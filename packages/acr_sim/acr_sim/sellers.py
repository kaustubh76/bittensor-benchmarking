"""Heterogeneous seller population.

Observed price = latent price × a *quality premium* × idiosyncratic noise. The
quality premium is a deterministic function of the seller's hedonic features
(model class, latency SLO). Recovering the latent, quality-free rate from prices
that mix these premia together is exactly the job of Pillar 2 (hedonic
adjustment) — so the simulator plants a known premium structure the estimator
must strip back out.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from acr_core import ModelClass, SellerAttestation, Service

# Multiplicative price premium by model class, normalized so the hedonic
# REFERENCE class (MID) has premium exactly 1.0. Combined with a latency factor
# that is 1.0 at the 250ms reference, a MID/250ms seller prices exactly at the
# latent rate — so the constant-quality rate the estimator recovers *is* the
# latent price, and evaluation compares like with like.
CLASS_PREMIUM: dict[ModelClass, float] = {
    ModelClass.FRONTIER: 1.45,
    ModelClass.MID: 1.00,
    ModelClass.SMALL: 0.73,
    ModelClass.OPEN: 0.50,
}


@dataclass
class Seller:
    seller_id: str
    service: Service
    model_class: ModelClass
    latency_slo_ms: float
    schema_id: str
    #: Persistent idiosyncratic price bias (log-space), e.g. brand/relationship.
    log_bias: float
    #: Base share of flow this seller wins.
    weight: float
    is_sybil: bool = False

    def quality_premium(self) -> float:
        """Deterministic hedonic premium from this seller's features.

        Frontier models cost more; tighter latency SLOs command a premium.
        The latency term is a mild log-linear effect on 1/latency.
        """
        base = CLASS_PREMIUM[self.model_class]
        # Lower latency (faster) -> higher premium; reference 250ms.
        latency_factor = (250.0 / self.latency_slo_ms) ** 0.08
        return base * latency_factor

    def to_attestation(self, ts: float = 0.0) -> SellerAttestation:
        return SellerAttestation(
            seller=self.seller_id,
            service=self.service,
            model_class=self.model_class,
            latency_slo_ms=self.latency_slo_ms,
            schema_id=self.schema_id,
            ts=ts,
        )


_CLASSES = list(CLASS_PREMIUM.keys())


def make_seller_population(
    service: Service,
    n: int,
    rng: np.random.Generator,
    prefix: str = "seller",
) -> list[Seller]:
    """Draw ``n`` honest sellers with a spread of quality features."""
    sellers: list[Seller] = []
    for i in range(n):
        mc = _CLASSES[rng.integers(0, len(_CLASSES))]
        latency = float(rng.uniform(50, 600))
        schema = f"{service.value}.v{rng.integers(1, 4)}"
        bias = float(rng.normal(0.0, 0.03))
        weight = float(rng.gamma(2.0, 1.0))
        sellers.append(
            Seller(
                seller_id=f"0x{prefix}{i:04d}",
                service=service,
                model_class=mc,
                latency_slo_ms=latency,
                schema_id=schema,
                log_bias=bias,
                weight=weight,
            )
        )
    return sellers
