"""``SimSource`` — wraps the calibrated simulator as a TapeSource."""

from __future__ import annotations

from collections.abc import Iterator

from acr_core import SellerAttestation, TapeEvent
from acr_sim import SimConfig, SimResult, simulate

from .base import TapeSource


class SimSource(TapeSource):
    """Serve tape + attestations from an ``acr_sim`` run.

    Pass either a ready ``SimResult`` or a ``SimConfig`` (simulated lazily on
    first use). The ground-truth latent paths remain accessible via ``.result``
    for the evaluation harness — but the estimator never touches them.
    """

    def __init__(
        self,
        result: SimResult | None = None,
        config: SimConfig | None = None,
    ) -> None:
        self._result = result
        self._config = config or SimConfig()

    @property
    def result(self) -> SimResult:
        # Simulated lazily on first use so importing a module that constructs a
        # SimSource (e.g. index_api.app) does not run a full simulation at import.
        if self._result is None:
            self._result = simulate(self._config)
        return self._result

    def stream(self) -> Iterator[TapeEvent]:
        yield from self.result.events

    def attestations(self) -> list[SellerAttestation]:
        return list(self.result.attestations)
