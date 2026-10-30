"""The Desk feed pass — a USDC payment the chain can see, turned into a right it can check.

A Desk user holds a Circle smart account (SCA). An SCA cannot sign the EIP-3009
authorization the x402 paywall needs — `ecrecover` does not speak ERC-1271 — so
until this existed a human on the Desk could trade the venue but could not buy
the product. The pass closes that: the SCA pays `pass_price_usdc` to the seller
with a plain on-chain USDC transfer (gas sponsored, one PIN), this module
VERIFIES that transfer from the chain itself, the press signs a `FeedAccess`
attestation for `pass_window_s`, relays `redeem`, and from then on the gate
serves that wallet's Desk session without a payment header.

What is checked before anything is signed, all from the chain:
  * the transaction is mined and succeeded;
  * it carries a USDC `Transfer` from the session's wallet to `x402_pay_to`
    of at least the pass price (in the 6-decimal ERC-20 view);
  * it is recent (`pass_claim_max_age_s`) — a receipt from last month is not a
    purchase made today;
  * its hash has not been claimed before (the attestation nonce IS the hash's
    first 16 bytes, and the contract refuses a reused nonce).

The signer is the press custody wallet — the same key that signs prints. That
is deliberate: a pass is a statement by the seller, and the seller has exactly
one signing identity on chain.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from acr_core import get_settings

log = logging.getLogger("index_api.feedpass")

#: keccak256("Transfer(address,address,uint256)")
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"


class PassError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Quote:
    price_usdc: float
    window_s: int
    pay_to: str
    attestor: str | None


def quote() -> Quote:
    s = get_settings()
    return Quote(
        price_usdc=float(s.pass_price_usdc),
        window_s=int(s.pass_window_s),
        pay_to=(s.x402_pay_to or "").strip(),
        attestor=(s.attestor_address or "").strip() or None,
    )


def _topic_addr(topic) -> str:
    h = topic.hex() if hasattr(topic, "hex") else str(topic)
    h = h[2:] if h.startswith("0x") else h
    return "0x" + h[-40:]


def transfer_in_receipt(receipt: dict, *, usdc: str, sender: str, recipient: str) -> int:
    """USDC units moved from `sender` to `recipient` in this receipt's logs — the
    sum over matching Transfer events, 0 when none. Pure: a receipt dict in, an
    integer out, so the check is tested without a chain."""
    usdc, sender, recipient = usdc.lower(), sender.lower(), recipient.lower()
    total = 0
    for lg in receipt.get("logs", []) or []:
        addr = str(lg.get("address", "")).lower()
        topics = lg.get("topics") or []
        if addr != usdc or len(topics) < 3:
            continue
        t0 = topics[0].hex() if hasattr(topics[0], "hex") else str(topics[0])
        if not t0.startswith("0x"):
            t0 = "0x" + t0
        if t0.lower() != TRANSFER_TOPIC:
            continue
        if _topic_addr(topics[1]).lower() != sender or _topic_addr(topics[2]).lower() != recipient:
            continue
        data = lg.get("data", b"")
        value = int.from_bytes(bytes(data), "big") if isinstance(data, (bytes, bytearray)) else int(str(data), 16)
        total += value
    return total


def verify_payment(w3, tx_hash: str, *, sender: str, now: float | None = None) -> tuple[int, int]:
    """Read the transaction from the chain and hold it to every rule above.
    Returns (units_paid, block_timestamp). Raises PassError with the reason."""
    s = get_settings()
    q = quote()
    if not q.pay_to.startswith("0x"):
        raise PassError(503, "no seller wallet configured to receive a pass payment")
    try:
        rcpt = w3.eth.get_transaction_receipt(tx_hash)
    except Exception as exc:  # noqa: BLE001 — the chain's answer, whatever shape
        raise PassError(404, f"transaction not found on this chain: {exc}"[:160]) from exc
    if not rcpt or int(rcpt.get("status", 0)) != 1:
        raise PassError(402, "the transaction did not succeed")
    units = transfer_in_receipt(dict(rcpt), usdc=s.usdc_address, sender=sender, recipient=q.pay_to)
    need = int(round(q.price_usdc * 1_000_000))
    if units < need:
        raise PassError(402, f"the transfer carries {units / 1e6:.6f} USDC to the seller; a pass costs {q.price_usdc:.6f}")
    block = w3.eth.get_block(rcpt["blockNumber"])
    ts = int(block["timestamp"])
    if (now if now is not None else time.time()) - ts > int(s.pass_claim_max_age_s):
        raise PassError(402, "that payment is too old to claim a pass with — a pass is bought, not remembered")
    return units, ts


def nonce_for(tx_hash: str) -> int:
    """The attestation nonce is the transaction hash — one payment, one pass."""
    h = tx_hash[2:] if tx_hash.startswith("0x") else tx_hash
    return int(h[:32], 16)


def mint(w3, signer, *, wallet: str, tx_hash: str) -> dict:
    """Verify the payment, sign the attestation, relay it. Returns what the chain
    now says. Everything that can fail does so BEFORE the signature exists."""
    from acr_oracle_client.feed_access import access_of, relay_redeem, sign_feed_access

    q = quote()
    if not q.attestor:
        raise PassError(503, "no FeedAccessAttestor configured: a pass has nowhere to live")
    units, _ = verify_payment(w3, tx_hash, sender=wallet)
    paid_until = int(time.time()) + q.window_s
    nonce = nonce_for(tx_hash)
    v, r, sg = sign_feed_access(
        signer, w3, q.attestor, payer=wallet, beneficiary=wallet,
        paid_until=paid_until, amount_units=units, nonce=nonce,
    )
    try:
        relay_tx = relay_redeem(
            signer, w3, q.attestor, payer=wallet, beneficiary=wallet,
            paid_until=paid_until, amount_units=units, nonce=nonce, v=v, r=r, s=sg,
        )
    except Exception as exc:  # noqa: BLE001
        if "nonce used" in str(exc):
            raise PassError(409, "this payment already bought a pass") from exc
        raise PassError(502, f"the attestation could not be relayed: {exc}"[:160]) from exc
    # WAIT for it. `relay_redeem` returns as soon as the transaction is accepted,
    # so reading access straight afterwards races the block that grants it: the
    # reader has paid, the redeem is fine, and `access_of` still answers "no
    # pass" because it looked too early. Reproduced on anvil — receipt status 1
    # with an AccessGranted log, and a mint that reported has_access False.
    # A reverted redeem is worse and was equally invisible: the money moved, the
    # attestation did not, and this returned 200 saying so.
    try:
        rcpt = w3.eth.wait_for_transaction_receipt(
            relay_tx if str(relay_tx).startswith("0x") else f"0x{relay_tx}", timeout=60
        )
    except Exception as exc:  # noqa: BLE001
        raise PassError(502, f"the attestation was sent but never mined: {exc}"[:160]) from exc
    if getattr(rcpt, "status", 1) != 1:
        raise PassError(502, f"the attestation reverted on chain (tx {relay_tx})")
    has, until = access_of(w3, q.attestor, wallet)
    log.info("feed pass minted for %s until %s (tx %s, relay %s)", wallet, until, tx_hash, relay_tx)
    return {"wallet": wallet, "has_access": bool(has), "paid_until": int(until), "payment_tx": tx_hash, "attest_tx": relay_tx}


# --- what the gate asks ---------------------------------------------------------

_access_memo: dict[str, tuple[float, bool, int]] = {}
ACCESS_TTL_S = 60.0


def access(w3, wallet: str) -> tuple[bool, int]:
    """(has_access, paid_until) for a wallet, from the chain, memoised for a minute
    so a pass holder's every read is not an RPC round trip."""
    q = quote()
    if not q.attestor:
        return False, 0
    key = wallet.lower()
    hit = _access_memo.get(key)
    now = time.monotonic()
    if hit and now - hit[0] < ACCESS_TTL_S:
        return hit[1], hit[2]
    from acr_oracle_client.feed_access import access_of

    try:
        has, until = access_of(w3, q.attestor, wallet)
    except Exception:  # noqa: BLE001 — a throttled RPC is "no pass", not a 500
        return False, 0
    _access_memo[key] = (now, bool(has), int(until))
    return bool(has), int(until)


def forget(wallet: str) -> None:
    _access_memo.pop(wallet.lower(), None)
