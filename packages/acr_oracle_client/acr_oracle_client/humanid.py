"""Human cluster ids — the grouping key the tape publishes instead of a nullifier.

`HumanIdMirror` never sees a World ID nullifier. What it records is

    clusterId = keccak256(abi.encode(nullifier, SERVICE_SALT, window))

computed here, off chain, by whoever holds the salt. Within one rotation window
the id groups a human's wallets, which is all the cleaning stack needs to cap
them together and all the tape needs to count distinct humans; across windows it
changes, so the durable identifier never reaches ARC and this tape
cannot be joined to another service's data keyed by the same human.

Rotation does NOT make a fleet unlinkable — wallets are the join key and they do
not rotate. See `contracts/src/HumanIdMirror.sol` and `hackathon/ethonline-2026/WORLD-MODULE.md` for
the full argument and for what would actually be required to hide the grouping.

ONE implementation, deliberately. The API derives a cluster id from a verified
proof; the resolver derives the same id when it records the wallet. If those two
disagreed by a byte, the API would look up a cluster the resolver never wrote and
every human would read as unresolved — a silent zero, not an error. So both
import from here, and `tests/test_human_window_parity.py` pins the result against
the contract's own arithmetic.
"""

from __future__ import annotations

import time

#: The rotation period, in seconds. MUST equal `HumanIdMirror.RATING_WINDOW` and
#: `RATING_WINDOW` in `graph/src/parties.ts`. This is the Python source of truth —
#: `index_api.tca` imports it rather than keeping a second copy.
RATING_WINDOW_S = 604800  # 7 days


def window_of(ts: float) -> int:
    """The rotation window a timestamp falls in — `blockTime / RATING_WINDOW`."""
    return int(ts) // RATING_WINDOW_S


def current_window() -> int:
    """The window in progress. The only window `HumanIdMirror.record` accepts."""
    return window_of(time.time())


def as_bytes32(value: bytes | bytearray | str) -> bytes:
    """Normalise a nullifier or salt to exactly 32 bytes.

    Accepts `0x`-prefixed hex or raw bytes, because a nullifier arrives from a
    proof as hex and a salt arrives from the environment as hex, while the
    hashing below wants bytes. Anything that is not exactly 32 bytes raises: a
    short salt silently left-padded would still hash, still look like a cluster
    id, and match nothing on the tape.
    """
    if isinstance(value, str):
        raw = bytes.fromhex(value[2:] if value.startswith(("0x", "0X")) else value)
    else:
        raw = bytes(value)
    if len(raw) != 32:
        raise ValueError(f"expected 32 bytes, got {len(raw)}")
    return raw


def cluster_id(nullifier: bytes | str, salt: bytes | str, window: int) -> bytes:
    """`keccak256(abi.encode(bytes32, bytes32, uint64))`, exactly as Solidity.

    `abi.encode` of three STATIC types is simply their three 32-byte words laid
    end to end: two `bytes32` verbatim, then `uint64` left-padded into a full
    word. So the encoding is a concatenation and needs no ABI library.

    Deliberately NOT `Web3.solidity_keccak`, which implements `encodePacked` —
    that would pack the `uint64` into 8 bytes rather than 32, produce a different
    digest for the same inputs, and fail in the one way that is hardest to
    notice: consistently, and only against the chain.
    """
    from web3 import Web3

    if window < 0 or window > 0xFFFFFFFFFFFFFFFF:
        raise ValueError(f"window {window} does not fit a uint64")
    encoded = as_bytes32(nullifier) + as_bytes32(salt) + window.to_bytes(32, "big")
    return bytes(Web3.keccak(encoded))


def salt_commitment(salt: bytes | str) -> bytes:
    """`keccak256(salt)` — the value the deployed contract holds immutably.

    The salt itself is never published. Committing to it at deploy is what stops
    a resolver retroactively choosing salts to manufacture whichever grouping
    flattered a number, and lets a later audit verify one salt was used
    throughout without revealing it.
    """
    from web3 import Web3

    return bytes(Web3.keccak(as_bytes32(salt)))


# --- the client -------------------------------------------------------------
# Derivation above, the chain below — the same split `mirror.py` uses, so one
# module covers one subject.

import logging  # noqa: E402

from acr_core import get_settings  # noqa: E402

from .signer import Signer, build_role_signer  # noqa: E402

log = logging.getLogger("acr_oracle_client.humanid")

RESOLUTION_TYPES = {
    "HumanCluster": [
        {"name": "wallet", "type": "address"},
        {"name": "clusterId", "type": "bytes32"},
        {"name": "window", "type": "uint64"},
        {"name": "sandbox", "type": "bool"},
    ]
}

HUMANID_ABI = [
    {
        "type": "function",
        "name": "record",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "wallet", "type": "address"},
            {"name": "clusterId", "type": "bytes32"},
            {"name": "window", "type": "uint64"},
            {"name": "sandbox", "type": "bool"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "resolutionDigest",
        "stateMutability": "view",
        "inputs": [
            {"name": "wallet", "type": "address"},
            {"name": "clusterId", "type": "bytes32"},
            {"name": "window", "type": "uint64"},
            {"name": "sandbox", "type": "bool"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "currentWindow",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint64"}],
    },
    {
        "type": "function",
        "name": "clusterOf",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}, {"name": "", "type": "uint64"}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "clusterProvenance",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint8"}],
    },
    {
        "type": "function",
        "name": "isSigner",
        "stateMutability": "view",
        "inputs": [{"name": "", "type": "address"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "function",
        "name": "SALT_COMMITMENT",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
]


class HumanIdMirrorClient:
    """Records wallet→human resolutions. Offline-tolerant, like MirrorClient."""

    def __init__(
        self,
        rpc_url: str | None = None,
        mirror_address: str | None = None,
        signer: Signer | None = None,
        settings=None,
    ) -> None:
        s = settings or get_settings()
        self.rpc_url = rpc_url or s.arc_rpc_url
        self.mirror_address = mirror_address or (s.humanid_mirror_address or None)
        # The press's own signer again: one trust anchor for prints, for the
        # settlements benchmarked against them, and for who is one human.
        self.signer = signer or build_role_signer("poster", s)
        self._w3 = None

    def configured(self) -> bool:
        """Able to WRITE — an address and a signer. What the resolver needs."""
        return bool(self.mirror_address and self.signer)

    def readable(self) -> bool:
        """Able to READ. An address is enough, because `cluster_of` is a view.

        SEPARATE FROM `configured()` BECAUSE A READER MUST NOT NEED A KEY. The
        gate verifies human claims by calling `cluster_of` and never writes, so
        gating it on `configured()` made the human tier unreachable on exactly the
        deployment that should have it — production, which has no business holding
        a key that can write to this mirror. Asking for one to answer a view call
        is how a security boundary gets widened for no reason.

        An unreachable RPC is not covered here and must not be: `cluster_of`
        raises, and the caller reports the claim as unverifiable. Folding
        connectivity into this predicate would turn a transient outage into
        "the mirror is not configured", which sends an operator to fix the wrong
        thing.
        """
        return bool(self.mirror_address)

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 20}))
            self._w3 = w3 if w3.is_connected() else None
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("HumanIdMirrorClient: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def _contract(self):
        from web3 import Web3

        return self._connect().eth.contract(
            address=Web3.to_checksum_address(self.mirror_address), abi=HUMANID_ABI
        )

    def _domain(self, chain_id: int) -> dict:
        from web3 import Web3

        return {
            "name": "ACR Human Id Mirror",
            "version": "1",
            "chainId": int(chain_id),
            "verifyingContract": Web3.to_checksum_address(self.mirror_address),
        }

    def _agrees_with_chain(self, domain: dict, message: dict) -> bool:
        """Does our EIP-712 digest equal the one the CONTRACT computes?

        Stronger than recovering our own signature, which only proves we agree
        with ourselves. `resolutionDigest` is a public view for exactly this: an
        encoding mistake here would recover to a stranger on chain and the only
        symptom would be "bad signer" on a call that should have worked, after
        the gas was spent.
        """
        from eth_account.messages import encode_typed_data
        from eth_utils import keccak

        signable = encode_typed_data(
            domain_data=domain, message_types=RESOLUTION_TYPES, message_data=message
        )
        ours = keccak(b"\x19" + signable.version + signable.header + signable.body)
        theirs = self._contract().functions.resolutionDigest(
            message["wallet"], message["clusterId"], message["window"], message["sandbox"]
        ).call()
        return bytes(ours) == bytes(theirs)

    def _send(self, fn, wait: bool = True) -> str:
        w3 = self._connect()
        tx = fn.build_transaction(
            {
                "from": self.signer.address,
                "nonce": w3.eth.get_transaction_count(self.signer.address),
                "chainId": w3.eth.chain_id,
            }
        )
        tx_hash = self.signer.send_transaction(w3, tx)
        if wait:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
            if rcpt.status != 1:
                raise RuntimeError(f"humanid tx reverted ({tx_hash})")
        return str(tx_hash)

    def chain_window(self) -> int | None:
        """The window the CONTRACT is in, not the one this host's clock is in.

        `record` requires `window == currentWindow()` computed from
        `block.timestamp`. A resolver that used its own clock would sign for the
        wrong window near a boundary — or on any host whose time has drifted —
        and every call would revert for a reason that looks nothing like the
        cause.
        """
        if not self.readable() or self._connect() is None:
            return None
        return int(self._contract().functions.currentWindow().call())

    def cluster_of(self, wallet: str, window: int) -> bytes | None:
        """What the chain already records for this wallet in this window.

        GATED ON `readable()`, AND THE DIFFERENCE IS NOT COSMETIC. While this asked
        `configured()` it returned None whenever no WRITE key was present — and None
        here means "this wallet belongs to no human", so a missing credential was
        indistinguishable from an unresolved wallet. Every surface downstream would
        have reported nobody as verified while every call succeeded: the same silent
        zero that `salt_matches` returning False exists to make loud.
        """
        if not self.readable() or self._connect() is None:
            return None
        from web3 import Web3

        got = self._contract().functions.clusterOf(
            Web3.to_checksum_address(wallet), int(window)
        ).call()
        return None if got == b"\x00" * 32 else bytes(got)

    def salt_matches(self, salt: bytes | str) -> bool | None:
        """Does the configured salt hash to the deployed commitment?

        None when the chain is unreachable. False is the one worth stopping for:
        a wrong salt derives cluster ids that match nothing, so the resolver
        would enroll everybody into clusters the API will never look up, and
        every surface would read "no humans" while every transaction succeeded.
        """
        if not self.readable() or self._connect() is None:
            return None
        deployed = bytes(self._contract().functions.SALT_COMMITMENT().call())
        return deployed == salt_commitment(salt)

    def signer_authorized(self) -> bool | None:
        """Is our signer in the contract's signer set? None if unreachable.

        A dry run that only checked the digest would report success for a key the
        contract will reject: the digest is a statement about ENCODING, and
        authorization is a different fact entirely. Getting that wrong costs a
        reverted transaction and its gas, and the revert reason ("bad signer")
        points at the signature rather than at the signer set, which is the wrong
        place to start looking.
        """
        if not self.configured() or self._connect() is None:
            return None
        from web3 import Web3

        return bool(
            self._contract().functions.isSigner(
                Web3.to_checksum_address(self.signer.address)
            ).call()
        )

    def record(
        self,
        wallet: str,
        cluster: bytes,
        window: int,
        sandbox: bool,
        dry_run: bool = False,
    ) -> str | None:
        """Record one wallet into one human's cluster. Returns a tx hash, or None."""
        from web3 import Web3

        message = {
            "wallet": Web3.to_checksum_address(wallet),
            "clusterId": cluster,
            "window": int(window),
            "sandbox": bool(sandbox),
        }
        if not self.configured() or self._connect() is None:
            log.info("HumanIdMirrorClient offline: would record %s", wallet)
            return None

        chain_id = self._connect().eth.chain_id
        domain = self._domain(chain_id)
        v, r, s_ = self.signer.sign_typed_data(
            domain, RESOLUTION_TYPES, message, "HumanCluster"
        )
        if not self._agrees_with_chain(domain, message):
            raise RuntimeError(
                "our resolution digest disagrees with the contract's — not broadcasting"
            )
        if dry_run:
            return None
        c = self._contract()
        return self._send(
            c.functions.record(
                message["wallet"], message["clusterId"], message["window"],
                message["sandbox"], v, r, s_,
            )
        )


# --- one window's resolution, shared by the script and the press ------------
# `scripts/resolve_humans.py` was the only place this loop lived, and `scripts/`
# is not in the press image, so a host holding every credential the chore needs
# still could not run it. On 2026-10-16 the last resolved window was 2958, five
# weeks back: a weekly laptop chore had simply stopped happening.

from collections.abc import Iterable, Iterator  # noqa: E402
from dataclasses import dataclass  # noqa: E402


@dataclass(frozen=True)
class Resolution:
    """What happened to one wallet in one window."""

    wallet: str
    nullifier: str
    cluster: bytes
    sandbox: bool
    #: "already" (on chain, skipped), "recorded" (broadcast), "dry-run" (signed only).
    status: str
    tx: str | None = None


def roster_pairs(book) -> list[tuple[str, str, bool]] | None:
    """(wallet, nullifier, sandbox) for every wallet of every human the book can
    list, or None when it cannot list its humans (World Chain's AgentBook)."""
    roster = book.roster()
    if roster is None:
        return None
    return [(w, h.nullifier, h.sandbox) for h in roster for w in h.wallets]


def resolve_window(
    client: HumanIdMirrorClient,
    pairs: Iterable[tuple[str, str, bool]],
    salt: str,
    window: int,
    *,
    dry_run: bool,
) -> Iterator[Resolution]:
    """Record each wallet's cluster for ``window``, skipping any already on chain.

    A generator, so a caller can report each wallet as its transaction lands: a
    Circle-signed record blocks for up to two minutes, and four of them in
    silence reads as a hang. Idempotent by construction (`cluster_of` is read
    first, and the contract makes an identical repeat a no-op), so a rerun after
    a partial failure finishes the job instead of duplicating it.
    """
    for wallet, nullifier, sandbox in pairs:
        cluster = cluster_id(nullifier, salt, window)
        if client.cluster_of(wallet, window) == cluster:
            yield Resolution(wallet, nullifier, cluster, sandbox, "already")
            continue
        tx = client.record(wallet, cluster, window, sandbox, dry_run=dry_run)
        yield Resolution(wallet, nullifier, cluster, sandbox, "dry-run" if dry_run else "recorded", tx)
