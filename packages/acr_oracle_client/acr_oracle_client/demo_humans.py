"""Deterministic demo humans and their wallets — the fixture AgentBook's roster.

Until World Sandbox access exists there is no real AgentBook to read, so the
demo needs identities of its own. They are derived from labels exactly the way
`demo_sellers.py` derives sellers, in their own namespaces so a buyer key can
never collide with a seller key.

THESE ARE NOT PEOPLE. Every one is flagged `sandbox` all the way onto the chain
(`HumanIdMirror.clusterProvenance`) and into the tape (`HumanCluster.sandbox`),
so a rating that counts them says how much of its human depth is demo. That flag
is the whole reason this file is safe to have.

The SHAPE is the point. One human holds three wallets and another holds one, so
`distinctHumans` (2) differs visibly from `distinctPayers` (4). With one wallet
per human the two numbers are always equal, the ratio is always 1.0, and nothing
on screen shows why grouping wallets by human matters at all — which is the only
thing the human-depth component measures.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from .signer import LocalKeySigner


@dataclass(frozen=True)
class DemoBuyer:
    """One demo payer wallet. Testnet identity only, never funds worth keeping."""

    label: str

    @property
    def private_key(self) -> str:
        return "0x" + hashlib.sha256(f"acr-buyer::{self.label}".encode()).hexdigest()

    def signer(self) -> LocalKeySigner:
        return LocalKeySigner(self.private_key)

    @property
    def address(self) -> str:
        return self.signer().address


@dataclass(frozen=True)
class DemoHuman:
    """One demo human and the wallets that act for them."""

    label: str
    buyers: tuple[str, ...]

    @property
    def nullifier(self) -> str:
        """Stands in for a World ID nullifier.

        A different namespace from the wallet keys on purpose: these two values
        must never be derivable from one another, or a wallet address would
        reveal the human behind it and the rotation in `HumanIdMirror` would be
        protecting nothing.
        """
        return "0x" + hashlib.sha256(f"acr-human::{self.label}".encode()).hexdigest()

    @property
    def wallets(self) -> list[str]:
        return [DemoBuyer(b).address for b in self.buyers]


#: Two humans, four wallets, deliberately lopsided — see the module docstring.
DEMO_HUMANS: list[DemoHuman] = [
    DemoHuman("fleet", ("acr-buyer-1", "acr-buyer-2", "acr-buyer-3")),
    DemoHuman("solo", ("acr-buyer-4",)),
]
