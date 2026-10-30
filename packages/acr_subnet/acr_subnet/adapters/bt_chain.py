"""The one place that knows what the Bittensor SDK looks like.

Every other adapter goes through `Chain`, so when the SDK moves — and it has
moved hard, see below — exactly one file needs rewriting.

WHAT v11 ACTUALLY IS, measured against the installed package on 2026-10-30
-------------------------------------------------------------------------
`bittensor` 11.3.0 is **not** the SDK that `bittensor-subnet-template` and every
tutorial still describe. `bt.Synapse`, `bt.axon` and `bt.dendrite` do not exist
in it. The neuron-serving HTTP framework is gone; what is left is a chain client:
wallets, reads, and extrinsics as classes.

That matters because code written from memory of the old SDK does not fail
gracefully here, it fails at import. Verified present in 11.3.0:

    RegisterSubnet(hotkey_ss58=None)
    BurnedRegister(netuid, hotkey_ss58=None)
    ServeAxon(netuid, ip, port, protocol=4, version=1)
    SetWeights(netuid, uids=None, weights=None, mechid=0, version_key=0, raw_u16=False)
    CommitWeights / RevealWeights
    Subtensor.execute(intent, wallet, **kwargs)      <- the real write path
    Subtensor.submit_call(call, wallet, **kwargs)    <- raw-call escape hatch
    Subtensor.block                      -> int
    Wallet(name='default', hotkey='default', path='~/.bittensor/wallets')

So an extrinsic is *built* as a value and *submitted* through the Subtensor. The
consequence for us is that a miner serves its print over ordinary HTTP — this
repo already depends on FastAPI and httpx — and `ServeAxon` publishes where to
reach it. The chain carries the address; it was never carrying the payload.

The scoring engine is untouched by all of this, which was the design bet: if
`scoring.py` had needed to change to run against a real chain, the mechanism
would have been wrong rather than the plumbing.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

# Networks worth naming. `test` is the public testnet, which is a real chain with
# real consensus that happens to be worth no money -- the right place to be wrong
# in public. `finney` is mainnet and is spelled out rather than defaulted: no code
# path here should reach mainnet because a parameter was left off.
NETWORK_TEST = "test"
NETWORK_MAIN = "finney"


class BittensorNotInstalled(RuntimeError):
    """Raised when the optional SDK is absent, with the command that fixes it."""

    def __init__(self) -> None:
        super().__init__(
            "the bittensor SDK is not installed. It is an optional extra so that "
            "the base install stays deployable and the test suite runs without a "
            "chain:\n\n    uv sync --extra bittensor\n"
        )


def _bt() -> Any:
    """Import the SDK lazily, and say something useful when it is missing.

    Lazy because importing this module must stay free: `acr_subnet` itself never
    imports it, and a missing extra should be a clear sentence rather than an
    ImportError traceback out of somebody else's package.
    """
    try:
        import bittensor as bt  # noqa: PLC0415 -- deliberately deferred
    except ImportError as exc:  # pragma: no cover -- needs the extra absent
        raise BittensorNotInstalled from exc
    return bt


@dataclass(frozen=True)
class MinerEndpoint:
    """A miner the chain says is reachable, and where.

    The chain stores an address; v11 has no transport of its own, so whatever
    answers here is an ordinary HTTP server. `uid` travels with the address
    because weights are keyed by uid, and looking it up a second time later is
    how a validator ends up paying the wrong miner.

    TWO CHANNELS, AND WHY THERE HAD TO BE TWO. `ServeAxon` takes `ip: str` and
    `port: int` and nothing else -- there is no hostname field anywhere in the
    axon record. A miner on a managed host (Render, Fly, Vercel) therefore
    cannot be announced as an axon at all: those platforms put many apps behind
    one address and route by `Host`/SNI, so a request to the bare IP arrives at
    the router and never at us. Announcing such an address would publish
    exactly the unkeepable promise `bt_miner` already refuses for loopback.

    So a miner may instead publish its URL in its on-chain **commitment**, and
    `announced_url` is that URL. Discovery stays entirely on-chain either way;
    `source` records which channel answered, because "the chain told me" and
    "the chain told me *where*" are different claims and a log that conflates
    them cannot be audited.
    """

    uid: int
    hotkey: str
    ip: str
    port: int
    announced_url: str | None = None
    source: str = "axon"

    @property
    def url(self) -> str:
        return self.announced_url or f"http://{self.ip}:{self.port}"


@dataclass(frozen=True)
class ChainHead:
    """What the chain says about itself right now.

    `network` and `block` together are the citation. A number without the chain
    it came from is not evidence, and this repo quotes block heights in its docs.
    """

    network: str
    block: int

    def cite(self) -> str:
        return f"{self.network} block {self.block}"


# ---- the commitment payload protocol -------------------------------------
#
# The Commitments pallet stores a `CommitmentInfo { fields: BoundedVec<Data> }`,
# where `Data` is the same enum the identity pallet uses: `Raw0` .. `Raw128`
# plus hash variants. Two consequences, both read out of the installed SDK
# rather than assumed:
#
#   * one field carries at most 128 bytes, and the variant NAME encodes the
#     length -- `Raw61` for 61 bytes, not a `Raw` with a length prefix;
#   * on the way back out the SDK concatenates every `Raw*` field and decodes
#     utf-8 (`bittensor/reads/identity.py::_decode_commitment_data`), which is
#     why a plain JSON object round-trips without a codec of our own.
MAX_COMMITMENT_FIELD_BYTES = 128

# A hotkey gets ONE commitment slot per subnet, so each neuron publishes the
# single thing only it can say: a miner says where to reach it, a validator
# says what the subnet concluded. The version marker is first and cheap so a
# later format change is detectable rather than silently mis-parsed -- an
# unversioned payload is the thing you cannot fix later without a flag day.
COMMITMENT_VERSION = "acr/1"


def commitment_payload(payload: dict[str, Any]) -> bytes:
    """Serialize a commitment compactly, and refuse one that will not fit.

    Sorted keys and no whitespace because the budget is 128 bytes and every
    space is a byte that could have been a digit of the estimate. The length is
    checked here rather than discovered as a chain rejection: `set_commitment`
    costs a signature and a `FieldDeposit`, and finding out the payload was two
    bytes too long by spending both is the expensive way to learn it.
    """
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    if len(raw) > MAX_COMMITMENT_FIELD_BYTES:
        raise ValueError(
            f"commitment payload is {len(raw)} bytes, over the "
            f"{MAX_COMMITMENT_FIELD_BYTES}-byte `Data::Raw*` limit for a single "
            "field. Shorten it (the estimate needs few digits; a URL needs no "
            "query string) rather than spreading it over several fields. "
            "Measured on testnet at block 8335689: `Commitments.MaxFields` is "
            "3 and `Commitments.FieldDeposit` is 0, so ~384 bytes over three "
            "fields is available and costs no deposit -- chunking is a real "
            "option, it is just not one this subnet has needed."
        )
    return raw


def endpoint_commitment(url: str) -> dict[str, Any]:
    """The payload a miner publishes to say where it answers.

    Refuses anything a validator could not actually dial, for the same reason
    `bt_miner` refuses to announce loopback: an endpoint nobody outside this
    machine can reach is not an announcement, it is a claim that will be read
    as one and then time out on somebody else's epoch.
    """
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise ValueError(
            f"refusing to announce {url!r}: a commitment endpoint must be an "
            "http(s) URL, because that is what a validator dials."
        )
    host = (parts.hostname or "").lower()
    if not host:
        raise ValueError(f"refusing to announce {url!r}: no host in the URL.")
    if host in {"localhost", "127.0.0.1", "::1", "0.0.0.0"} or host.endswith(
        ".local"
    ):
        raise ValueError(
            f"refusing to announce {url!r}: {host} resolves to this machine "
            "only. Publishing it would tell every validator on the subnet to "
            "query itself, which reads as a dead miner rather than as a "
            "misconfigured one."
        )
    return {"v": COMMITMENT_VERSION, "url": url}


def endpoint_from_commitment(value: Any) -> str | None:
    """The URL in a commitment, or `None` if it does not carry one.

    `None` is the answer for every commitment this subnet did not write --
    other subnets' hotkeys publish their own formats into the same storage, and
    a discovery path that raises on someone else's JSON is a discovery path
    that one stranger can switch off. Sealed timelocked payloads arrive as
    `None`/empty from the SDK and land here too.
    """
    if not value or not isinstance(value, str):
        return None
    try:
        data = json.loads(value)
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    if not str(data.get("v", "")).startswith("acr/"):
        return None
    url = data.get("url")
    if not isinstance(url, str) or not url:
        return None
    try:
        endpoint_commitment(url)
    except ValueError:
        # Published, but not dialable. Treated as no announcement at all rather
        # than trusted because it came off the chain: the chain stores what it
        # was given, it does not vouch for it.
        return None
    return url


class Chain:
    """A real Bittensor chain, read and written through the v11 SDK.

    Construction connects. That is deliberate: a `Chain` that exists but has not
    reached a node is an object that lies about its own availability, and every
    caller would have to remember to check. Failing here fails once, loudly.
    """

    def __init__(self, network: str = NETWORK_TEST) -> None:
        bt = _bt()
        self._bt = bt
        self.network = network
        self._st = bt.subtensor(network=network)

    # ---- reads. Free: no wallet, no tokens, no signature. ----

    def head(self) -> ChainHead:
        """Current block height, as the chain reports it."""
        return ChainHead(network=self.network, block=int(self._st.block))

    def is_mainnet(self) -> bool:
        return self.network == NETWORK_MAIN

    def metagraph(self, netuid: int) -> Any:
        """The whole metagraph for one subnet, in one call.

        v11 reaches this through a dynamic namespace — `subtensor.subnets` —
        whose attributes do not appear in `dir()`, so it cannot be discovered by
        introspection. It is in the read registry under `metagraph`, category
        `Subnets`, params `{netuid}`. Written down because the next person to
        look for it will not find it by tab-completion either.
        """
        return self._st.subnets.metagraph(netuid=netuid)

    def endpoints(self, netuid: int) -> tuple[list[MinerEndpoint], int]:
        """Who can actually be queried on this subnet, and how many cannot.

        Returns `(reachable, unannounced)`. The second number is not a
        formality. Measured against the live testnet on 2026-10-30, **0 of 256**
        neurons on netuid 1 ("apex") announced an axon at all — every `axon` was
        `None`. That is the same shape as this project's mainnet finding on
        Chutes (subnet 64), where 248 of 256 miners reported `ip` as the string
        `"0"`: the public chain does not reliably carry where a miner lives.

        So a validator cannot treat silence as a bad answer. An unreachable
        miner has published nothing to be wrong about, and scoring it zero would
        punish it for the chain's reticence rather than for its estimate. The
        count is returned so the caller has to look at it.

        Two channels are consulted, axon first. The axon is Bittensor's own and
        is preferred when present; the commitment channel exists because
        `ServeAxon` stores an `ip` and a `port` and has no hostname field, so a
        miner behind a managed host's shared address has no way to express
        itself as an axon at all. A neuron is counted `unannounced` only when
        BOTH are silent -- which is the honest reading, and was not what this
        method did when it looked at axons alone.
        """
        reachable: list[MinerEndpoint] = []
        unannounced = 0
        announced = self.endpoint_commitments(netuid)
        for n in self.metagraph(netuid).neurons:
            axon = getattr(n, "axon", None)
            ip = str(getattr(axon, "ip", "") or "") if axon is not None else ""
            port = int(getattr(axon, "port", 0) or 0) if axon is not None else 0
            hotkey = str(n.hotkey)
            # "0" and 0.0.0.0 are both the chain's way of saying "not serving",
            # not addresses. Treating either as routable produces a validator
            # that spends its epoch timing out against nothing.
            if ip and ip not in {"0", "0.0.0.0"} and port > 0:
                reachable.append(
                    MinerEndpoint(uid=int(n.uid), hotkey=hotkey, ip=ip, port=port)
                )
                continue
            # No axon -- but an axon is not the only thing the chain carries.
            # Ask the commitment channel before calling this neuron silent: a
            # miner on a managed host CANNOT announce an axon (`ServeAxon` has
            # no hostname field; see `MinerEndpoint`), so an absent axon there
            # means "announced elsewhere", not "not serving".
            url = announced.get(hotkey)
            if url:
                reachable.append(
                    MinerEndpoint(
                        uid=int(n.uid),
                        hotkey=hotkey,
                        ip="",
                        port=0,
                        announced_url=url,
                        source="commitment",
                    )
                )
                continue
            unannounced += 1
        return reachable, unannounced

    def uid_of(self, netuid: int, hotkey: str) -> int | None:
        """Which uid a hotkey holds on this subnet, or `None` if it holds none.

        v11's `Metagraph.by_hotkey` is a **lookup method taking the ss58**, not
        the mapping its name suggests: `mg.by_hotkey` is callable and
        `mg.by_hotkey(ss58)` returns a `MetagraphNeuron` or `None`. Treating it
        as a dict raises `TypeError: by_hotkey() missing 1 required positional
        argument`, which is how this was found.
        """
        neuron = self.metagraph(netuid).by_hotkey(hotkey)
        return None if neuron is None else int(neuron.uid)

    def commit_reveal_enabled(self, netuid: int) -> bool:
        """Whether this subnet turns `SetWeights` into a timelocked commitment.

        Worth a read of its own because it changes what a successful receipt
        means. On netuid 602 `commit_reveal_enabled` is True and
        `commit_reveal_period` is 1, and a plain `SetWeights` there was not
        stored as a weight vector at all: it became an entry in
        `timelocked_weight_commits` carrying a `commit_block`, a drand
        `reveal_round`, a `reveals_at` timestamp and an AES-GCM `ciphertext`.
        The vector is encrypted to a future beacon round and cannot be read --
        by the chain or by anyone else -- until that round publishes.

        So `success=True` attests that **the commitment** landed. The weights
        are not live, and will not be, until the reveal and then an epoch step.
        Measured on 602: a commit at block 8334370 was still not reflected in
        any neuron's incentive at the epoch step two blocks later, and the first
        step that acted on it was 8334732 -- about 72 minutes after the receipt
        said Success.
        """
        return bool(self._st.subnets.commit_reveal_enabled(netuid=netuid))

    def stored_weights(self, netuid: int) -> dict[int, dict[int, float]]:
        """The weight vectors the chain is actually using, keyed by setter uid.

        Exposed because verifying a write means reading it back, and a caller
        that has to reach into `_st` to do that will skip it. Note the chain
        does not echo what you sent: see `bt_validator.stored_matches`.
        """
        return dict(self._st.weights.weights(netuid=netuid))

    def commitments(self, netuid: int) -> dict[str, Any]:
        """Every commitment on this subnet, keyed by hotkey.

        `subtensor.subnets.commitments` rather than reading the whole
        metagraph: the SDK documents it as batched storage-map reads pinned to
        one block with no metagraph runtime call, which is the difference
        between a cheap per-epoch lookup and one that scales with the subnet.

        Each value carries `.uid` (`None` once the hotkey has deregistered),
        `.value` (the content visible *now*), `.block`, and `.status` -- the
        last because the Commitments pallet also holds timelocked payloads that
        are sealed until a drand round publishes, exactly as `SetWeights`
        becomes a sealed commit on a commit-reveal subnet. A sealed commitment
        has nothing readable in it yet, and says so rather than looking empty.
        """
        return dict(self._st.subnets.commitments(netuid=netuid))

    def endpoint_commitments(self, netuid: int) -> dict[str, str]:
        """Hotkey -> announced URL, for the commitments that carry one.

        Everything else on the subnet is skipped silently: other hotkeys
        publish their own formats into the same storage, and discovery that
        raises on a stranger's payload is discovery a stranger can switch off.
        """
        out: dict[str, str] = {}
        for hotkey, record in self.commitments(netuid).items():
            url = endpoint_from_commitment(getattr(record, "value", None))
            if url:
                out[str(hotkey)] = url
        return out

    def subnet_identity(self, netuid: int) -> dict[str, str] | None:
        """What the subnet calls itself, or None if it has never said.

        `None` is the default and was this subnet's own state until 2026-10-30:
        a netuid with a working mechanism and no name. Worth a public reader
        rather than a reach into `_st`, because the honest rendering of a blank
        field is "not stated" and a caller can only do that if it can see it.
        """
        got = self._st.subnets.subnet_identity(netuid=netuid)
        if got is None:
            return None
        # v11 hands back an object here, not a mapping, so normalise to the
        # plain dict the callers (and JSON) actually want.
        if isinstance(got, dict):
            return {str(k): str(v) for k, v in got.items()}
        fields = (
            "subnet_name",
            "github_repo",
            "subnet_contact",
            "subnet_url",
            "discord",
            "description",
            "logo_url",
            "additional",
        )
        return {f: str(getattr(got, f, "") or "") for f in fields}

    def hyperparameters(self, netuid: int) -> dict[str, Any]:
        """The subnet's hyperparameters, always as a dict.

        v11 returns a MAPPING here, not an object. `getattr(hp, "...", default)`
        therefore never raises and never finds anything -- it just returns the
        default, so every subnet reads as closed and every threshold reads as
        whatever the caller guessed. Normalised in one place so no caller has to
        remember which it is.
        """
        hp = self._st.subnets.subnet_hyperparameters(netuid=netuid)
        return dict(hp) if hp is not None else {}

    def registration_burn(self, netuid: int) -> Any | None:
        """What a burned registration costs on this subnet AT THIS BLOCK.

        Not in `subnet_hyperparameters`: that carries `min_burn`, `max_burn`,
        `burn_half_life` and `burn_increase_mult`, but no current figure. The
        live price is `subnets.burn(netuid=...)`, and reading it per-attempt is
        the whole lesson of this subnet's registration history -- a fresh
        subnet starts near `max_burn` (τ100 here) and decays toward `min_burn`,
        so the same registration cost τ0.902987384, then τ0.539024711, then
        τ0.014028537 at three different blocks on one day.

        Returns None when the SDK does not expose it, and callers must treat
        None as UNKNOWN rather than as affordable.
        """
        fn = getattr(self._st.subnets, "burn", None)
        if fn is None:
            return None
        try:
            return fn(netuid=netuid)
        except Exception:  # noqa: BLE001 -- an unreadable price is not a free one
            return None

    # ---- writes. Each one costs TAO and is signed by a real key. ----

    def submit(self, intent: Any, wallet: Any, **kwargs: Any) -> Any:
        """Sign and submit one intent through the policy-gated choke point.

        Every write in this adapter funnels through here so there is a single
        place that could ever spend money, and a single place to audit.

        `execute`, not `submit_call`. This was wrong in the first version and the
        chain caught it: `submit_call` is documented in the SDK as an "escape
        hatch: submit any generated raw call directly", and an active `Policy`
        **refuses it** unless that policy sets `allow_raw_calls=True`. v11 is
        intent-based — `bt.SetWeights(...)` and friends are `Intent` dataclasses,
        not calls — and `execute` is the gate that previews them and applies
        policy. Routing writes through the escape hatch would have meant opting
        out of the SDK's own safety rail to do the ordinary thing.
        """
        return self._st.execute(intent, wallet, **kwargs)

    def set_weights(
        self,
        wallet: Any,
        netuid: int,
        weights: dict[int, float],
        *,
        version_key: int = 0,
    ) -> Any:
        """Commit a validator's weight vector for one subnet.

        Takes the `dict[uid, weight]` that `scoring.weights_from_scores` already
        returns, so the validator does not reshape its own output to suit the
        wire. An empty mapping is refused rather than sent as an empty vector:
        `weights_from_scores` returns `{}` to mean "nobody earned anything this
        epoch", and the honest expression of that is to set no weights at all.
        Sending an empty vector would instead be read as a positive statement
        about the subnet, which is a different claim.
        """
        if not weights:
            raise ValueError(
                "refusing to set an empty weight vector: no miner scored above "
                "zero this epoch, and that is a reason to stay silent rather "
                "than to publish a vector that says nothing"
            )
        uids = sorted(weights)
        call = self._bt.SetWeights(
            netuid=netuid,
            uids=uids,
            weights=[weights[u] for u in uids],
            version_key=version_key,
        )
        return self.submit(call, wallet)

    def serve_axon(
        self, wallet: Any, netuid: int, ip: str, port: int, *, protocol: int = 4
    ) -> Any:
        """Publish where this miner can be reached.

        The chain stores an address, not a payload — v11 has no transport of its
        own. Whatever answers on `ip:port` is our own HTTP server.
        """
        call = self._bt.ServeAxon(netuid=netuid, ip=ip, port=port, protocol=protocol)
        return self.submit(call, wallet)

    def set_commitment(self, wallet: Any, netuid: int, payload: dict[str, Any]) -> Any:
        """Publish bytes of our own into this subnet's commitment storage.

        THE ONE PLACE THE ESCAPE HATCH IS THE RIGHT TOOL, and worth saying why
        because `submit` above exists to say the opposite. v11 wraps extrinsics
        as `Intent` dataclasses and `execute` previews and policy-checks them;
        routing an ordinary write through `submit_call` means opting out of that
        rail. But there is **no commitment Intent**: the ~75 Intents in 11.3.0
        cover `SetWeights`, `ServeAxon`, `SetIdentity`, `SetSubnetIdentity` and
        the rest, and commitments appear only as the generated raw call
        `Commitments.set_commitment(netuid, info)`. So the rule is not "never
        use the hatch", it is: **the hatch is wrong when an Intent covers the
        call and right when none does.** The first version of this adapter used
        it for `SetWeights`, where an Intent did exist, and the chain refused.

        Signed by the HOTKEY, not the coldkey. `submit_call` defaults to
        `signer="coldkey"`, and commitment storage is keyed by
        `(netuid, hotkey)` -- a coldkey-signed commitment would either be
        refused or land against an identity no neuron on the subnet has, which
        is the kind of success that is worse than a failure.

        No `Policy` is active on this client, and the SDK is explicit that "no
        policy means no restriction, exactly as for intents", so this needs no
        `allow_raw_calls`. If a policy is ever added here it must set that flag
        or this call starts failing -- noted because the failure would look
        like a chain problem and would not be one.
        """
        raw = commitment_payload(payload)
        info = {"fields": [{f"Raw{len(raw)}": raw}]}
        call = self._bt.calls.Commitments.set_commitment(netuid=netuid, info=info)
        return self._st.submit_call(call, wallet, signer="hotkey")

    def set_subnet_identity(
        self,
        wallet: Any,
        netuid: int,
        *,
        subnet_name: str,
        description: str = "",
        github_repo: str = "",
        subnet_url: str = "",
        subnet_contact: str = "",
        discord: str = "",
        logo_url: str = "",
        additional: str = "",
    ) -> Any:
        """Name the subnet, so it stops reading as `unknown` to everyone else.

        Owner-only: the signing coldkey must own `netuid`, and the chain
        refuses it otherwise. Measured on 602 before this existed,
        `subnets.subnet_identity(netuid=602)` returned `None` -- the subnet had
        a mechanism, two neurons and a moved reward, and no name. That is not
        cosmetic if the goal is for anyone else to join: a registration burn is
        a real payment, and nobody pays it into an unnamed slot number.

        Only `subnet_name` is required. The rest default to empty rather than
        to invented values -- an identity that states a Discord nobody runs is
        worse than one that states nothing, because the empty field is honest
        and the wrong one wastes somebody's afternoon.
        """
        intent = self._bt.SetSubnetIdentity(
            netuid=netuid,
            subnet_name=subnet_name,
            github_repo=github_repo,
            subnet_contact=subnet_contact,
            subnet_url=subnet_url,
            discord=discord,
            description=description,
            logo_url=logo_url,
            additional=additional,
        )
        return self.submit(intent, wallet)

    # ---- wallets. Keys, never key material. ----

    def wallet(self, name: str, hotkey: str) -> Any:
        """Open an existing wallet by name.

        This never creates one and never touches a mnemonic. Wallet creation is
        `btcli wallet new_coldkey`, which puts the seed phrase under
        ~/.bittensor/wallets/ and shows it to a human exactly once. A seed phrase
        must not pass through this process, be written to the repo, land in
        `.env`, or appear in a log — so the one function that could have done
        that does not exist.
        """
        return self._bt.Wallet(name=name, hotkey=hotkey)
