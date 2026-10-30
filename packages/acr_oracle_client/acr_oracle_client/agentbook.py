"""AgentBook — World's registry of which wallet acts for which human.

A real, shipping contract, not a placeholder:

    0xA23aB2712eA7BBa896930544C7d6636a96b944dA   World Chain mainnet (eip155:480)

    function lookupHuman(address agent) view returns (uint256 humanId)

`humanId` IS the World ID nullifier hash, and `0` means "not registered". Both
the address and a public RPC are constants, so this reader needs no credentials
and works out of the box.

A LOOKUP, NOT AN ENUMERATION — and that shapes everything downstream. The
contract answers "who is this wallet?", not "list every human". So the resolver
starts from the payers already on ACR's tape and asks about each, rather than
enrolling a roster and hoping those wallets go on to trade. Wallets that never
trade are never enrolled; wallets that trade are never missed.

WHAT THIS MEANS FOR PRIVACY, STATED PLAINLY. AgentBook publishes wallet →
nullifier on World Chain, so a human's registered fleet is ALREADY public there
to anyone scanning `AgentRegistered`. The rotation in `HumanIdMirror` therefore
does not make a fleet private — nothing here could. What it does is keep ACR's
tape from becoming a SECOND publication of that durable identifier, keyed to our
own settlement data, so this tape cannot be joined to another service's by id.

OFFLINE-TOLERANT, NOT FAIL-CLOSED. `humanid.py` refuses to serve a request it
could not verify, because admitting an unverified human is a security failure.
This is the other side: reading. An unreachable AgentBook means the resolver
enrolls nobody this run — the same steady state an outage produces. Refusing to
admit without proof and declining to invent data when a source is down are the
same discipline, not opposite ones.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from acr_core import get_settings

from .demo_humans import DEMO_HUMANS

log = logging.getLogger("acr_oracle_client.agentbook")

#: The deployed AgentBook. A public constant, verified on worldscan.org — not a
#: credential, and not something an operator should have to discover.
AGENTBOOK_ADDRESS = "0xA23aB2712eA7BBa896930544C7d6636a96b944dA"
#: World Chain mainnet. Lookup ALWAYS resolves here, even when registration was
#: relayed via another chain.
WORLD_CHAIN_ID = 480
WORLD_CHAIN_RPC = "https://worldchain-mainnet.g.alchemy.com/public"

#: Only what we read. The AgentKit repo ships Solidity, not an ABI artifact, so
#: this is hand-written — kept to the two view functions so there is little to
#: drift and nothing here can write.
AGENTBOOK_ABI = [
    {
        "type": "function",
        "name": "lookupHuman",
        "stateMutability": "view",
        "inputs": [{"name": "agent", "type": "address"}],
        "outputs": [{"name": "humanId", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "getNextNonce",
        "stateMutability": "view",
        "inputs": [{"name": "agent", "type": "address"}],
        "outputs": [{"name": "nonce", "type": "uint256"}],
    },
]


@dataclass(frozen=True)
class Registration:
    """What AgentBook says about one wallet. Never constructed for humanId 0."""

    wallet: str
    #: The World ID nullifier hash, as the contract stores it.
    human_id: int
    #: A Sandbox/simulator identity rather than an Orb-verified person.
    #:
    #: AgentBook does not record this — it stores a nullifier and nothing about
    #: how the human proved themselves. So on this path the flag is OUR
    #: assertion, taken from configuration, and it is labelled as one wherever
    #: it surfaces. Only the cloud-verify path can observe it (v4 returns
    #: `environment`), and even there the legacy v2 response cannot.
    sandbox: bool

    @property
    def nullifier(self) -> str:
        """The nullifier as bytes32 hex — what `cluster_id()` expects."""
        return "0x" + self.human_id.to_bytes(32, "big").hex()


@dataclass(frozen=True)
class Human:
    """One human and the wallets known to act for them. Enumerable books only."""

    nullifier: str
    wallets: tuple[str, ...]
    sandbox: bool


@runtime_checkable
class AgentBook(Protocol):
    source: str

    def lookup(self, wallet: str) -> Registration | None: ...

    def roster(self) -> list[Human] | None:
        """Every human, or None when the source cannot enumerate."""
        ...


class FixtureAgentBook:
    """The demo roster. Every human is `sandbox=True`, and cannot be otherwise.

    Not a mock in the testing sense — it is what the demo genuinely runs on
    until wallets are registered in the real AgentBook, and the flag is what
    keeps that honest rather than hidden.
    """

    source = "fixture"

    def __init__(self) -> None:
        self._by_wallet: dict[str, str] = {}
        for human in DEMO_HUMANS:
            for wallet in human.wallets:
                self._by_wallet[wallet.lower()] = human.nullifier

    def lookup(self, wallet: str) -> Registration | None:
        nullifier = self._by_wallet.get(wallet.lower())
        if nullifier is None:
            return None
        return Registration(wallet=wallet, human_id=int(nullifier, 16), sandbox=True)

    def roster(self) -> list[Human]:
        return [
            Human(nullifier=h.nullifier, wallets=tuple(h.wallets), sandbox=True)
            for h in DEMO_HUMANS
        ]


class WorldChainAgentBook:
    """Reads the live AgentBook on World Chain.

    Address and RPC default to the published constants, so this works with no
    configuration at all — an operator should not have to supply a value that is
    the same for everyone.
    """

    source = "world-chain"

    def __init__(self, rpc_url: str | None = None, address: str | None = None, settings=None):
        s = settings or get_settings()
        self.rpc_url = rpc_url or (s.world_rpc_url or WORLD_CHAIN_RPC)
        self.address = address or (s.agentbook_address or AGENTBOOK_ADDRESS)
        self.sandbox = s.humanid_sandbox
        self._w3 = None

    def configured(self) -> bool:
        return bool(self.rpc_url and self.address)

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 20}))
            self._w3 = w3 if w3.is_connected() else None
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("AgentBook: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def _contract(self):
        from web3 import Web3

        return self._connect().eth.contract(
            address=Web3.to_checksum_address(self.address), abi=AGENTBOOK_ABI
        )

    def lookup(self, wallet: str) -> Registration | None:
        """The human behind a wallet, or None if unregistered or unreachable.

        A zero `humanId` is the contract's own "not registered", and it is
        returned as None rather than as a Registration with a zero id — the two
        would otherwise be one keystroke apart downstream, and a zero nullifier
        would derive a cluster id that looked perfectly valid.
        """
        from web3 import Web3

        if not self.configured() or self._connect() is None:
            log.info("AgentBook unreachable — treating %s as unresolved", wallet)
            return None
        try:
            human_id = int(
                self._contract().functions.lookupHuman(
                    Web3.to_checksum_address(wallet)
                ).call()
            )
        except Exception as exc:  # pragma: no cover - live chain
            log.warning("AgentBook lookup failed for %s: %s", wallet, type(exc).__name__)
            return None
        if human_id == 0:
            return None
        return Registration(wallet=wallet, human_id=human_id, sandbox=self.sandbox)

    def roster(self) -> None:
        """AgentBook cannot be enumerated through `lookupHuman`.

        Reconstructing one would mean scanning `AgentRegistered` across World
        Chain's whole history — every human of every app, almost all of them
        nothing to do with this tape. The resolver asks about the payers it
        already has instead, which is both cheaper and narrower.
        """
        return None


def build_agentbook(settings=None) -> AgentBook:
    """The fixture roster unless World Chain is explicitly asked for.

    Explicit opt-in, even though the real reader now needs no configuration: a
    default that silently read mainnet would make a demo's numbers depend on
    whether a stranger had registered a wallet we happen to pay.
    """
    s = settings or get_settings()
    mode = (s.agentbook_mode or "auto").strip().lower()
    if mode == "fixture":
        return FixtureAgentBook()
    if mode == "worldchain":
        return WorldChainAgentBook(settings=s)
    # auto — the real book only when the operator named an endpoint or address.
    if s.world_rpc_url.strip() or s.agentbook_address.strip():
        return WorldChainAgentBook(settings=s)
    return FixtureAgentBook()
