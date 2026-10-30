"""Adapters that put the subnet mechanism on a real chain.

Nothing here is imported by `acr_subnet/__init__.py`, and nothing here is
imported at package-import time. The mechanism — `scoring`, `miner`, `validator`
— is arithmetic over the ACR estimator and runs offline and deterministically,
which is what makes it testable in CI on a machine that has never heard of
Bittensor. These modules are the network skin over that same arithmetic.

Import them directly, and only when you mean to talk to a chain:

    from acr_subnet.adapters.bt_chain import Chain

They need the `bittensor` extra:

    uv sync --extra bittensor
"""

from __future__ import annotations

__all__: list[str] = []
