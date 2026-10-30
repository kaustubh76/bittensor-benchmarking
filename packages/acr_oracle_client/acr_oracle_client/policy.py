"""``PolicyClient`` — spend a business's money through its on-chain budget.

``contracts/src/PolicyWallet.sol`` is the authority; this is its client. Two
callers, two keys, and they are not the same party:

* the **agent** submits every transaction, and is the only address the contract
  will move money for;
* the **owner** signs nothing but approvals, and only for payments at or above
  the per-transaction limit. Their signature is what makes the escalation
  threshold real rather than a flag in a database somebody can flip.

So this client holds two signers and keeps them apart. An operator that could
sign its own approvals would have no threshold at all, which is the failure this
whole contract exists to prevent — ``spend_approved`` therefore refuses when the
two signers are the same address, rather than discovering it on chain.

Mirrors ``MirrorClient``'s shape (same offline tolerance, same pre-broadcast
signature check, same ``_send``) so one reader's understanding covers both.
"""

from __future__ import annotations

import json
import logging
from decimal import Decimal, InvalidOperation, localcontext

from acr_core import get_settings

from .mirror import to_bytes32
from .signer import Signer, build_role_signer

log = logging.getLogger("acr_oracle_client.policy")

USDC = 10**6

APPROVAL_TYPES = {
    "SpendApproval": [
        {"name": "category", "type": "bytes32"},
        {"name": "to", "type": "address"},
        {"name": "amount", "type": "uint256"},
        {"name": "decisionHash", "type": "bytes32"},
        {"name": "nonce", "type": "uint256"},
        {"name": "deadline", "type": "uint64"},
    ]
}

POLICY_ABI = [
    {
        "name": "consumedDecisionHashes", "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "decisionHash", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "bool"}],
    },
    {
        "name": "spend",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "category", "type": "bytes32"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "decisionHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "spendApproved",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "category", "type": "bytes32"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "decisionHash", "type": "bytes32"},
            {"name": "deadline", "type": "uint64"},
            {"name": "v", "type": "uint8"},
            {"name": "r", "type": "bytes32"},
            {"name": "s", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "spendAsOwner",
        "type": "function",
        "stateMutability": "nonpayable",
        "inputs": [
            {"name": "category", "type": "bytes32"},
            {"name": "to", "type": "address"},
            {"name": "amount", "type": "uint256"},
            {"name": "decisionHash", "type": "bytes32"},
        ],
        "outputs": [],
    },
    {
        "name": "budgetOf",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "category", "type": "bytes32"}],
        "outputs": [
            {"name": "exists", "type": "bool"},
            {"name": "cap", "type": "uint256"},
            {"name": "spent", "type": "uint256"},
            {"name": "perTxLimit", "type": "uint256"},
            {"name": "periodStart", "type": "uint64"},
            {"name": "periodLength", "type": "uint64"},
        ],
    },
    {
        "name": "remaining",
        "type": "function",
        "stateMutability": "view",
        "inputs": [{"name": "category", "type": "bytes32"}],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "approvalNonce",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint256"}],
    },
    {
        "name": "owner",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    # `IERC20 public immutable usdc` (PolicyWallet.sol:79) generates this getter,
    # and `balance_usdc()` has always been written to read the token address from
    # it rather than from configuration — "the wallet names the token it can
    # actually move, so this figure cannot drift from what `spend` transfers".
    # It was simply never declared here, so every live call raised
    # `The function 'usdc' was not found in this contract's abi` and the cash
    # rung reported "not measured" against a wallet holding 20 USDC. Measured on
    # Arc testnet 2026-10-12: the getter answers 0x3600…0000.
    {
        "name": "usdc",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
    },
    {
        "name": "paused",
        "type": "function",
        "stateMutability": "view",
        "inputs": [],
        "outputs": [{"name": "", "type": "bool"}],
    },
]


def usdc_units(amount: str | int | float | Decimal) -> int:
    """USDC to its 6-decimal integer, or raise.

    Refuses rather than rounds. A payment of ``0.0000001`` is not 0 and it is not
    1 unit either — it is an amount this rail cannot express, and quietly picking
    one of those two is how an agent pays the wrong number. ``apps/terminal`` does
    the same thing with ``parseUnits(..., 6)``.

    ``float`` is accepted but converted through ``str`` first, because
    ``Decimal(0.1)`` is 0.1000000000000000055511151231257827021181583404541015625
    and would fail the exactness check for a number the caller typed as 0.1.
    """
    try:
        d = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"not an amount: {amount!r}") from exc
    if not d.is_finite():
        raise ValueError("USDC amount must be finite")
    if d < 0:
        raise ValueError(f"negative amount: {amount!r}")
    with localcontext() as ctx:
        ctx.prec = max(78, len(d.as_tuple().digits) + 6)
        scaled = d * USDC
    if scaled != scaled.to_integral_value():
        raise ValueError(
            f"{amount} is finer than USDC's 6 decimals; round it before paying, "
            "so the rounding is a decision someone made"
        )
    return int(scaled)


def decision_hash(record: dict) -> bytes:
    """The on-chain commitment to one decision record.

    ``PolicyWallet`` stores this and rejects zero, so the chain holds a promise
    about the reasoning *before* the money moves. For that to mean anything the
    hash must be reproducible from the stored record forever, so the encoding is
    pinned here and nowhere else:

        keccak256(b"acr.decision::" + canonical_json)

    Canonical means sorted keys and no insignificant whitespace. Anything else —
    dict ordering, a prettier indent, a changed float repr — silently produces a
    different hash, and a record that no longer matches its own hash is
    indistinguishable from one that was edited after the fact.
    """
    canonical = json.dumps(
        record, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )
    return to_bytes32(f"acr.decision::{canonical}")


def category_id(name: str) -> bytes:
    """A budget category's bytes32 id, from its human name.

    Hashed, not padded: a category is written by a person ("infra", "contractors",
    "model inference") and a name longer than 31 bytes would truncate into a
    different category that still looks right in a log.
    """
    return to_bytes32(f"acr.category::{name}")


class PolicyClient:
    """Spends through one business's ``PolicyWallet``. Offline-tolerant."""

    def __init__(
        self,
        rpc_url: str | None = None,
        wallet_address: str | None = None,
        agent_signer: Signer | None = None,
        owner_signer: Signer | None = None,
        settings=None,
    ) -> None:
        s = settings or get_settings()
        self.rpc_url = rpc_url or s.arc_rpc_url
        self.wallet_address = wallet_address or (s.policy_wallet_address or None)
        # The operator acts as the taker: it is the role that spends on the
        # product's own account, and it already has its own Circle wallet.
        self.agent_signer = agent_signer or build_role_signer("taker", s)
        self.owner_signer = owner_signer or build_role_signer("owner", s)
        self._w3 = None
        #: Cached once answered. A contract does not appear at an address later
        #: in the same process, and re-reading code before every payment would
        #: put an RPC round trip in front of the money.
        self._wallet_status: str | None = None

    # --- plumbing -----------------------------------------------------------

    def configured(self) -> bool:
        return bool(self.wallet_address and self.agent_signer)

    def can_escalate(self) -> bool:
        """Is there an owner key able to clear a payment above the threshold?

        Separate from :meth:`configured` on purpose. An operator with no owner
        signer still runs — it just cannot clear the large payments, and the
        honest thing is to escalate them to a human rather than to report the
        wallet as unconfigured.
        """
        return bool(self.owner_signer)

    def signer_kinds(self) -> dict:
        """WHICH CHANNEL each role would pay through: ``circle`` · ``local`` ·
        ``none``.

        The same argument `screen_backend` won: a verdict without its source is
        a claim without a basis, and a payment without its channel is too. The
        operator's agent role resolves to a Circle developer-controlled wallet
        when `ACR_CIRCLE_TAKER_WALLET_ID` and an API key are both set, and
        silently to a raw local key otherwise — identical calldata either way, so
        nothing downstream could tell. Five payments on Arc testnet went out
        from a raw EOA for exactly that reason, while the deck's 20% is "Circle
        tool usage".

        Class names only, deliberately: `CircleWalletSigner.address` is lazy and
        a reporting surface must not make a network call to answer.
        """
        def kind(signer) -> str:
            if signer is None:
                return "none"
            return "circle" if type(signer).__name__ == "CircleWalletSigner" else "local"

        return {"agent": kind(self.agent_signer), "owner": kind(self.owner_signer)}

    def wallet_status(self) -> str:
        """Is there actually a contract at this wallet, on the chain we are on?

        ``ok`` · ``no_wallet`` · ``no_rpc`` · ``not_on_this_chain``.

        THE PHANTOM PAYMENT THIS EXISTS TO REFUSE. A ``PolicyWallet`` address is
        read with whatever RPC the press happens to hold, and the registry row
        carrying it travels with the image to any deployment. Point a mainnet
        press at a testnet wallet and every read returns empty: ``eth_call`` to
        an address with no code does not revert, it succeeds and returns
        nothing, so ``budget()`` decodes nothing and reports ``None`` — which the
        statement renders as "no budget set on chain", identical to a correctly
        wired wallet whose category is simply unset.

        The write path is worse. A CALL to nothing does not revert either, so
        ``eth_estimateGas`` SUCCEEDS (measured against a live Arc node: 22026
        gas), the transaction broadcasts, the receipt comes back ``status: 1``,
        and the only check ``_send`` makes passes. The operator then records a
        payment with a TRANSACTION HASH AS ITS EVIDENCE. *Agents and Ledgers*
        names exactly this as the thing SolidInvoice cannot disprove.

        TESTED BY CODE PRESENCE, NOT BY A CHAIN LABEL. A registry row saying
        ``chain: "testnet"`` is a label, and a label is what goes wrong first —
        this repo has already shipped a bundle carrying the right chain id while
        serving the wrong chain's data, and what caught it was a block height,
        not the id. An address either has code on the chain you are connected to
        or it does not, and no label can fake that.

        ``no_rpc`` is kept apart from ``not_on_this_chain`` deliberately: a node
        that is down is a thing to wait out, a wallet on another chain is a thing
        to fix, and reporting them as one hides which.
        """
        if self._wallet_status is not None:
            return self._wallet_status
        if not self.wallet_address:
            self._wallet_status = "no_wallet"
            return self._wallet_status
        w3 = self._connect()
        if w3 is None:
            # NOT cached: the node may come back, and a client built during a
            # blip would otherwise refuse every payment for the rest of its life.
            return "no_rpc"
        try:
            from web3 import Web3

            code = w3.eth.get_code(Web3.to_checksum_address(self.wallet_address))
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("PolicyClient: cannot read code at %s (%s)", self.wallet_address, exc)
            return "no_rpc"
        self._wallet_status = "ok" if len(code) > 0 else "not_on_this_chain"
        return self._wallet_status

    def _require_wallet(self) -> None:
        """Refuse to send when the contract is not there. This guards money."""
        status = self.wallet_status()
        if status == "ok":
            return
        if status == "no_wallet":
            raise RuntimeError("PolicyClient not configured (no wallet address)")
        if status == "no_rpc":
            raise RuntimeError(
                "cannot reach the chain to confirm the wallet, so nothing is sent"
            )
        raise RuntimeError(
            f"no contract at {self.wallet_address} on the chain this client is "
            "reading: the wallet is NOT ON THIS CHAIN. A call to an address with "
            "no code succeeds and moves nothing, so sending here would record a "
            "payment that never happened"
        )

    def _connect(self):
        if self._w3 is not None:
            return self._w3
        try:
            from web3 import Web3

            w3 = Web3(Web3.HTTPProvider(self.rpc_url, request_kwargs={"timeout": 20}))
            self._w3 = w3 if w3.is_connected() else None
        except Exception as exc:  # pragma: no cover - env dependent
            log.warning("PolicyClient: web3 unavailable (%s)", exc)
            self._w3 = None
        return self._w3

    def web3(self):
        """The connected client, or None. A PUBLIC door onto what `_connect`
        already built.

        `app.py`'s audit confirmer needs a chain to put a transaction back to,
        and it was reaching into `client._connect()` — a private method, from
        another package, in the one code path that decides whether a recorded
        payment is corroborated. A caller that has to break encapsulation to do
        its job is a caller whose job the class forgot to offer.
        """
        return self._connect()

    def _contract(self):
        from web3 import Web3

        return self._connect().eth.contract(
            address=Web3.to_checksum_address(self.wallet_address), abi=POLICY_ABI
        )

    def _domain(self, chain_id: int) -> dict:
        from web3 import Web3

        return {
            "name": "ACR Policy Wallet",
            "version": "1",
            "chainId": int(chain_id),
            "verifyingContract": Web3.to_checksum_address(self.wallet_address),
        }

    def _send(self, fn, wait: bool = True, signer: Signer | None = None) -> str:
        """Submit a built call. ``signer`` defaults to the agent.

        Parameterised because ``spendAsOwner`` must be sent BY the owner — the
        contract checks ``msg.sender``, so sending it from the agent's key would
        revert "not owner" no matter who decided to pay.
        """
        who = signer or self.agent_signer
        w3 = self._connect()
        tx = fn.build_transaction(
            {
                "from": who.address,
                "nonce": w3.eth.get_transaction_count(who.address),
                "chainId": w3.eth.chain_id,
            }
        )
        tx_hash = who.send_transaction(w3, tx)
        if wait:
            rcpt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=45)
            if rcpt.status != 1:
                raise RuntimeError(f"policy tx reverted ({tx_hash})")
        return str(tx_hash)

    # --- reads --------------------------------------------------------------

    def budget(self, category: str) -> dict | None:
        """One category's live budget, with the period already rolled by the
        contract's own view — so what this reports is what it will enforce.

        A PUBLIC READ: it needs the wallet address and a node, not a signer. Gating
        it on `configured()` made a keyless host (the mainnet probe that serves
        `/operator/*`) report "no budget set on chain" for a wallet that had one.
        """
        if not self.wallet_address or self._connect() is None:
            return None
        try:
            row = self._contract().functions.budgetOf(category_id(category)).call()
        except Exception:  # pragma: no cover - live chain
            return None
        exists, cap, spent, per_tx, period_start, period_length = row
        if not exists:
            return None
        return {
            "category": category,
            "cap_usdc": cap / USDC,
            "spent_usdc": spent / USDC,
            "remaining_usdc": max(cap - spent, 0) / USDC,
            "per_tx_limit_usdc": per_tx / USDC,
            "period_start": int(period_start),
            "period_length": int(period_length),
        }

    def balance_usdc(self) -> float | None:
        """What the wallet actually HOLDS, as opposed to what it is allowed.

        `budget()` above answers permission: `cap - spent`, from the contract's
        own counters. It would report a healthy remainder on a wallet holding
        nothing, and the payment would then revert on chain — so until this
        method existed the operator had no way to tell "you may spend 900" from
        "you have 900".

        SIX DECIMALS, and the reason matters. `PolicyWallet` holds USDC as an
        ERC-20 (`IERC20 public immutable usdc`) and moves it with `transfer`;
        every amount in that contract is the 1e6 view. On Arc the SAME asset is
        also the native gas token at 18 decimals, so `eth_getBalance` on this
        address answers a different question about a different pile — and
        `PolicyWallet.sol`'s own comment is blunt about the stakes: "Mixing them
        is a 1e12 error that looks like a fat finger." Reading the ERC-20 view
        also keeps cash in the same unit as `budget()`'s figures, which is what
        makes the two comparable at all.

        The token address comes from the WALLET, via its `usdc()` getter, not
        from configuration. The wallet names the token it can actually move, so
        this figure cannot drift from what `spend` transfers, and no USDC
        address is hardcoded here.

        `None` when it cannot be read, never 0.0. An unfunded wallet and an
        unreachable node are different things to do on a Monday morning, and a
        confident zero from a failed read is the kind of number the traction
        page already refuses.
        """
        w3 = self._connect()
        if not self.wallet_address or w3 is None:  # a public read: no signer needed
            return None
        try:
            token = self._contract().functions.usdc().call()
            erc20 = w3.eth.contract(
                address=token,
                abi=[
                    {
                        "name": "balanceOf",
                        "type": "function",
                        "stateMutability": "view",
                        "inputs": [{"name": "account", "type": "address"}],
                        "outputs": [{"name": "", "type": "uint256"}],
                    }
                ],
            )
            raw = erc20.functions.balanceOf(
                w3.to_checksum_address(self.wallet_address)
            ).call()
        except Exception as exc:
            # LOGGED, unlike `budget()` above, and deliberately. A `None` budget
            # renders as "no budget set on chain", which is a state an operator
            # expects; a `None` balance renders as "not measured", which is a
            # state they will want to fix and cannot diagnose from the page. The
            # reason can be a node that is down, a wallet at an address with no
            # contract, or a `usdc()` that is not there — three different
            # mornings, and the surface can only say one of them.
            log.warning("PolicyClient: balance unreadable (%s)", exc)
            return None
        return int(raw) / USDC

    def approval_nonce(self) -> int | None:
        if not self.configured() or self._connect() is None:
            return None
        try:
            return int(self._contract().functions.approvalNonce().call())
        except Exception:  # pragma: no cover - live chain
            return None

    def chain_now(self) -> int | None:
        """The chain's own clock, as the latest block reports it.

        ``PolicyWallet`` compares a deadline against ``block.timestamp``, so a
        deadline computed from the host's clock is denominated in the wrong
        clock. Measured on a local anvil the two were 3,815 seconds apart, which
        expired an approval signed one second earlier; on Arc the drift is
        smaller but it is not zero, and ``ACROracle`` already carries a
        timestamp-skew bound for the same reason.
        """
        if not self.configured() or self._connect() is None:
            return None
        try:
            return int(self._connect().eth.get_block("latest")["timestamp"])
        except Exception:  # pragma: no cover - live chain
            return None

    def deadline_in(self, seconds: int = 600) -> int:
        """A deadline ``seconds`` from now, on the clock the contract reads.

        Refuses rather than falling back to ``time.time()``: an approval signed
        against a clock the contract does not use is either rejected outright or,
        worse, valid for far longer than intended.
        """
        now = self.chain_now()
        if now is None:
            raise RuntimeError(
                "cannot read the chain clock; refusing to date an approval from the host's"
            )
        return now + int(seconds)

    def paused(self) -> bool | None:
        if not self.configured() or self._connect() is None:
            return None
        try:
            return bool(self._contract().functions.paused().call())
        except Exception:  # pragma: no cover - live chain
            return None

    # --- the two spending paths ---------------------------------------------

    def spend(self, category: str, to: str, amount_usdc, record: dict) -> str:
        """The agent's own authority: strictly below the per-transaction limit.

        ``record`` is the decision itself, not a hash — hashing here is what keeps
        the stored record and the on-chain commitment from drifting apart.
        """
        from web3 import Web3

        if not self.configured():
            raise RuntimeError("PolicyClient not configured (wallet address or agent signer)")
        self._require_wallet()
        fn = self._contract().functions.spend(
            category_id(category),
            Web3.to_checksum_address(to),
            usdc_units(amount_usdc),
            decision_hash(record),
        )
        return self._send(fn)

    def spend_as_owner(self, category: str, to: str, amount_usdc, record: dict) -> str:
        """The owner paying an escalated obligation from their OWN wallet.

        This is the escalation path that needs no signature scheme. ``ecrecover``
        cannot check a smart-contract account (``docs/WALLETS.md`` C1), so an
        owner whose wallet is a Circle PIN-secured SCA can never clear
        ``spendApproved`` — but it can simply call the contract, and the contract
        checks ``msg.sender``. Nothing is signed off-chain, so nothing can go
        stale between deciding and paying: there is no nonce and no deadline.

        Sent by ``owner_signer``, necessarily. Refuses when that key is the
        agent's, because an operator holding both would be approving its own
        payments and the threshold would mean nothing — the same refusal
        ``spend_approved`` makes, for the same reason.
        """
        from web3 import Web3

        if not self.configured():
            raise RuntimeError("PolicyClient not configured (wallet address or agent signer)")
        if not self.can_escalate():
            raise RuntimeError("no owner signer: this payment must go to a human")
        if self.owner_signer.address.lower() == self.agent_signer.address.lower():
            raise RuntimeError(
                "the agent and the owner are the same key, so the escalation "
                "threshold would authorize itself; give the owner its own key"
            )
        self._require_wallet()

        fn = self._contract().functions.spendAsOwner(
            category_id(category),
            Web3.to_checksum_address(to),
            usdc_units(amount_usdc),
            decision_hash(record),
        )
        return self._send(fn, signer=self.owner_signer)

    def sign_approval(
        self, category: str, to: str, amount_usdc, record: dict, deadline: int
    ) -> tuple[int, bytes, bytes]:
        """The owner's signature over one specific payment.

        Signed against the live nonce, so an approval is good exactly once and
        cannot be banked for later. Verified against ``eth_account`` before it is
        ever broadcast: an EIP-712 type mismatch recovers to a stranger, and the
        only symptom on chain would be "not owner signature" on a call that should
        have worked, after the gas was spent.
        """
        from eth_account import Account
        from eth_account.messages import encode_typed_data
        from web3 import Web3

        if not self.can_escalate():
            raise RuntimeError("no owner signer: this payment must go to a human")

        nonce = self.approval_nonce()
        if nonce is None:
            raise RuntimeError("cannot read the approval nonce; refusing to sign blind")

        message = {
            "category": category_id(category),
            "to": Web3.to_checksum_address(to),
            "amount": usdc_units(amount_usdc),
            "decisionHash": decision_hash(record),
            "nonce": int(nonce),
            "deadline": int(deadline),
        }
        domain = self._domain(self._connect().eth.chain_id)
        v, r, s_ = self.owner_signer.sign_typed_data(
            domain, APPROVAL_TYPES, message, "SpendApproval"
        )

        recovered = Account.recover_message(
            encode_typed_data(
                domain_data=domain, message_types=APPROVAL_TYPES, message_data=message
            ),
            vrs=(v, int.from_bytes(r, "big"), int.from_bytes(s_, "big")),
        )
        if recovered.lower() != self.owner_signer.address.lower():
            raise RuntimeError(
                f"approval recovers to {recovered}, not the owner "
                f"{self.owner_signer.address} — refusing to broadcast"
            )
        return v, r, s_

    def spend_approved(
        self,
        category: str,
        to: str,
        amount_usdc,
        record: dict,
        deadline: int,
        vrs: tuple[int, bytes, bytes] | None = None,
    ) -> str:
        """A payment at or above the threshold, carrying the owner's signature.

        ``vrs`` is accepted so the signature can come from a human's own wallet
        out of band — which is the point of the threshold. When it is omitted the
        owner signer here produces it, which is only honest while that key belongs
        to a different party than the agent's; hence the refusal below.
        """
        from web3 import Web3

        if not self.configured():
            raise RuntimeError("PolicyClient not configured (wallet address or agent signer)")

        if vrs is None:
            if not self.can_escalate():
                raise RuntimeError("no owner signer: this payment must go to a human")
            if self.owner_signer.address.lower() == self.agent_signer.address.lower():
                raise RuntimeError(
                    "the agent and the owner are the same key, so the approval "
                    "threshold would authorize itself; give the owner its own key "
                    "or pass a signature from one"
                )
            vrs = self.sign_approval(category, to, amount_usdc, record, deadline)

        v, r, s_ = vrs
        fn = self._contract().functions.spendApproved(
            category_id(category),
            Web3.to_checksum_address(to),
            usdc_units(amount_usdc),
            decision_hash(record),
            int(deadline),
            int(v),
            r,
            s_,
        )
        return self._send(fn)
