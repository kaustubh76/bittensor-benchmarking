"""AgentKit human proofs — the authorization that makes a fleet view safe to serve.

WHAT THIS IS NOT. It is not a paywall. `/tca/{payer}` is already ungated, on the
principle that a benchmark nobody can check for free is a benchmark nobody
checks, and a proof here buys no data that a caller could not already fetch one
wallet at a time.

WHAT IT IS. `/tca/human` answers "what did MY agents pay, across all of them" —
and the only way to ask that without a proof would be to pass a cluster id, which
would let anyone enumerate a stranger's entire wallet fleet from a public
endpoint. The proof is what stops the aggregation being a privacy leak. That is a
real access-control property and it is the one to claim: present no proof and the
answer is 401; present a proof and the answer is your own union; there is no
request shape that returns somebody else's.

The nullifier never leaves this module. A verified proof yields a cluster id —
`keccak256(nullifier ‖ salt ‖ window)` — and everything downstream works from
that, so the wallet set exists for the duration of one query and is never held.

Mirrors `x402.py` deliberately: same challenge/verify seam, same module
singleton, same fail-closed discipline, so one reviewer's understanding covers
both gates.
"""

from __future__ import annotations

import base64
import json
import logging
import secrets
import time
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass

from acr_core import get_settings
from acr_oracle_client.humanid import cluster_id, current_window, salt_commitment
from fastapi import Header, HTTPException, Request

log = logging.getLogger("index_api.humanid")

#: The header a caller presents a proof in, and the one a challenge names.
PROOF_HEADER = "HUMAN-PROOF"
CHALLENGE_HEADER = "HUMAN-PROOF-CHALLENGE"

#: How long a challenge nonce stays spendable. Short, because the only thing it
#: has to survive is one round trip.
NONCE_TTL_S = 300.0
_MAX_NONCES = 4096


def mask(nullifier: str) -> str:
    """A nullifier, rendered so a log line cannot deanonymise anyone.

    Logs are read on stream, pasted into issues and shown on stage. The last
    three characters are enough to correlate two lines in one session and not
    enough to be anybody's identifier. Same instinct as `_short_addr` for
    wallets, applied to the value that actually matters.
    """
    tail = nullifier[-3:] if len(nullifier) >= 3 else "?"
    return f"••••{tail}"


@dataclass
class HumanProof:
    """One verified human, for the duration of one request.

    Deliberately carries NO wallet list. The route hands `cluster` to the tape
    and the tape answers; nothing here holds a fleet, so "only the nullifier is
    retained" is literally true rather than aspirational — and even the nullifier
    is dropped when the request ends.
    """

    nullifier: str
    cluster: str
    window: int
    verified_at: float
    backend: str
    sandbox: bool
    #: Did the backend actually SEE this, or is it our configured assertion?
    #: Published alongside the flag itself so a reader can tell the two apart.
    sandbox_observed: bool = False

    @property
    def masked(self) -> str:
        return mask(self.nullifier)

    def public(self) -> dict:
        """What a response may say about the human. Never the nullifier."""
        return {"cluster": self.cluster, "window": self.window,
                "backend": self.backend, "sandbox": self.sandbox,
                "sandbox_observed": self.sandbox_observed}


class HumanProofRequired(Exception):
    """The 401 challenge: a nonce to sign, and what to sign it for.

    401 rather than 403: the caller is not forbidden, they are unauthenticated,
    and `WWW-Authenticate` is how a client is told what to present.
    """

    status_code = 401

    def __init__(self, body: dict, headers: dict[str, str]) -> None:
        super().__init__("Human Proof Required")
        self.body = body
        self.headers = headers


def _b64(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj).encode()).decode()


class _Nonces:
    """Single-use challenge nonces, TTL-bounded and capacity-bounded.

    A nonce that could be spent twice would make every proof replayable, which
    would turn "this human authorized this call" into "this human authorized one
    call, once, and anyone who saw it can repeat it forever". Bounded by count as
    well as time so a long-lived process cannot be made to leak memory by asking
    for challenges it never answers.
    """

    def __init__(self, ttl_s: float = NONCE_TTL_S, max_keys: int = _MAX_NONCES) -> None:
        self._issued: OrderedDict[str, float] = OrderedDict()
        self._ttl = ttl_s
        self._max = max_keys

    def issue(self, *, now: float | None = None) -> str:
        t = time.monotonic() if now is None else now
        nonce = secrets.token_hex(16)
        self._issued[nonce] = t
        while len(self._issued) > self._max:
            self._issued.popitem(last=False)
        return nonce

    def spend(self, nonce: str, *, now: float | None = None) -> bool:
        """True exactly once per nonce, and only inside its TTL."""
        t = time.monotonic() if now is None else now
        issued = self._issued.pop(nonce, None)
        return issued is not None and (t - issued) < self._ttl


class HumanVerifier(ABC):
    """Base gate: the challenge/verify seam `require_human` calls.

    Subclasses turn a presented credential into a nullifier and nothing else.
    Deriving the cluster id is done here, once, so no backend can accidentally
    ship its own copy of the arithmetic the whole tape depends on.
    """

    backend = "base"

    def __init__(self) -> None:
        self.nonces = _Nonces()
        self.verified = 0

    # --- the seam ---

    @abstractmethod
    def challenge(self, request: Request) -> HumanProofRequired:
        """The 401 to raise when no proof is present."""

    @abstractmethod
    async def nullifier_of(self, request: Request, header: str) -> tuple[str, bool | None]:
        """Verify a credential. Returns (nullifier, observed_sandbox).

        `observed_sandbox` is None when the backend cannot see it — which is the
        usual case, and the reason it is a separate value rather than a bool.
        AgentBook stores a nullifier and nothing about how the human proved
        themselves; the legacy v2 verify response carries no environment either.
        Only World ID v4 returns one. When it is None the caller falls back to
        configuration, and what it publishes is then an assertion rather than an
        observation — a difference the tape is required to keep.
        """

    # --- shared ---

    def salt_ok(self) -> bool | None:
        """Does the configured salt hash to the deployed contract's commitment?

        None when there is nothing to compare against. False is the failure worth
        catching loudly: a wrong salt derives cluster ids that match nothing on
        the tape, so every human comes back with an empty union that reads
        exactly like "this human has never traded" — a silent zero, which is the
        one answer a benchmark must never give by accident.
        """
        s = get_settings()
        if not s.humanid_salt or not s.humanid_salt_commitment:
            return None
        try:
            return "0x" + salt_commitment(s.humanid_salt).hex() == s.humanid_salt_commitment.lower()
        except ValueError:
            return False

    async def prove(self, request: Request, header: str) -> HumanProof:
        nullifier, observed = await self.nullifier_of(request, header)
        s = get_settings()
        if not s.humanid_salt:
            # Fail closed. Without a salt there is no cluster id, and answering
            # with an un-scoped union would be the leak this gate exists to stop.
            raise HTTPException(status_code=503, detail="human proofs are not configured")
        if self.salt_ok() is False:
            raise HTTPException(
                status_code=503,
                detail="human proofs are misconfigured: the salt does not match the "
                "commitment the mirror was deployed with",
            )
        window = current_window()
        try:
            cluster = "0x" + cluster_id(nullifier, s.humanid_salt, window).hex()
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="malformed proof") from exc
        self.verified += 1
        return HumanProof(
            nullifier=nullifier, cluster=cluster, window=window,
            verified_at=time.time(), backend=self.backend,
            sandbox=s.humanid_sandbox if observed is None else observed,
            sandbox_observed=observed is not None,
        )

    def _challenge_body(self, request: Request, nonce: str) -> tuple[dict, dict[str, str]]:
        s = get_settings()
        body = {
            "error": "human proof required",
            "scheme": "agentkit",
            "app_id": s.humanid_app_id or None,
            "nonce": nonce,
            "expires_in_seconds": int(NONCE_TTL_S),
            # A proof is scoped to one resource so one minted for another route
            # or another service cannot be presented here.
            "resource": getattr(getattr(request, "url", None), "path", "") or "",
            "sandbox": s.humanid_sandbox,
            "header": PROOF_HEADER,
        }
        headers = {
            "WWW-Authenticate": f'HumanProof nonce="{nonce}"',
            CHALLENGE_HEADER: _b64(body),
        }
        return body, headers


class DevHumanVerifier(HumanVerifier):
    """Dev gate — accepts `humanid <nullifier-hex>:<nonce>`. No network, no World.

    Real in every respect that the security properties depend on: the nonce is
    still single-use and still expires, so replay is genuinely exercised rather
    than assumed. What it does not do is prove a human exists — which is exactly
    why /humanid/info reports the backend, and why the README says what a
    Sandbox identity is and is not.
    """

    backend = "dev"

    def challenge(self, request: Request) -> HumanProofRequired:
        body, headers = self._challenge_body(request, self.nonces.issue())
        body["scheme"] = "dev"
        return HumanProofRequired(body=body, headers=headers)

    async def nullifier_of(self, request: Request, header: str) -> str:
        try:
            scheme, rest = header.split(" ", 1)
            nullifier, nonce = rest.rsplit(":", 1)
        except ValueError as exc:
            raise HTTPException(status_code=401, detail="malformed human proof") from exc
        if scheme.lower() != "humanid":
            raise HTTPException(status_code=401, detail="unsupported proof scheme")
        if not self.nonces.spend(nonce.strip()):
            # Covers both a replay and an expiry. Saying which would tell an
            # attacker whether a nonce was ever real.
            raise HTTPException(status_code=401, detail="challenge nonce is not spendable")
        # None: a dev gate observes nothing about who a human is.
        return nullifier.strip(), None


class AgentKitVerifier(HumanVerifier):
    """World AgentKit — a signed CAIP-122 message plus an AgentBook lookup.

    NOT a proof POST. An earlier version of this class invented one; World's
    actual design is an x402 extension, which is why it lands so naturally here:

      1. the agent registers its wallet in AgentBook once, off the hot path,
      2. the server challenges it to sign a CAIP-122 message,
      3. the server recovers the address and asks AgentBook whose it is.

    Nothing is minted per request, and freshness is carried inside the signed
    message rather than enforced by a table on our side.

    ONE THING IS TAKEN ON FAITH AND IS FLAGGED HERE RATHER THAN BURIED. World's
    SDK reference documents the header name (`agentkit`), its base64-JSON
    encoding, and that "eip155:* payloads are reconstructed into a SIWE message"
    — but it does not enumerate the payload's field names. So the field names
    below follow CAIP-122/EIP-4361, the standard the docs name. If a real header
    ever fails to parse, this is the first place to look, and `_fields` is the
    only place that would need to change.
    """

    backend = "agentkit"
    #: World's own header name, from the SDK reference.
    HEADER = "agentkit"
    #: The docs' default freshness window for `issuedAt`.
    MAX_AGE_S = 300.0

    def __init__(self, book=None) -> None:
        super().__init__()
        self._book = book

    def book(self):
        if self._book is None:
            from acr_oracle_client.agentbook import build_agentbook

            self._book = build_agentbook(get_settings())
        return self._book

    def challenge(self, request: Request) -> HumanProofRequired:
        body, headers = self._challenge_body(request, self.nonces.issue())
        body["scheme"] = "agentkit"
        # `header` names what THIS server reads — HUMAN-PROOF. For a day it named
        # World's own header, `agentkit`, which `require_human` did not read, so a
        # client doing exactly what the challenge said was re-challenged forever.
        # Both are read now; the SDK's name is listed as the alternative.
        body["header"] = PROOF_HEADER
        body["also_accepted"] = self.HEADER
        return HumanProofRequired(body=body, headers=headers)

    @staticmethod
    def _fields(payload: dict) -> dict:
        """The CAIP-122 fields we care about, tolerant of casing.

        A payload may nest them under `message`, per the SDK's talk of parsing
        into "a structured payload". Accept either shape rather than rejecting a
        valid credential over a wrapper.
        """
        msg = payload.get("message") if isinstance(payload.get("message"), dict) else payload
        get = lambda *names: next(  # noqa: E731
            (msg[n] for n in names if isinstance(msg, dict) and msg.get(n) is not None), None
        )
        return {
            "address": get("address", "agent"),
            "nonce": get("nonce"),
            "issued_at": get("issuedAt", "issued_at"),
            "expiration": get("expirationTime", "expiration_time"),
            "uri": get("uri", "resource", "resourceUri"),
            "chain_id": get("chainId", "chain_id"),
        }

    def _recover(self, payload: dict) -> str:
        """The address that signed. EIP-191 only; EIP-1271 is not supported here.

        A smart-contract wallet's signature cannot be recovered — it has to be
        asked of the wallet itself on its own chain. Rejecting it outright beats
        accepting it unverified, and the message says which case it is so an
        agent is not left guessing.
        """
        from eth_account import Account
        from eth_account.messages import encode_defunct

        signature = payload.get("signature") or payload.get("sig")
        raw = payload.get("signedMessage") or payload.get("siwe") or payload.get("raw")
        if not signature or not raw:
            raise HTTPException(status_code=401, detail="malformed agentkit payload")
        if str(payload.get("type", "eip191")).lower() == "eip1271":
            raise HTTPException(
                status_code=401,
                detail="eip1271 signatures are not verified here — an EOA signature is required",
            )
        try:
            return Account.recover_message(encode_defunct(text=str(raw)), signature=signature)
        except Exception as exc:
            raise HTTPException(status_code=401, detail="signature does not recover") from exc

    def _check_binding(self, request: Request, f: dict) -> None:
        """Freshness, expiry, and that the message names THIS resource.

        The docs are explicit that context must be enforced server-side and
        never taken from the client, so every one of these is checked against
        our own view rather than against something the payload asserts.
        """
        import datetime as dt

        now = time.time()

        def _ts(value):
            try:
                return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
            except Exception:
                return None

        issued = _ts(f["issued_at"])
        if issued is None or now - issued > self.MAX_AGE_S:
            raise HTTPException(status_code=401, detail="proof is stale or undated")
        if f["expiration"] is not None:
            expires = _ts(f["expiration"])
            if expires is not None and expires < now:
                raise HTTPException(status_code=401, detail="proof has expired")
        path = getattr(getattr(request, "url", None), "path", "") or ""
        if f["uri"] and path and path not in str(f["uri"]):
            # A proof minted for another route must not authorize this one.
            raise HTTPException(status_code=401, detail="proof is bound to another resource")

    async def nullifier_of(self, request: Request, header: str) -> tuple[str, bool | None]:
        try:
            payload = json.loads(base64.b64decode(header))
        except Exception as exc:
            raise HTTPException(status_code=401, detail="malformed agentkit header") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=401, detail="malformed agentkit header")

        f = self._fields(payload)
        if not f["nonce"] or not self.nonces.spend(str(f["nonce"])):
            raise HTTPException(status_code=401, detail="challenge nonce is not spendable")
        self._check_binding(request, f)

        recovered = self._recover(payload)
        claimed = f["address"]
        if claimed and str(claimed).lower() != recovered.lower():
            raise HTTPException(status_code=401, detail="signature does not match the address")

        registration = self.book().lookup(recovered)
        if registration is None:
            # The honest answer, and a common one: AgentBook returns 0 for any
            # wallet nobody has registered. Not an error on our side.
            raise HTTPException(
                status_code=401, detail="this wallet is not registered in AgentBook"
            )
        # AgentBook records a nullifier and nothing about how the human proved
        # themselves, so provenance is not observable on this path.
        return registration.nullifier, None


class WorldIdCloudVerifier(HumanVerifier):
    """Direct World ID proofs, verified by World's Developer Portal.

    A different flow from AgentKit: here a human proves with IDKit and we check
    the proof, rather than an agent signing and us looking up its owner. Kept
    because it is the path a human can take without a registered agent wallet.

    Two API generations, and the difference matters:

      * v4 `POST {base}/api/v4/verify/{rp_id}` is primary. Its response carries
        `environment`, which is the ONLY place any of this can OBSERVE whether a
        proof came from a real human or the simulator.
      * v2 `POST {base}/api/v2/verify/{app_id}` is the fallback, entered only on
        v4's own `app_not_migrated` error. Note the field rename — v2 answers
        with `nullifier_hash`, v4 with `nullifier` — and note that a v2 response
        carries no environment at all, so on that path provenance is unobservable
        and we say so rather than guessing.

    The payload is forwarded AS-IS. World's docs are explicit: "Forward the IDKit
    result payload as-is. No field remapping is required." Remapping it here
    would be re-deriving a contract we do not own.
    """

    backend = "worldid"
    DEFAULT_BASE = "https://developer.world.org"

    def challenge(self, request: Request) -> HumanProofRequired:
        body, headers = self._challenge_body(request, self.nonces.issue())
        body["scheme"] = "worldid"
        return HumanProofRequired(body=body, headers=headers)

    def _base(self) -> str:
        return (get_settings().humanid_verifier_url or self.DEFAULT_BASE).rstrip("/")

    async def _post(self, url: str, payload: dict) -> tuple[int, dict]:
        import httpx

        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.post(url, json=payload)
        try:
            return r.status_code, r.json()
        except Exception:
            return r.status_code, {}

    async def nullifier_of(self, request: Request, header: str) -> tuple[str, bool | None]:
        s = get_settings()
        if not s.humanid_app_id:
            raise HTTPException(status_code=503, detail="human proofs are not configured")
        try:
            payload = json.loads(base64.b64decode(header))
        except Exception as exc:
            raise HTTPException(status_code=401, detail="malformed proof") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=401, detail="malformed proof")

        base, app = self._base(), s.humanid_app_id
        status, body = await self._post(f"{base}/api/v4/verify/{app}", payload)
        if status == 200 and body.get("success"):
            nullifier = str(body.get("nullifier") or "")
            env = str(body.get("environment") or "")
            # The one place provenance is genuinely observed rather than asserted.
            observed = (env != "production") if env else None
            if not nullifier:
                raise HTTPException(status_code=401, detail="verifier returned no nullifier")
            return _as_hex32(nullifier), observed

        if str(body.get("code") or "") == "app_not_migrated":
            status, body = await self._post(f"{base}/api/v2/verify/{app}", payload)
            if status == 200 and body.get("success"):
                nullifier = str(body.get("nullifier_hash") or "")
                if not nullifier:
                    raise HTTPException(status_code=401, detail="verifier returned no nullifier")
                # v2 carries no environment — unobservable, not "production".
                return _as_hex32(nullifier), None

        detail = str(body.get("detail") or body.get("code") or "proof rejected by the verifier")
        raise HTTPException(status_code=401, detail=detail)


def _as_hex32(value: str) -> str:
    """A nullifier as 0x-prefixed bytes32, whether it arrived hex or decimal.

    World's own guidance is to store nullifiers as numerics rather than parsing
    strings loosely; `cluster_id` wants 32 bytes. Normalising in one place beats
    each caller guessing which form it received.
    """
    raw = value.strip()
    n = int(raw, 16) if raw.startswith(("0x", "0X")) else int(raw)
    return "0x" + n.to_bytes(32, "big").hex()


def stray_world_credentials() -> list[str]:
    """Env vars that look like World credentials but are under a name nothing reads.

    `ACRSettings` uses `env_prefix="ACR_"` with `extra="ignore"`, so a variable
    named `WORLD_APP_ID` or `world_id_debug_address` is discarded in silence —
    no error, no warning, and the service simply behaves as if unconfigured.
    That is the hardest kind of misconfiguration to find, because everything
    reports "not set" and the operator can see the value sitting in the file.

    Scans the process environment AND the `.env` file, because the failure this
    exists to catch happens in the file. pydantic-settings reads `.env` without
    exporting it, so a variable that is only ever written there never appears in
    `os.environ` — and a check that looked only at the environment would have
    missed the very case it was written for.

    Names only. The values are credentials and must not be logged or returned.
    """
    import os
    import re
    from pathlib import Path

    names = set(os.environ)
    env_file = Path(".env")
    if env_file.exists():
        try:
            for line in env_file.read_text().splitlines():
                m = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
                if m:
                    names.add(m.group(1))
        except Exception:  # pragma: no cover - unreadable file is not fatal
            pass

    hits = []
    for name in names:
        upper = name.upper()
        if upper.startswith("ACR_"):
            continue
        if any(w in upper for w in ("WORLD", "AGENTKIT", "AGENTBOOK", "IDKIT", "NULLIFIER")):
            hits.append(name)
    return sorted(hits)


# --- module singleton + selection (mirrors x402.get_facilitator) --------------
_verifier: HumanVerifier | None = None


def get_verifier() -> HumanVerifier:
    global _verifier
    if _verifier is None:
        s = get_settings()
        mode = s.humanid_mode.strip().lower()
        if mode == "dev":
            _verifier = DevHumanVerifier()
        elif mode == "agentkit":
            # Needs no URL and no app id: AgentBook's address and RPC are public
            # constants. What it needs is a wallet somebody has registered.
            _verifier = AgentKitVerifier()
        elif mode in ("worldid", "cloud"):
            if not s.humanid_app_id:
                log.warning("humanid_mode=%s but app id unset — fails closed", mode)
            _verifier = WorldIdCloudVerifier()
        else:  # auto — a real gate only once something is actually configured
            _verifier = AgentKitVerifier() if s.humanid_app_id.strip() else DevHumanVerifier()
    return _verifier


def set_verifier(verifier: HumanVerifier) -> None:
    """Install a verifier (tests inject one wired to a mock transport)."""
    global _verifier
    _verifier = verifier


def reset_verifier() -> None:
    global _verifier
    _verifier = None


async def require_human(
    request: Request,
    human_proof: str | None = Header(default=None, alias=PROOF_HEADER),
    agentkit_proof: str | None = Header(default=None, alias=AgentKitVerifier.HEADER),
) -> HumanProof:
    """FastAPI dependency: 401 unless a valid human proof is present.

    Reads our header and World's (`agentkit`), so a client built from the SDK
    reference and one built from our challenge both get through. Ours wins when
    both are sent; nothing is merged.
    """
    verifier = get_verifier()
    human_proof = human_proof if human_proof is not None else agentkit_proof
    if human_proof is None:
        raise verifier.challenge(request)
    proof = await verifier.prove(request, human_proof)
    request.state.human = proof
    log.info("human proof verified %s (%s, window %d)", proof.masked, proof.backend, proof.window)
    return proof
