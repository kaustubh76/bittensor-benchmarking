"""The autonomous hedger's public state, derived entirely from public data.

``scripts/hedger.py`` runs wherever an operator runs it — a laptop, a dispatched
workflow — and writes its decision log there. None of that is reachable from
this service, and shipping a log file around would make the Terminal's picture
depend on a file nobody can verify.

So this derives the whole thing from what anyone can already check: the agent's
position and collateral on the venue, the ``Traded`` events whose taker is the
agent, and the x402 receipts whose payer is the agent. A reader can reproduce
every number here from the chain and the public ledger, which is the only kind
of claim this project makes.

Two addresses, one agent — the detail that trips people up (docs/WALLETS.md):

* the **SCA** (``ACR_HEDGER_ADDRESS``) is what ``ACRFutures.trade`` records as
  the taker, because it reads ``msg.sender``;
* the **backing EOA** (``ACR_HEDGER_PAYER``) is what the x402 settlement
  records, because EIP-3009 needs a signature ``ecrecover`` can verify and
  Circle signs with that EOA.

Both are the same agent. Unset either and the surface honestly reports itself
as not configured rather than inventing an empty agent.
"""

from __future__ import annotations

import os

from acr_core import get_settings

#: The mandate, in contracts — the position the agent is trying to hold. Mirrors
#: HEDGER_TARGET in scripts/hedger.py; the Terminal shows it so a reader can see
#: what the agent was aiming at, not just where it ended up.
TARGET_CONTRACTS = float(os.environ.get("HEDGER_TARGET", "2.5"))
HEDGER_INDEX = os.environ.get("HEDGER_INDEX", "ACR-INF")


#: How many of the agent's own settlements ride along. The same ceiling `fills`
#: uses, for the same reason: the panel shows five and says when it is hiding
#: some, while a payload carrying the whole ledger would grow without bound and
#: teach nothing past the first page.
RECENT_RECEIPTS = 10


def _addr(name: str, from_settings: str = "") -> str:
    """One of the agent's two addresses: process environment first, ``.env`` second.

    Both paths have to work. Render sets these as real environment variables
    (render.yaml), so the raw read must keep answering. A checkout runs
    ``make snapshot`` with nothing exported and the addresses sitting in
    ``.env`` — which only ``ACRSettings`` reads, because ``env_file`` is
    configured there. Reading os.environ alone is why the committed bundle
    archived a live venue with no agent standing on it.

    Environment FIRST, deliberately: it is the precedence pydantic-settings
    itself applies, so the two paths can never disagree about which wins, and
    ``get_settings()`` is a process-wide singleton — a caller that sets the
    variable after it was built would otherwise be handed a stale answer by the
    very object meant to be the fallback.
    """
    v = (os.environ.get(name, "") or "").strip()
    if v:
        # A raw env var can carry the same inline-comment pollution a `.env`
        # can. ACRSettings has `_ignore_comment_pollution` for the dotenv path;
        # this branch never goes through pydantic, so it keeps its own copy.
        return "" if v.startswith("#") else v
    return (from_settings or "").strip()


def _checksum(address: str) -> str:
    """EIP-55 form. Circle only ever returns lowercase and web3 refuses it."""
    try:
        from eth_utils import to_checksum_address

        return to_checksum_address(address)
    except Exception:
        return address


def build_hedger_state(futures, receipts: dict | None = None) -> dict:
    """The agent's live standing: mandate, position, fills, and what it paid.

    ``futures`` is a :class:`~index_api.onchain.FuturesReader` (cached, so this
    stays off the RPC hot path). ``receipts`` is the settlement ledger dict from
    :func:`~index_api.marketplace.build_receipts`; omitted → the spend figures
    report as unknown rather than zero, because an unread ledger is not an
    unpaid one.
    """
    s = get_settings()
    sca = _addr("ACR_HEDGER_ADDRESS", s.hedger_address)
    payer = _addr("ACR_HEDGER_PAYER", s.hedger_payer)

    # Every key the payload promises, declared once. The unconfigured branch
    # returns this same dict, so both states carry the SAME shape — `HedgerState`
    # in apps/terminal/lib/types.ts is typed against it and ops.py reads several
    # by name. `gap_contracts` used to be attached only at the bottom, past the
    # early return, so a switched-off deployment silently returned one key fewer.
    out: dict = {
        "configured": bool(sca),
        "agent": sca or None,
        "payer": payer or None,
        "index_id": HEDGER_INDEX,
        "target_contracts": TARGET_CONTRACTS,
        "venue": s.futures_address or None,
        "wallet_kind": "circle-agent-wallet",
        "series_id": None,
        "multiplier": None,
        "mark": None,
        "mark_block": None,
        "position_contracts": None,
        "gap_contracts": None,
        "collateral_usdc": None,
        "fills": [],
        # None = the ledger was not read. [] = it was read and this payer is not
        # in it. Opposite conclusions, so they never collapse into each other.
        "receipts": None,
        "paid_queries": None,
        "spent_usdc": None,
    }
    if not sca:
        return out

    low = sca.lower()
    # Fills first: they need no configuration beyond the venue and they are the
    # part a reader is most likely to check against the explorer.
    try:
        trades = futures.recent_trades()
    except Exception:  # pragma: no cover - live chain
        trades = []
    out["fills"] = [t for t in trades if str(t.get("taker", "")).lower() == low][:10]

    try:
        from acr_oracle_client.futures import collateral_or_none

        desk = futures.read_desk(HEDGER_INDEX)
        sid = desk.get("series_id") if desk else None
        out["series_id"] = sid
        if sid is not None:
            out["multiplier"] = int(desk.get("multiplier") or 0) or None
            # The venue's own last fill price on THIS series, taken off the tape
            # already read above rather than through a second call. ACRFutures
            # fills every trade at `oracle.latestValue(indexId)` and emits it, so
            # this IS an oracle print — the one the contract itself used — and
            # `mark_block` makes it checkable on the explorer. It comes from the
            # same tape the panel renders beside it, so the two can never
            # disagree by a refresh, which an independent read eventually would.
            # `read_desk` carries no mark at all, so the tape is the only source.
            for t in trades:  # recent_trades() is newest-first
                if t.get("series_id") == sid and float(t.get("mark") or 0.0) > 0:
                    out["mark"] = float(t["mark"])
                    out["mark_block"] = int(t.get("block") or 0) or None
                    break
            # The position/collateral reads live on the client, not the reader —
            # same seam desk.py uses. Addresses are checksummed at the boundary
            # because Circle hands back lowercase and web3 refuses it, a failure
            # that presents as a throttled venue rather than a format problem.
            client = futures._client
            who = _checksum(sca)
            pos = client.position_of(sid, who)
            if pos is not None:
                out["position_contracts"] = pos.get("contracts")
            # `or 0` here would turn a throttled read into an empty account, and
            # those call for opposite conclusions. None stays None.
            out["collateral_usdc"] = collateral_or_none(client, sid, who, tries=2)
    except Exception:  # pragma: no cover - live chain
        pass

    if receipts and payer:
        low_payer = payer.lower()
        # Payer AND chain. The committed archive holds real settlements from Arc
        # testnet, and filtering on payer alone put seven `eip155:5042002` rows
        # into a mainnet bundle — the panel then argued that the print this agent
        # bought led to the position it holds, using purchases made on another
        # network. Same rule as `x402._load_receipts`: a row with no `network`
        # recorded is kept, because pre-dating the field is not evidence.
        here = s.caip2()
        rows = [
            r for r in (receipts.get("receipts") or [])
            if str(r.get("payer", "")).lower() == low_payer
            and str(r.get("network") or "") in ("", here)
        ]
        # Sorted here rather than trusted. The live ring is already newest-first,
        # but the committed archive is append-ordered by CAPTURE, and capture
        # order is not settlement order: of its seven hedger rows the newest by
        # `settled_at` sits FIFTH from the top, so "take the top of the file"
        # would publish the wrong payment as the latest. A row written before
        # `settled_at` existed sorts last rather than to the epoch.
        rows.sort(key=lambda r: float(r.get("settled_at") or 0.0), reverse=True)
        out["paid_queries"] = len(rows)
        out["spent_usdc"] = round(sum(float(r.get("amount_usdc", 0.0)) for r in rows), 6)
        # Projected, never splatted. The two ledger paths carry DIFFERENT keys:
        # the live ring adds `seq` and the committed archive adds `resource` —
        # and a field present on one path and absent on the other is precisely
        # how a panel learns to render "…" for a value that is really there.
        # (`resource` is empty on 32 of the archive's 34 rows anyway, so it names
        # nothing.) Everything below is on BOTH paths, coerced at this boundary.
        out["receipts"] = [
            {
                "tx_ref": str(r.get("tx_ref") or ""),
                "amount_usdc": float(r.get("amount_usdc") or 0.0),
                "network": str(r.get("network") or ""),
                # None, not 0 — the epoch is a wall-clock and renders as 1970.
                "settled_at": float(r["settled_at"]) if r.get("settled_at") else None,
            }
            for r in rows[:RECENT_RECEIPTS]
        ]

    # The gap is the whole decision: mandate minus reality. Computed here so the
    # UI never has to re-derive it and drift from what the agent itself used.
    if out["position_contracts"] is not None:
        out["gap_contracts"] = round(TARGET_CONTRACTS - float(out["position_contracts"]), 4)
    return out
