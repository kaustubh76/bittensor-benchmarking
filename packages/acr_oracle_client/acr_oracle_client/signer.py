"""Signer abstraction — EIP-712-sign typed data and submit transactions.

Two implementations, selected by config presence:

  * ``LocalKeySigner`` — a raw in-process EOA (``eth_account``). The default;
    byte-identical to the pre-Circle behavior, so the anvil round-trip is
    unchanged.
  * ``CircleWalletSigner`` — a Circle Developer-Controlled wallet. The signing
    key stays in Circle custody: it EIP-712-signs the ``Print`` (the oracle
    verifies the *signer*, not ``msg.sender``) and submits via Circle
    contract-execution (paying Arc USDC gas). The Circle SDK is imported lazily,
    only when no client is injected, so hermetic tests never require it.

The public seam is the ``Signer`` protocol; ``OracleClient``/``RegistryClient``
depend on it instead of a raw key. ``build_signer`` picks the impl from settings.
"""

from __future__ import annotations

import json
import logging
import uuid
from typing import Protocol, runtime_checkable

log = logging.getLogger("acr_oracle_client.signer")


@runtime_checkable
class Signer(Protocol):
    @property
    def address(self) -> str: ...

    def sign_typed_data(
        self, domain: dict, types: dict, message: dict, primary_type: str
    ) -> tuple[int, bytes, bytes]:
        """EIP-712-sign ``message`` and return ``(v, r, s)``."""
        ...

    def send_transaction(self, w3, tx: dict) -> str:
        """Submit a built transaction dict (``to``/``data``/…); return tx-hash hex."""
        ...


def split_signature(sig: bytes) -> tuple[int, bytes, bytes]:
    """Split a 65-byte ``r‖s‖v`` signature into ``(v, r, s)``; normalize v to 27/28."""
    if len(sig) != 65:
        raise ValueError(f"expected a 65-byte signature, got {len(sig)}")
    r, s, v = sig[0:32], sig[32:64], sig[64]
    if v < 27:
        v += 27
    return v, r, s


def full_eip712_json(domain: dict, types: dict, primary_type: str, message: dict) -> dict:
    """Assemble a *complete* EIP-712 typed-data document.

    Circle's typed-data signing wants the full JSON (the ``EIP712Domain`` type and
    ``primaryType`` included), unlike ``eth_account``'s partial-dict form. For JSON
    transport: ``bytes`` fields are hex-encoded, and integers are encoded as
    **strings** — a raw JSON number loses precision above 2^53, which would corrupt
    the hash for WAD-scaled (1e18) print values and make the signature invalid.
    """
    domain_type = [
        {"name": "name", "type": "string"},
        {"name": "version", "type": "string"},
        {"name": "chainId", "type": "uint256"},
        {"name": "verifyingContract", "type": "address"},
    ]

    def _enc(v):
        if isinstance(v, (bytes, bytearray)):
            return "0x" + bytes(v).hex()
        if isinstance(v, int) and not isinstance(v, bool):
            return str(v)  # uint as string — JSON numbers lose precision > 2^53
        return v

    return {
        "types": {"EIP712Domain": domain_type, **types},
        "domain": domain,
        "primaryType": primary_type,
        "message": {k: _enc(v) for k, v in message.items()},
    }


class LocalKeySigner:
    """Raw EOA signer — the default, byte-identical to the pre-Circle path."""

    def __init__(self, private_key: str) -> None:
        from eth_account import Account

        self._acct = Account.from_key(private_key)

    @property
    def address(self) -> str:
        return self._acct.address

    def sign_typed_data(self, domain, types, message, primary_type):
        from eth_account import Account

        signed = Account.sign_typed_data(
            self._acct.key, domain_data=domain, message_types=types, message_data=message
        )
        return int(signed.v), int(signed.r).to_bytes(32, "big"), int(signed.s).to_bytes(32, "big")

    def send_transaction(self, w3, tx: dict) -> str:
        """Submit ``{to, data}`` — completing it first, which is the contract.

        ``CircleWalletSigner`` takes exactly ``to`` + ``data`` and lets Circle
        fill nonce and gas server-side, so every caller is written that way:
        ``feed_access.relay_redeem`` hands over two keys and nothing else. This
        path passed that straight to ``eth_account``, which requires ``nonce``,
        ``gas`` and ``gasPrice`` and raises ``TypeError`` without them — so on a
        raw-key deployment the feed pass could never be relayed, and the only
        symptom was "the attestation could not be relayed". Every test around it
        replaced this method or the function calling it, and the one marker of
        how long that had gone unexercised was this method's own
        ``pragma: no cover``.

        Only absent fields are filled, so a caller that already built a full
        transaction (``OracleClient.post`` sets from/nonce/chainId and leaves gas
        to the node's estimate) keeps its own values.
        """
        out = dict(tx)
        out.setdefault("from", self._acct.address)
        out.setdefault("chainId", w3.eth.chain_id)
        if "nonce" not in out:
            out["nonce"] = w3.eth.get_transaction_count(self._acct.address)
        if "gas" not in out:
            # A fifth of headroom: `redeem` writes two slots and the estimate is
            # exact only for the state it was measured against.
            out["gas"] = int(w3.eth.estimate_gas(out) * 1.2)
        if "gasPrice" not in out and "maxFeePerGas" not in out:
            out["gasPrice"] = w3.eth.gas_price
        signed = self._acct.sign_transaction(out)
        raw = getattr(signed, "raw_transaction", None) or signed.rawTransaction
        return w3.to_hex(w3.eth.send_raw_transaction(raw))  # 0x-prefixed hash


class CircleAuthError(RuntimeError):
    """Circle refused the API key: one sentence instead of the SDK's traceback.

    On 2026-10-16 `make resolve-humans ARGS=--commit` ended in sixty lines of SDK
    output and `403 {"code":3,"message":"Forbidden"}`, with nothing naming the
    cause: a testnet `.env` holding a `LIVE_API_KEY`.
    """


#: Circle's key prefixes. Arc mainnet takes LIVE keys; every other chain TEST ones.
_CIRCLE_KEY_KINDS = ("TEST_API_KEY", "LIVE_API_KEY")


def circle_key_mismatch(api_key: str, chain_id: int) -> str | None:
    """Why this Circle key cannot serve this chain, or None.

    Judged from the key's own prefix, so it costs no call and catches the case
    Circle only answers with a bare 403. An unrecognised shape is not judged:
    Circle will say what it thinks, and guessing here could refuse a good key.
    The key itself is never echoed.
    """
    from acr_core.mainnet_guard import MAINNET_CHAIN_ID

    kind = (api_key or "").strip().split(":", 1)[0]
    if kind not in _CIRCLE_KEY_KINDS:
        return None
    mainnet = int(chain_id) == MAINNET_CHAIN_ID
    want = "LIVE_API_KEY" if mainnet else "TEST_API_KEY"
    if kind == want:
        return None
    return (
        f"ACR_CIRCLE_API_KEY is a {kind}, but chain {int(chain_id)} is Arc "
        f"{'mainnet' if mainnet else 'testnet'}, which needs a {want}: Circle answers 403 Forbidden "
        f"to every wallet call. Use this chain's key (the {'mainnet press' if mainnet else 'testnet press acr-api'} "
        "holds one in Render)."
    )


def _is_circle_auth_refusal(exc: BaseException) -> bool:
    """The SDK's ForbiddenException / UnauthorizedException, recognised by name or
    HTTP status so the SDK need not be importable to tell."""
    return type(exc).__name__ in ("ForbiddenException", "UnauthorizedException") or getattr(
        exc, "status", None
    ) in (401, 403)


@runtime_checkable
class CircleClient(Protocol):
    """The minimal surface ``CircleWalletSigner`` needs — stable and fully mockable.

    The real SDK is wrapped by ``_RealCircleClient`` (the one place that touches
    the uncertain Circle method names). Tests inject a fake implementing this.
    """

    def wallet_address(self, wallet_id: str) -> str: ...
    def sign_typed_data(self, wallet_id: str, typed_data_json: str) -> str: ...  # 65-byte hex sig
    def contract_execution(
        self, wallet_id: str, contract_address: str, call_data: str, idempotency_key: str
    ) -> str: ...  # on-chain tx hash


class CircleWalletSigner:
    """Signer backed by a Circle Developer-Controlled wallet (custody signing)."""

    def __init__(
        self,
        *,
        wallet_id: str,
        api_key: str = "",
        entity_secret: str = "",
        base_url: str | None = None,
        client: CircleClient | None = None,
    ) -> None:
        self.wallet_id = wallet_id
        self._api_key = api_key
        self._entity_secret = entity_secret
        self._base_url = base_url
        self._client = client
        self._address: str | None = None

    def _get_client(self) -> CircleClient:
        if self._client is None:  # pragma: no cover - requires the Circle SDK + creds
            self._client = _RealCircleClient(
                api_key=self._api_key, entity_secret=self._entity_secret, base_url=self._base_url
            )
        return self._client

    def _call(self, method: str, *args):
        """One Circle call, with a refused key turned into a CircleAuthError."""
        try:
            return getattr(self._get_client(), method)(*args)
        except Exception as exc:
            if not _is_circle_auth_refusal(exc):
                raise
            status = getattr(exc, "status", None) or ("403" if "Forbidden" in type(exc).__name__ else "401")
            raise CircleAuthError(
                f"Circle refused the API key (HTTP {status}) for wallet {self.wallet_id[:8]}…: the key is "
                "revoked, or it belongs to the other environment (a TEST key cannot reach a mainnet wallet, "
                "nor a LIVE key a testnet one)."
            ) from exc

    @property
    def address(self) -> str:
        if self._address is None:
            self._address = self._call("wallet_address", self.wallet_id)
        return self._address

    def sign_typed_data(self, domain, types, message, primary_type):
        doc = full_eip712_json(domain, types, primary_type, message)
        sig_hex = self._call("sign_typed_data", self.wallet_id, json.dumps(doc))
        sig = bytes.fromhex(sig_hex[2:] if sig_hex.startswith("0x") else sig_hex)
        return split_signature(sig)

    def send_transaction(self, w3, tx: dict) -> str:
        data = tx["data"]
        call_data = data if isinstance(data, str) else "0x" + bytes(data).hex()
        return self._call("contract_execution", self.wallet_id, tx["to"], call_data, str(uuid.uuid4()))


class _RealCircleClient:  # pragma: no cover - requires the Circle SDK + live creds
    """Adapter mapping ``CircleClient`` onto the circle-developer-controlled-wallets
    SDK (verified against v9.6.0). Kept isolated so an SDK bump is a one-file
    change. In 9.6.0 the operations live on typed Api classes (not the client),
    and every write carries a fresh per-call ``entity_secret_ciphertext``.
    """

    def __init__(self, api_key: str, entity_secret: str, base_url: str | None = None) -> None:
        # Lazy import so the SDK is only required on the live path.
        from circle.web3 import developer_controlled_wallets as dcw  # type: ignore
        from circle.web3 import utils  # type: ignore

        self._api_key = api_key
        self._entity_secret = entity_secret
        self._utils = utils
        self._dcw = dcw
        client = utils.init_developer_controlled_wallets_client(
            api_key=api_key, entity_secret=entity_secret
        )
        self._wallets = dcw.WalletsApi(client)
        self._signing = dcw.SigningApi(client)
        self._transactions = dcw.TransactionsApi(client)

    def _ciphertext(self) -> str:
        # Each developer-controlled write needs a fresh RSA ciphertext of the secret.
        return self._utils.generate_entity_secret_ciphertext(self._api_key, self._entity_secret)

    def wallet_address(self, wallet_id: str) -> str:
        from eth_utils import to_checksum_address

        # Circle returns lowercase; web3's tx `from` requires a checksum address.
        return to_checksum_address(self._wallets.get_wallet(id=wallet_id).data.wallet.address)

    def sign_typed_data(self, wallet_id: str, typed_data_json: str) -> str:
        resp = self._signing.sign_typed_data(
            self._dcw.SignTypedDataRequest(
                wallet_id=wallet_id,
                data=typed_data_json,
                entity_secret_ciphertext=self._ciphertext(),
            )
        )
        return resp.data.signature

    def contract_execution(
        self, wallet_id: str, contract_address: str, call_data: str, idempotency_key: str
    ) -> str:
        resp = self._transactions.create_developer_transaction_contract_execution(
            self._dcw.CreateContractExecutionTransactionForDeveloperRequest(
                wallet_id=wallet_id,
                contract_address=contract_address,
                call_data=call_data,
                fee_level="MEDIUM",
                entity_secret_ciphertext=self._ciphertext(),
                idempotency_key=idempotency_key,
            )
        )
        return self._poll_tx_hash(resp.data.id)

    def _poll_tx_hash(self, tx_id: str) -> str:
        # Poll GET /transactions/{id} until CONFIRMED and return the on-chain hash.
        import time

        for _ in range(60):
            tx = self._transactions.get_transaction(id=tx_id).data.transaction
            txhash = getattr(tx, "tx_hash", None) or getattr(tx, "txHash", None)
            if tx.state in ("CONFIRMED", "COMPLETE") and txhash:
                return txhash
            if tx.state == "FAILED":
                raise RuntimeError(f"Circle tx {tx_id} failed")
            time.sleep(2)
        raise TimeoutError(f"Circle tx {tx_id} not confirmed in time")


#: Which settings field holds each venue role's Circle wallet id. Roles exist so
#: one credential stops being four jobs: before this, a single raw EOA was the
#: venue maker AND the keeper AND the contract owner AND the x402 payTo address.
_ROLE_WALLET_FIELDS = {
    "maker": "circle_maker_wallet_id",
    "taker": "circle_taker_wallet_id",
    "poster": "circle_wallet_id",
    # Governance, not operations: the owner decides a series may exist and acts
    # rarely, on a human's initiative. Falls back to the poster wallet, then to
    # build_signer, so a venue whose owner has not been migrated still rolls.
    "owner": "circle_owner_wallet_id",
}


def build_role_signer(
    role: str, settings=None, private_key: str | None = None
) -> Signer | None:
    """The signer for one venue ROLE, preferring its own Circle wallet.

    Precedence is deliberately the inverse of :func:`build_signer` for the key:
    an explicitly passed ``private_key`` still wins (anvil tests and
    ``futures_demo`` hand one over and must keep working), but the *ambient*
    ``ACR_POSTER_PRIVATE_KEY`` does NOT. That ambient key is why every venue
    script signed as a raw EOA even on a host with full Circle credentials —
    ``build_signer`` prefers any key it can find, and one is always in ``.env``.
    A role with its own wallet configured should use it.

    Falls back to :func:`build_signer` when the role has no wallet id, so every
    offline, anvil and pre-migration path behaves exactly as before — with one
    exception, ``reader``, which is documented at its branch below and returns
    ``None`` rather than borrowing the poster's key.
    """
    from acr_core import get_settings

    settings = settings or get_settings()
    if private_key and not private_key.lstrip().startswith("#"):
        return LocalKeySigner(private_key)

    # READER IS NOT A VENUE ROLE AND MUST NOT INHERIT ONE'S KEY. It exists only to
    # sign agent cards, and it has no wallet because it spends nothing — but
    # "no wallet" used to mean "fall through to build_signer", which returns the
    # ambient ACR_POSTER_PRIVATE_KEY. That silently signed a read-only credential
    # with the key that posts the oracle: key reuse across a trust boundary, in
    # the direction that matters, since a leaked card reveals the signer.
    #
    # So it takes its own key or none. None is a working state — a caller with no
    # reader key presents no card and is served anonymously.
    if role == "reader":
        key = (getattr(settings, "reader_private_key", "") or "").strip()
        return LocalKeySigner(key) if key and not key.startswith("#") else None

    # THE OWNER IS THE ONE ROLE THAT MUST NOT SHARE A KEY WITH THE AGENT, for a
    # harder reason than the reader's: `spendAsOwner` REFUSES when the two
    # addresses match, in the contract and again in `PolicyClient`. Falling back
    # to the ambient poster key made owner and taker the same EOA, so an operator
    # with a funded PolicyWallet still could not clear its own escalation queue —
    # the config had no way to name a second party. Its own key wins over the
    # wallet id, so a deployment can keep its Circle owner wallet and still hand
    # a raw key to a one-off approval.
    if role == "owner":
        key = (getattr(settings, "owner_private_key", "") or "").strip()
        if key and not key.startswith("#"):
            return LocalKeySigner(key)

    field = _ROLE_WALLET_FIELDS.get(role)
    wallet_id = (getattr(settings, field, "") or "").strip() if field else ""
    api_key = (settings.circle_api_key or "").strip()
    if (
        wallet_id
        and api_key
        and not wallet_id.startswith("#")
        and not api_key.startswith("#")
    ):
        return CircleWalletSigner(
            wallet_id=wallet_id,
            api_key=api_key,
            entity_secret=settings.circle_entity_secret,
            base_url=settings.circle_base_url,
        )
    return build_signer(settings, private_key=private_key)


def build_signer(settings=None, private_key: str | None = None) -> Signer | None:
    """Pick a signer from config: an explicit/settings raw key (dev) wins; else a
    Circle wallet (prod) if creds are present; else ``None`` (offline)."""
    from acr_core import get_settings

    settings = settings or get_settings()
    key = private_key or (settings.poster_private_key or None)
    if key and not key.lstrip().startswith("#"):
        return LocalKeySigner(key)
    api_key = (settings.circle_api_key or "").strip()
    wallet_id = (settings.circle_wallet_id or "").strip()
    # Only pick the Circle wallet if the creds look real (guards a malformed
    # .env from silently selecting the live signer — fail safe to offline).
    if api_key and wallet_id and not api_key.startswith("#") and not wallet_id.startswith("#"):
        return CircleWalletSigner(
            wallet_id=wallet_id,
            api_key=api_key,
            entity_secret=settings.circle_entity_secret,
            base_url=settings.circle_base_url,
        )
    return None
