"""Sign ACR prints (EIP-712) and post them to ``ACROracle.sol``.

Bridges the Python estimator to the on-chain oracle. Each print is signed with
the EIP-712 typed-data scheme the contract verifies (domain ``ACR Oracle`` v1,
``Print`` struct); the signature — not the sender — authenticates the print, so
any relayer may submit it. Prices are scaled to WAD (1e18) and the attack cost
to USDC-6 (1e6) to match the contract's storage convention. Like ``ArcSource``,
this degrades gracefully: without a configured RPC/private key it returns an
unsigned, unsent payload so the rest of the pipeline (and the demo) still runs
offline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from acr_core import ACRPrint, get_settings

from .signer import Signer, build_signer


def _is_revert(exc: Exception) -> bool:
    """A contract revert (an answer: "no print") as opposed to a transport failure."""
    try:
        from web3.exceptions import ContractLogicError
    except Exception:  # pragma: no cover - web3 is a dependency
        ContractLogicError = ()  # type: ignore[assignment]
    return isinstance(exc, ContractLogicError) or "revert" in str(exc).lower()

log = logging.getLogger("acr_oracle_client")

WAD = 10**18
USDC = 10**6


def index_id_to_bytes32(index_id: str) -> bytes:
    raw = index_id.encode("utf-8")
    if len(raw) > 32:  # pragma: no cover - defensive
        raise ValueError("index id too long for bytes32")
    return raw.ljust(32, b"\x00")


def to_wad(x: float) -> int:
    """Scale to the contract's 1e18 fixed point.

    Python's ``round`` is half-to-EVEN, so ``to_usdc(2.5e-6) == 2`` and
    ``to_usdc(1.5e-6) == 2``. That is the documented tie rule, not an accident:
    half-up would bias every rounded print in one direction, and over a year of
    hourly prints a one-sided tie rule is a drift.

    Note the scaling adds precision the float never had. A float64 near 0.5
    carries ~16 significant digits; the WAD integer has 18, so the last two
    decimal digits of every posted price are padding. That is harmless for a
    published rate and fatal for anything that must round-trip — never reverse
    this to reconstruct an input.
    """
    return int(round(x * WAD))


def to_usdc(x: float) -> int:
    """Scale to USDC's 1e6 fixed point. Half-to-even, as ``to_wad``."""
    return int(round(x * USDC))


@dataclass
class PostPayload:
    """The exact calldata a ``postPrint`` transaction carries."""

    index_id: bytes
    value: int
    ci_lo: int
    ci_hi: int
    attack_cost_per_bp: int
    timestamp: int
    #: v2 only; None on a v1 payload and never sent to a v1 oracle.
    human_adjusted_bound: int | None = None
    policy_hash: bytes | None = None
    window_start: int | None = None
    window_end: int | None = None

    @classmethod
    def from_print(cls, p: ACRPrint) -> PostPayload:
        # A human bound of None means "not computed" and is carried to chain as
        # 0, which the contract reads as the same thing. It is NOT defaulted to
        # the wallet bound: that would publish a number claiming humans are as
        # cheap to buy as wallets, which is false.
        # Each of the three is rounded INDEPENDENTLY, so a float triple that
        # satisfies ci_lo <= value <= ci_hi can come out of the scaling inverted
        # when two of them sit within one WAD ulp — which is exactly the case
        # `pipeline.py` produces on purpose, since it clamps ci_lo to value. The
        # contract rejects that with "value outside CI", so the ordering is
        # re-established here rather than discovered on a reverting broadcast.
        value = to_wad(p.value)
        ci_lo = min(to_wad(p.ci_lo), value)
        ci_hi = max(to_wad(p.ci_hi), value)
        return cls(
            index_id=index_id_to_bytes32(p.index_id),
            value=value,
            ci_lo=ci_lo,
            ci_hi=ci_hi,
            attack_cost_per_bp=max(1, to_usdc(p.attack_cost_per_bp or 0.0)),
            # Truncation, not rounding: rounding a fractional economic timestamp
            # UP could push it past the contract's MAX_TS_SKEW, and the Fixing
            # clock is integral by construction anyway.
            timestamp=int(p.ts),
            human_adjusted_bound=(
                None if p.human_adjusted_bound is None else to_usdc(p.human_adjusted_bound)
            ),
            policy_hash=(bytes.fromhex(p.policy_hash[2:]) if p.policy_hash else None),
            window_start=(None if p.window_start is None else int(p.window_start)),
            window_end=(None if p.window_end is None else int(p.window_end)),
        )

    def message(self, schema: OracleSchema | None = None) -> dict:
        """The signed message, in typehash order.

        THE one place field order lives. It feeds the EIP-712 message, the
        calldata tuple and the digest-parity test, so those three cannot drift
        apart — which is the failure mode that recovers a signature to a
        stranger and shows up only as "bad signer" on a call that should have
        worked.

        v2 APPENDS: the v1 six are a strict prefix, so nothing in the middle
        moves and no existing test vector shifts.
        """
        m = {
            "indexId": self.index_id,
            "value": self.value,
            "ciLo": self.ci_lo,
            "ciHi": self.ci_hi,
            "attackCostPerBp": self.attack_cost_per_bp,
            "timestamp": self.timestamp,
        }
        if schema is not None and schema.version == "2":
            if not self.policy_hash:
                raise ValueError("a v2 print must name the cleaning policy it was made under")
            if self.window_start is None or self.window_end is None:
                raise ValueError("a v2 print must state the window it summarizes")
            m["humanAdjustedBound"] = self.human_adjusted_bound or 0
            m["policyHash"] = self.policy_hash
            m["windowStart"] = self.window_start
            m["windowEnd"] = self.window_end
        return m

    def as_args(self, schema: OracleSchema | None = None) -> tuple:
        """The signed fields as a tuple, in typehash order. Dicts are ordered,
        so this cannot disagree with ``message``."""
        return tuple(self.message(schema).values())

    def signed_args(self, v: int, r: bytes, s: bytes, schema: OracleSchema | None = None) -> tuple:
        """Full ``postPrint`` calldata.

        v1 takes the six fields flat; v2 takes them as one struct, because ten
        signed fields plus a signature will not fit the stack otherwise.
        """
        args = self.as_args(schema)
        if schema is not None and schema.version == "2":
            return (args, v, r, s)
        return (*args, v, r, s)


#: EIP-712 typed-data schema for a print — must match ``ACROracle.PRINT_TYPEHASH``
#: byte-for-byte. ``EIP712Domain`` is supplied by ``sign_typed_data`` from the
#: domain dict, so it is intentionally absent here.
PRINT_TYPES = {
    "Print": [
        {"name": "indexId", "type": "bytes32"},
        {"name": "value", "type": "uint256"},
        {"name": "ciLo", "type": "uint256"},
        {"name": "ciHi", "type": "uint256"},
        {"name": "attackCostPerBp", "type": "uint256"},
        {"name": "timestamp", "type": "uint64"},
    ]
}


#: v2 APPENDS four fields. The v1 list is the literal prefix, so a v2 payload is
#: the v1 tuple plus four and nothing in the middle moves.
PRINT_TYPES_V2 = {
    "Print": [
        *PRINT_TYPES["Print"],
        {"name": "humanAdjustedBound", "type": "uint256"},
        {"name": "policyHash", "type": "bytes32"},
        {"name": "windowStart", "type": "uint64"},
        {"name": "windowEnd", "type": "uint64"},
    ]
}

#: v2's on-chain surface. `postPrint` and `printDigest` take the ten signed
#: fields as ONE struct — ten fields plus a signature will not fit the stack
#: flat — so the calldata shape differs from v1 even though the field order does
#: not. `_V2_PRINT_TUPLE` is that struct, and it is also the return shape of
#: `latestPrint`/`historyAt` plus `postedAt`/`exists`.
_V2_INPUT_COMPONENTS = [
    {"name": "indexId", "type": "bytes32"},
    {"name": "value", "type": "uint256"},
    {"name": "ciLo", "type": "uint256"},
    {"name": "ciHi", "type": "uint256"},
    {"name": "attackCostPerBp", "type": "uint256"},
    {"name": "timestamp", "type": "uint64"},
    {"name": "humanAdjustedBound", "type": "uint256"},
    {"name": "policyHash", "type": "bytes32"},
    {"name": "windowStart", "type": "uint64"},
    {"name": "windowEnd", "type": "uint64"},
]
_V2_PRINT_COMPONENTS = [
    {"name": "value", "type": "uint256"},
    {"name": "ciLo", "type": "uint256"},
    {"name": "ciHi", "type": "uint256"},
    {"name": "attackCostPerBp", "type": "uint256"},
    {"name": "humanAdjustedBound", "type": "uint256"},
    {"name": "policyHash", "type": "bytes32"},
    {"name": "windowStart", "type": "uint64"},
    {"name": "windowEnd", "type": "uint64"},
    {"name": "timestamp", "type": "uint64"},
    {"name": "postedAt", "type": "uint64"},
    {"name": "exists", "type": "bool"},
]

ORACLE_ABI_V2 = [
    {
        "type": "function",
        "name": "firstPrintPostedAtOrAfter",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}, {"name": "fromPostedAt", "type": "uint64"}],
        "outputs": [
            {"name": "found", "type": "bool"},
            {"name": "value", "type": "uint256"},
            {"name": "postedAt", "type": "uint64"},
        ],
    },

    {
        "name": "postPrint",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "p", "type": "tuple", "components": _V2_INPUT_COMPONENTS},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "printDigest",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "p", "type": "tuple", "components": _V2_INPUT_COMPONENTS}],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "name": "latestPrint",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "tuple", "components": _V2_PRINT_COMPONENTS}],
    },
    {
        "name": "latestValue",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "printMeta",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [
            {"name": "policyHash", "type": "bytes32"},
            {"name": "windowStart", "type": "uint64"},
            {"name": "windowEnd", "type": "uint64"},
            {"name": "humanAdjustedBound", "type": "uint256"},
        ],
    },
    {
        "name": "historyLength",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "historyAt",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}, {"name": "i", "type": "uint256"}],
        "outputs": [{"name": "", "type": "tuple", "components": _V2_PRINT_COMPONENTS}],
    },
]


@dataclass(frozen=True)
class OracleSchema:
    """Everything that differs between oracle generations, in one object.

    Two oracles run side by side during the migration — v1 because
    ``ACRFutures`` settles against it and cannot be repointed, v2 because it
    carries the policy hash and window that make a print reproducible. Holding
    the difference in one value means the signing path, the poster and the
    backfill all stay single-implementation.
    """

    version: str
    types: dict
    abi: list
    #: `recent_posts` filters logs by this signature; v2's differs, so a reader
    #: pointed at both can never decode one oracle's event as the other's.
    event_signature: str


def print_domain(chain_id: int, oracle_address: str, version: str = "1") -> dict:
    """The EIP-712 domain the oracle constructs in its constructor.

    ``version`` is DEFAULTED so every existing caller and test keeps signing
    exactly what it signed before; only a v2 client passes "2".
    """
    from web3 import Web3

    return {
        "name": "ACR Oracle",
        "version": version,
        "chainId": int(chain_id),
        "verifyingContract": Web3.to_checksum_address(oracle_address),
    }


def sign_print(
    payload: PostPayload, chain_id: int, oracle_address: str, private_key: str
) -> tuple[int, bytes, bytes]:
    """EIP-712-sign a print. Returns ``(v, r, s)`` ready for ``postPrint``."""
    from eth_account import Account

    message = {
        "indexId": payload.index_id,
        "value": payload.value,
        "ciLo": payload.ci_lo,
        "ciHi": payload.ci_hi,
        "attackCostPerBp": payload.attack_cost_per_bp,
        "timestamp": payload.timestamp,
    }
    signed = Account.sign_typed_data(
        private_key,
        domain_data=print_domain(chain_id, oracle_address),
        message_types=PRINT_TYPES,
        message_data=message,
    )
    return int(signed.v), int(signed.r).to_bytes(32, "big"), int(signed.s).to_bytes(32, "big")


# ABI fragment: postPrint (signed) + read-back + staleness views.
_PRINT_TUPLE = {
    "type": "tuple",
    "name": "",
    "components": [
        {"name": "value", "type": "uint256"},
        {"name": "ciLo", "type": "uint256"},
        {"name": "ciHi", "type": "uint256"},
        {"name": "attackCostPerBp", "type": "uint256"},
        {"name": "timestamp", "type": "uint64"},
        {"name": "postedAt", "type": "uint64"},
        {"name": "exists", "type": "bool"},
    ],
}
ORACLE_ABI = [
    {
        "type": "function",
        "name": "firstPrintPostedAtOrAfter",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}, {"name": "fromPostedAt", "type": "uint64"}],
        "outputs": [
            {"name": "found", "type": "bool"},
            {"name": "value", "type": "uint256"},
            {"name": "postedAt", "type": "uint64"},
        ],
    },

    # The backfill reads v1's history on chain rather than over logs — Arc caps
    # eth_getLogs at ~15k blocks, so paging a year of prints would be dozens of
    # round trips against a throttled RPC. These two were missing until v2
    # needed them.
    {
        "name": "historyLength",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "historyAt",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}, {"name": "i", "type": "uint256"}],
        "outputs": [
            {
                "name": "",
                "type": "tuple",
                "components": [
                    {"name": "value", "type": "uint256"},
                    {"name": "ciLo", "type": "uint256"},
                    {"name": "ciHi", "type": "uint256"},
                    {"name": "attackCostPerBp", "type": "uint256"},
                    {"name": "timestamp", "type": "uint64"},
                    {"name": "postedAt", "type": "uint64"},
                    {"name": "exists", "type": "bool"},
                ],
            }
        ],
    },

    {
        "type": "function",
        "name": "postPrint",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "indexId", "type": "bytes32"},
            {"name": "value", "type": "uint256"},
            {"name": "ciLo", "type": "uint256"},
            {"name": "ciHi", "type": "uint256"},
            {"name": "attackCostPerBp", "type": "uint256"},
            {"name": "timestamp", "type": "uint64"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "type": "function",
        "name": "printDigest",
        "stateMutability": "view",
        "inputs": [
            {"name": "indexId", "type": "bytes32"},
            {"name": "value", "type": "uint256"},
            {"name": "ciLo", "type": "uint256"},
            {"name": "ciHi", "type": "uint256"},
            {"name": "attackCostPerBp", "type": "uint256"},
            {"name": "timestamp", "type": "uint64"},
        ],
        "outputs": [{"name": "", "type": "bytes32"}],
    },
    {
        "type": "function",
        "name": "latestPrint",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [_PRINT_TUPLE],
    },
    {
        "type": "function",
        "name": "latestValue",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "latestPrintWithAge",
        "stateMutability": "view",
        "inputs": [{"name": "indexId", "type": "bytes32"}],
        "outputs": [_PRINT_TUPLE, {"name": "age", "type": "uint256"}],
    },
    {
        "type": "function",
        "name": "isStale",
        "stateMutability": "view",
        "inputs": [
            {"name": "indexId", "type": "bytes32"},
            {"name": "maxAge", "type": "uint256"},
        ],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "type": "event",
        "name": "PricePosted",
        "anonymous": False,
        "inputs": [
            {"name": "indexId", "type": "bytes32", "indexed": True},
            {"name": "value", "type": "uint256", "indexed": False},
            {"name": "ciLo", "type": "uint256", "indexed": False},
            {"name": "ciHi", "type": "uint256", "indexed": False},
            {"name": "attackCostPerBp", "type": "uint256", "indexed": False},
            {"name": "timestamp", "type": "uint64", "indexed": False},
            {"name": "signer", "type": "address", "indexed": True},
        ],
    },
]


ORACLE_V1 = OracleSchema(
    version="1",
    types=PRINT_TYPES,
    abi=ORACLE_ABI,
    event_signature="PricePosted(bytes32,uint256,uint256,uint256,uint256,uint64,address)",
)

ORACLE_V2 = OracleSchema(
    version="2",
    types=PRINT_TYPES_V2,
    abi=ORACLE_ABI_V2,
    event_signature=(
        "PricePosted(bytes32,bytes32,address,uint256,uint256,uint256,uint256,uint256,"
        "uint64,uint64,uint64)"
    ),
)


class OracleClient:
    def __init__(
        self,
        rpc_url: str | None = None,
        oracle_address: str | None = None,
        private_key: str | None = None,
        signer: Signer | None = None,
        schema: OracleSchema | None = None,
    ) -> None:
        settings = get_settings()
        # Defaults to v1, so every existing caller, script and test signs and
        # decodes exactly what it did before. Only the v2 poster passes ORACLE_V2.
        self.schema = schema or ORACLE_V1
        self.rpc_url = rpc_url or settings.arc_rpc_url
        self.oracle_address = oracle_address or (settings.oracle_address or None)
        # Back-compat: a passed/settings raw key builds a LocalKeySigner; else a
        # Circle wallet signer if creds are configured; else None (offline).
        self.private_key = private_key or (settings.poster_private_key or None)
        self.signer = signer or build_signer(settings, private_key=self.private_key)
        self._w3 = None
        #: Receipt of the most recent successful ``post`` ({tx, block, gas_used});
        #: None while offline — the poster reads this for on-chain provenance.
        self.last_receipt: dict | None = None
        #: Why the last post was skipped, or None. Set by the gas policy in
        #: `post()` so callers can RECORD the reason instead of reporting a
        #: silent no-op — /ops and the keeper's chore log both read it.
        self.last_skip_reason: str | None = None

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            self._w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 5}))
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("OracleClient: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def can_post(self) -> bool:
        w3 = self._connect()
        if w3 is None or not self.oracle_address or self.signer is None:
            return False
        try:
            return bool(w3.is_connected())
        except Exception:  # pragma: no cover - env dependent
            return False

    def print_gas_shortfall(self) -> str | None:
        """Why this print must NOT be posted, or None when it may be.

        Deliberately separate from :meth:`can_post`, which three callers use to
        mean "configured and online" (``app.py``, ``scripts/post_once.py``).
        Folding a balance into that would quietly change what those test.

        The press's two jobs are not equally recoverable. A missed print is a
        gap the next print fills. A missed receipt mirror is lost EVIDENCE: an
        unmirrored settlement never reaches the subgraph, so ``/tca`` can never
        see it. They come out of one wallet on Arc, where USDC is the gas, so
        without a priority the press spends to zero and loses the irreplaceable
        one alongside the recoverable one. Prints yield first; mirroring is
        never gated on this.

        Returns a sentence rather than a bool because the caller records it —
        a press that stops silently cost this project a 48.9-hour outage, and
        "stopped" is not a diagnosis.
        """
        floor = float(get_settings().press_print_floor_usdc or 0.0)
        if floor <= 0:
            return None  # explicitly disabled
        w3 = self._connect()
        if w3 is None or self.signer is None:
            return None  # not our call; can_post() already covers this
        try:
            bal = w3.eth.get_balance(w3.to_checksum_address(self.signer.address)) / 1e18
        except Exception:  # pragma: no cover - env dependent
            # A balance we cannot read is not evidence of an empty wallet, and
            # refusing to print because the RPC blinked would be the worse bug.
            return None
        if bal >= floor:
            return None
        return (
            f"print skipped: {bal:.4f} USDC is below the {floor:.2f} print floor — "
            "remaining gas is reserved for receipt mirroring, which cannot be redone"
        )

    def _contract(self):  # pragma: no cover - requires live chain
        w3 = self._connect()
        return w3.eth.contract(
            address=w3.to_checksum_address(self.oracle_address), abi=self.schema.abi
        )

    def recent_posts(  # pragma: no cover - live chain
        self, lookback_blocks: int | None = None, limit: int = 60, pages: int | None = None
    ) -> list[dict]:
        """Recent ``PricePosted`` events, chronological — the settlement
        provenance (tx / block / signer + the block's wall clock) that survives
        process restarts.

        PAGES backwards, for the same reason the trade tape does: Arc caps an
        ``eth_getLogs`` range at ~15000 blocks (413) regardless of how few logs
        match, so reach cannot be bought by widening the window. The old single
        10000-block window was ~1.42h against an **hourly** poster — no margin
        at all, so one late or dropped post put the provenance out of reach and
        /health went to `poster_last_tx: null` while real posts sat on chain.
        """
        from .futures import (  # lazy: futures imports client
            TAPE_PAGE_BLOCKS,
            TAPE_PAGES,
            _is_range_error,
            _rpc_retry,
            bytes32_to_index_id,
        )

        page_blocks = TAPE_PAGE_BLOCKS if lookback_blocks is None else lookback_blocks
        page_budget = TAPE_PAGES if pages is None else pages
        w3 = self._connect()
        if w3 is None or not self.oracle_address:
            return []
        try:
            c = self._contract()
            latest = int(_rpc_retry(lambda: w3.eth.block_number))
            sig = w3.keccak(text=self.schema.event_signature).hex()
            topic0 = sig if sig.startswith("0x") else "0x" + sig

            def _fetch(start: int, end: int) -> list:
                return _rpc_retry(
                    lambda: w3.eth.get_logs({
                        "address": c.address,
                        "fromBlock": start,
                        "toBlock": end,
                        "topics": [topic0],
                    }),
                    tries=5 if end == latest else 2,
                )

            logs: list = []
            end = latest
            for _ in range(max(1, page_budget)):
                if end <= 0:
                    break
                page = None
                start = max(0, end - page_blocks)
                for span in (page_blocks, 5000, 2500):
                    start = max(0, end - span)
                    try:
                        page = _fetch(start, end)
                        break
                    except Exception as exc:
                        # Narrowing cures a range refusal, never a throttle.
                        if not _is_range_error(exc):
                            break
                        page = None
                if page is None:
                    break  # keep what we have rather than crawl on a throttle
                logs = [c.events.PricePosted().process_log(log) for log in page] + logs
                if len(logs) >= limit or start <= 0:
                    break
                end = start - 1
            out: list[dict] = []
            block_ts: dict[int, float] = {}
            for ev in logs[-limit:]:
                args = ev["args"]
                block = int(ev["blockNumber"])
                # at_wall is the block's wall clock — the event's ``timestamp``
                # is the print's DATA timestamp (sim-relative in this pipeline).
                if block not in block_ts:
                    block_ts[block] = float(
                        _rpc_retry(lambda b=block: w3.eth.get_block(b).timestamp)
                    )
                out.append({
                    "index_id": bytes32_to_index_id(args["indexId"]),
                    "tx": w3.to_hex(ev["transactionHash"]),
                    "block": block,
                    "signer": args["signer"],
                    "at_wall": block_ts[block],
                })
            return out
        except Exception:
            return []

    def post(self, p: ACRPrint, wait: bool = True) -> str | None:
        """Post a print; returns the tx hash hex, or None if offline.

        Offline is the expected path in local demos — the payload is still
        constructed and logged so behavior is observable without a chain.
        """
        payload = PostPayload.from_print(p)
        if not self.can_post():
            log.info("OracleClient offline: would post %s -> %s", p.index_id, payload.as_args())
            self.last_receipt = None
            return None
        # Gas policy, after "are we online" and before we spend anything.
        shortfall = self.print_gas_shortfall()
        if shortfall:
            log.warning("%s (%s)", shortfall, p.index_id)
            self.last_receipt = None
            self.last_skip_reason = shortfall
            return None
        self.last_skip_reason = None
        w3 = self._connect()  # pragma: no cover - requires live chain
        contract = self._contract()
        # EIP-712-sign the print via the signer (raw key or Circle custody); the
        # contract verifies the *signer*, not the sender, so any relayer submits.
        chain_id = w3.eth.chain_id
        # One ordered message, from the one place field order lives.
        message = payload.message(self.schema)
        v, r, s = self.signer.sign_typed_data(
            print_domain(chain_id, self.oracle_address, self.schema.version),
            self.schema.types,
            message,
            "Print",
        )
        # Build with the signer's address as `from`; "gas" is omitted so web3
        # estimates it (the first post per index does ~12 cold SSTOREs).
        tx = contract.functions.postPrint(
            *payload.signed_args(v, r, s, self.schema)
        ).build_transaction(
            {
                "from": self.signer.address,
                "nonce": w3.eth.get_transaction_count(self.signer.address),
                "chainId": chain_id,
            }
        )
        tx_hash = self.signer.send_transaction(w3, tx)
        if wait:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=30)
            if rcpt.status != 1:
                raise RuntimeError(f"postPrint reverted for {p.index_id} (tx {tx_hash})")
            # None-safe receipt provenance (some RPCs omit fields on early reads).
            block = rcpt.get("blockNumber")
            gas = rcpt.get("gasUsed")
            self.last_receipt = {
                "tx": str(tx_hash),
                "block": int(block) if block is not None else None,
                "gas_used": int(gas) if gas is not None else None,
            }
        else:
            self.last_receipt = {"tx": str(tx_hash), "block": None, "gas_used": None}
        return tx_hash

    def _print_field_names(self) -> list[str]:
        """The ``latestPrint`` tuple's field names, taken from THIS client's
        schema rather than assumed.

        v2 returns eleven fields where v1 returns seven, and the four new ones
        sit *before* ``timestamp`` — so a positional unpack written against v1
        does not mislabel a v2 read, it raises, and ``read_latest``'s
        except-branch turns that into a silent ``None`` indistinguishable from
        "no print yet". That is not hypothetical: it left the backfill's resume
        floor at 0 so a re-run re-posted every print it had already posted, and
        it made ``verify_live`` report an oracle holding 131 prints as empty.
        """
        for entry in self.schema.abi:
            if entry.get("name") != "latestPrint":
                continue
            outputs = entry.get("outputs") or []
            if len(outputs) == 1 and outputs[0].get("type") == "tuple":
                return [c["name"] for c in outputs[0]["components"]]
            return [o.get("name", "") for o in outputs]
        return []

    def read_first_print_posted_at_or_after(
        self, index_id: str, from_posted_at: int
    ) -> dict | None:  # pragma: no cover - live chain
        """The first print the chain saw at or after `from_posted_at` — the venue's
        settlement print for a series expiring then. None when no such print
        exists, and None on an oracle deployed before the view did (the call
        reverts there): callers treat both as "the new rule cannot be evaluated".
        """
        if self._connect() is None or not self.oracle_address:
            return None
        try:
            found, value, posted_at = self._contract().functions.firstPrintPostedAtOrAfter(
                index_id_to_bytes32(index_id), int(from_posted_at)
            ).call()
        except Exception:
            return None
        if not found:
            return None
        return {"index_id": index_id, "value": value / WAD, "posted_at": int(posted_at)}

    def read_latest(self, index_id: str, *, strict: bool = False) -> dict | None:
        """Read back the latest on-chain print for ``index_id`` (WAD-descaled).

        The v1 keys come back unchanged whatever the schema, so every existing
        caller is untouched; a v2 print ADDS the four fields that make it
        reproducible (``human_adjusted_bound``, ``policy_hash``,
        ``window_start``, ``window_end``).

        ``strict``: None means only "the contract has no print" (it reverts
        ``"no print"``). A throttled or unreachable RPC raises instead, after
        retrying 429s. The default treats every failure as no print, which is
        how a 429 reached readers as ``404 no on-chain print`` (2026-10-14).
        """
        if not self.oracle_address:
            return None
        if self._connect() is None:
            if strict:
                raise ConnectionError("oracle RPC unreachable")
            return None
        c = self._contract()
        idx = index_id_to_bytes32(index_id)
        try:
            call = c.functions.latestPrint(idx).call
            if strict:
                from .futures import _rpc_retry  # lazy: futures imports client

                row = _rpc_retry(call, tries=3)
            else:
                row = call()
        except Exception as exc:
            if strict and not _is_revert(exc):
                raise
            return None  # contract reverts "no print" when none exists yet
        # strict: a length mismatch means the ABI and the deployed contract
        # disagree, which must be loud. Swallowing it is the bug above.
        fields = dict(zip(self._print_field_names(), row, strict=True))
        if not fields.get("exists"):
            return None
        out = {
            "index_id": index_id,
            "value": fields["value"] / WAD,
            "ci_lo": fields["ciLo"] / WAD,
            "ci_hi": fields["ciHi"] / WAD,
            "attack_cost_per_bp": fields["attackCostPerBp"] / USDC,
            "timestamp": fields["timestamp"],
            "posted_at": fields["postedAt"],
        }
        if "policyHash" in fields:  # v2 — same units as attackCostPerBp
            out["human_adjusted_bound"] = fields["humanAdjustedBound"] / USDC
            out["policy_hash"] = "0x" + fields["policyHash"].hex()
            out["window_start"] = fields["windowStart"]
            out["window_end"] = fields["windowEnd"]
        return out

    def is_stale(self, index_id: str, max_age: int) -> bool | None:  # pragma: no cover - live chain
        """True/False if the on-chain print is older than ``max_age`` seconds;
        None if offline."""
        if self._connect() is None or not self.oracle_address:
            return None
        try:
            return bool(self._contract().functions.isStale(index_id_to_bytes32(index_id), max_age).call())
        except Exception:
            return None
