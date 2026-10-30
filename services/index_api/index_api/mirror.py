"""Deciding what to mirror — the policy layer over ``MirrorClient``.

The client knows how to write a settlement on chain. This knows which ones may
be written, and it is deliberately strict, because everything it lets through
becomes a row in a public benchmark that cannot be edited afterwards.

Four gates, each closing a way the tape could end up saying something untrue:

* **Real settlements only.** Dev and sim receipts never touch the chain — the
  same ``_REAL_SCHEMES`` rule the durable ledger already applies. Invented
  revenue on a public counter is bad; invented settlements in a benchmark are
  worse.
* **Priced settlements only.** A receipt with no seller or no quantity has no
  unit price, and a settlement with no unit price contributes nothing to
  transaction-cost analysis but still lands in the volume a rating divides by.
* **Fresh settlements only.** ``ReceiptMirror`` bounds the ordinary path at one
  hour. Anything older is *reported*, not silently dropped and not quietly
  pushed through the owner-only late path — an operator decides that.
* **Bounded work per tick.** The keeper's first rule is that it can never take
  the press down.
"""

from __future__ import annotations

import logging
import time

from acr_oracle_client import MirrorClient

from .fleet import listing_for_resource
from .x402 import _REAL_SCHEMES, PaymentReceipt

log = logging.getLogger("index_api.mirror")

#: Matches ``ReceiptMirror.MAX_MIRROR_LAG``. Kept as its own constant rather
#: than read from chain: a keeper that discovered the bound by reverting has
#: already spent the gas.
MAX_MIRROR_LAG_S = 3600

#: Settlements mirrored per tick. Two transactions each, so this bounds the
#: keeper's chain time — a backlog drains over several ticks rather than
#: stalling one.
BATCH = 8


def is_synthetic(payer: str) -> bool:
    """Whether this buyer is one we control.

    The flag is a disclosure, so it has to be derived rather than asserted.
    Hardcoding it True made `SellerDay.realVolume` — the figure the schema
    documents as "the number a mainnet rating is allowed to use" — structurally
    zero for every seller, and told an analyst querying it that no real flow
    exists when the truth was that nothing had ever been marked real.

    Grader-controlled means: any wallet whose key this deployment holds. Anyone
    else paying the fleet is real flow and is recorded as such.
    """
    import os

    from acr_core import get_settings

    s = get_settings()
    ours = set()
    # ACR_BUYER_PRIVATE_KEY is NOT an ACRSettings field — it is read from the
    # raw environment by the Terminal's buy route. Reading it only through
    # settings would silently miss the demo buyer and mark its flow as real,
    # which is the same one-field-via-settings-one-via-environ split that once
    # shipped a live venue beside `configured: false`.
    for key in (s.poster_private_key, os.environ.get("ACR_BUYER_PRIVATE_KEY", "")):
        if not key:
            continue
        try:
            from eth_account import Account

            ours.add(Account.from_key(key if key.startswith("0x") else "0x" + key).address.lower())
        except Exception:  # noqa: BLE001 - a malformed key must not stop mirroring
            continue
    for addr in (s.x402_pay_to, s.hedger_payer, s.hedger_address):
        if addr:
            ours.add(addr.lower())
    try:
        from acr_oracle_client.demo_sellers import DEMO_SELLERS

        ours.update(d.address.lower() for d in DEMO_SELLERS)
    except Exception:  # noqa: BLE001
        pass
    return (payer or "").lower() in ours


def mirrorable(r: PaymentReceipt, now: float | None = None) -> tuple[bool, str]:
    """Whether this receipt may be mirrored, and why not when it may not."""
    now = time.time() if now is None else now
    if r.scheme not in _REAL_SCHEMES:
        return False, "not a real settlement"
    if not r.seller:
        return False, "no seller — a platform-flat receipt has no unit price"
    if r.quantity <= 0:
        return False, "no quantity — nothing to divide the amount by"
    if r.amount_usdc <= 0:
        return False, "no amount"
    if not r.settled_at:
        return False, "no settlement time — nothing to anchor arrival to"
    if r.settled_at > now:
        return False, "settled in the future"
    if now - r.settled_at > MAX_MIRROR_LAG_S:
        return False, "older than the mirror window — needs the operator's late path"
    return True, ""


def mirror_once(
    receipts: list[PaymentReceipt],
    client: MirrorClient | None = None,
    now: float | None = None,
    dry_run: bool = False,
) -> str | None:
    """Mirror what is eligible. Returns a verdict string, or None when idle.

    Never raises: this runs as a keeper chore, and a chore that throws costs the
    press a beat. A per-settlement failure is counted and reported, and the next
    tick tries again — the contract's own replay guards make that safe.
    """
    client = client or MirrorClient()
    if not client.configured():
        return None

    eligible = [r for r in receipts if mirrorable(r, now)[0]]
    if not eligible:
        return None

    opened = finalized = skipped = failed = 0
    stale = sum(
        1
        for r in receipts
        if r.scheme in _REAL_SCHEMES
        and r.seller
        and mirrorable(r, now)[1].startswith("older than")
    )

    # BATCH bounds the WORK, not the candidate list. Slicing `eligible[:BATCH]`
    # instead meant a backlog larger than BATCH could never drain: the first
    # BATCH stay eligible for the whole mirror window, are re-examined every
    # tick as already-finalized no-ops, and the next one is never reached. The
    # keeper never noticed because live receipts arrive a few at a time and age
    # out; the operator's documented backlog path stalled at exactly BATCH.
    sent = 0
    for r in eligible:
        if sent >= BATCH:
            break
        listing = listing_for_resource(r.resource)
        if listing is None:
            skipped += 1
            continue
        try:
            state = client.state_for(r.tx_ref)
            worked = False
            if state is None:
                client.open_settlement(
                    tx_ref=r.tx_ref,
                    payer=r.payer,
                    seller=r.seller,
                    index_id=listing.index_id,
                    amount_usdc=r.amount_usdc,
                    settled_at=r.settled_at,
                    synthetic=is_synthetic(r.payer),
                    dry_run=dry_run,
                )
                opened += 1
                worked = True
                state = {"opened": True, "finalized": False}
            if not state.get("finalized"):
                client.finalize_settlement(
                    tx_ref=r.tx_ref,
                    service=listing.service,
                    quantity=r.quantity,
                    dry_run=dry_run,
                )
                finalized += 1
                worked = True
            # Counted per SETTLEMENT, not per transaction: BATCH is two txs each
            # by design, and a settlement already fully mirrored costs nothing
            # and must not consume a slot the backlog needs.
            if worked:
                sent += 1
        except Exception as exc:  # noqa: BLE001 — the reason matters, not the trace
            failed += 1
            log.warning("mirror %s failed: %s", r.tx_ref[:12], str(exc)[:160])

    # `skipped` counts too: a receipt that passed every gate and still could not
    # be placed against an index is a hole in the tape for a reason nobody has
    # named yet, which is precisely the case worth surfacing.
    if not (opened or finalized or failed or skipped or stale):
        return None
    parts = []
    if opened:
        parts.append(f"opened {opened}")
    if finalized:
        parts.append(f"finalized {finalized}")
    if failed:
        parts.append(f"FAILED {failed}")
    if skipped:
        parts.append(f"skipped {skipped} (no listing)")
    if stale:
        # Surfaced, never silently dropped: an unmirrored settlement is a hole in
        # a public tape, and a hole nobody is told about reads as an absence of
        # trading rather than an absence of mirroring.
        parts.append(f"{stale} past the mirror window — operator late-path needed")
    return " · ".join(parts)
