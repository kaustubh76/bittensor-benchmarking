"""Global configuration.

Values here are load-bearing for the *math*, not just deployment — most
notably ``usdc_fee_bps`` and ``usdc_fee_flat``, which make the manipulation
bound (Pillar 3) a fixed number rather than a random variable. That determinism
is the whole reason the bound is computable on Arc.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import field_validator, model_validator
from pydantic_core import PydanticUseDefault
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class ChainProfile:
    """What differs between Arc networks, from primary sources — never guessed.

    Mainnet values: docs.arc.io (RPC endpoints, contract addresses) and
    developers.circle.com (Gateway and Wallets supported-blockchains), read 2026-09-18
    and re-probed 2026-09-21: the mainnet RPC and explorer answer without credentials,
    and Gateway lists Arc mainnet without the private-preview header.
    Testnet values are the ones this deployment has run against since July.
    """

    name: str
    explorer: str
    #: The RPC a VISITOR's wallet is told about (EIP-3085 `wallet_addEthereumChain`).
    #: Never the server's `arc_rpc_url`, which may carry a provider key.
    public_rpc: str
    #: Circle's GatewayWallet on this chain — the contract a payer deposits into.
    gateway_wallet: str
    #: The blockchain enum Circle's Wallets (W3S) API uses for this chain.
    circle_blockchain: str
    #: The chain key Circle's Gateway SDK (`@circle-fin/x402-batching`) uses.
    gateway_chain: str
    #: Send Gateway the `X-ARC-PRIVATE-MAINNET-ENABLED` header. Arc mainnet's
    #: permissioned preview ended before 2026-09-21 (Gateway lists it without the
    #: header now); the flag stays for an env override and is harmless when sent.
    private_mainnet: bool


CHAIN_PROFILES: dict[int, ChainProfile] = {
    5042: ChainProfile(
        name="Arc",
        explorer="https://explorer.arc.io",
        public_rpc="https://rpc.mainnet.arc.io",
        gateway_wallet="0x77777777Dcc4d5A8B6E418Fd04D8997ef11000eE",
        circle_blockchain="ARC",
        gateway_chain="arc",
        private_mainnet=False,
    ),
    5042002: ChainProfile(
        name="Arc Testnet",
        explorer="https://testnet.arcscan.app",
        public_rpc="https://rpc.testnet.arc.io",
        gateway_wallet="0x0077777d7EBA4688BDeF3E311b846F25870A19B9",
        circle_blockchain="ARC-TESTNET",
        gateway_chain="arcTestnet",
        private_mainnet=False,
    ),
    31337: ChainProfile(
        name="anvil", explorer="", public_rpc="", gateway_wallet="", circle_blockchain="",
        gateway_chain="", private_mainnet=False,
    ),
}


class ACRSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="ACR_", env_file=".env", extra="ignore")

    @field_validator("*", mode="before")
    @classmethod
    def _ignore_comment_pollution(cls, v):
        """Treat a leftover inline-comment value as unset.

        A ``.env`` whose empty placeholder lines carry a trailing ``# comment``
        (e.g. ``ACR_X402_PAY_TO=   # our wallet``) makes dotenv read the *comment*
        as the value — a non-empty string that silently selects the live path
        (Circle facilitator / wallet) instead of the safe dev/offline default.
        ``PydanticUseDefault`` (not ``""``) keeps this fail-safe for EVERY field
        type: a blank string would be a hard ValidationError on the numeric
        fields (``x402_price_usdc``, ``x402_max_timeout_seconds``, …), crashing
        every ``get_settings()`` on exactly the misconfig this tolerates.
        """
        if isinstance(v, str) and v.lstrip().startswith("#"):
            raise PydanticUseDefault()
        return v

    # --- estimator ---
    #: Trimmed-median trim fraction per tail (α-trim).
    trim_alpha: float = 0.10
    #: Target volume per volume-time bar, in USDC notional.
    bar_volume_usdc: float = 25_000.0
    #: Bootstrap resamples for the per-print confidence interval.
    ci_bootstrap: int = 500
    #: Confidence level for the CI (two-sided).
    ci_level: float = 0.95
    #: Per-cluster volume cap as a fraction of total window volume.
    cluster_volume_cap: float = 0.05
    #: Seed for the two stages that draw randomly: the Louvain partition and the
    #: bootstrap CI. It lived as a hardcoded default argument on both functions,
    #: reachable by no caller and recorded in no config, so the reproducibility
    #: claim rested on a number nobody could see. It joins `policy_hash`, because
    #: a different seed can produce a different partition and therefore a
    #: different set of exclusions.
    estimator_seed: int = 0
    #: Assumed Gateway batch width (seconds) — sets the deconvolution window.
    batch_interval_seconds: float = 300.0

    # --- Arc / USDC economics (make the bound a NUMBER) ---
    #: Proportional USDC transfer fee, basis points of notional.
    usdc_fee_bps: float = 1.0
    #: Flat USDC fee per transfer.
    usdc_fee_flat: float = 0.0001

    # --- chain ---
    arc_rpc_url: str = "http://127.0.0.1:8545"
    oracle_address: str = ""
    registry_address: str = ""
    #: Deployed ``ACRFutures`` address — the on-chain cash-settled future that
    #: settles against ``ACROracle``. Empty → the futures desk stays off (the
    #: term structure still renders from the maker model; no live positions).
    futures_address: str = ""
    #: Deployed ``FeedAccessAttestor`` — the contract that turns a paid x402
    #: query into an on-chain feed-access right. Written only by
    #: scripts/attest_feed_access.py; the service reads it so the Terminal can
    #: name the fourth contract instead of pretending it does not exist.
    #: Empty → the chip stays off rather than rendering a zero address.
    attestor_address: str = ""
    #: Deployed ``ACROracleV2`` — the oracle whose prints carry the cleaning
    #: policy hash, the estimation window and the human-denominated bound, so a
    #: print can be re-derived rather than trusted. Deployed ALONGSIDE v1, never
    #: instead of it: ``ACRFutures.oracle`` is immutable and the venue refuses a
    #: print older than two hours, so if v1 stopped printing every expired open
    #: series would be unsettleable and its collateral stranded. Empty → the
    #: poster posts to v1 only, exactly as before.
    oracle_v2_address: str = ""
    #: Deployed ``ReceiptMirror`` — the contract that puts an off-chain Gateway
    #: settlement on chain so the subgraph has a tape to index. Circle settles
    #: x402 off-chain and returns a batch UUID, not a transaction, so without
    #: this there is no settlement event on Arc at all and TCA has no basis.
    #: Empty → the mirror keeper stands down rather than writing nowhere.
    receipt_mirror_address: str = ""
    #: Deployed ``PolicyWallet`` for THIS business — the contract that holds its
    #: USDC and decides what its agent may move. One wallet per business, so this
    #: is the operator's own; another business's wallet is passed to
    #: ``PolicyClient`` explicitly rather than read from here.
    #: Empty → the operator spends nothing and escalates everything, which is a
    #: working state and the correct one for an unconfigured host.
    policy_wallet_address: str = ""
    #: Private key the oracle-poster signs prints with (EIP-712) and relays.
    #: Empty → the in-service poster stays offline (logs the payload only).
    poster_private_key: str = ""
    #: Private key the `reader` role signs AGENT CARDS with — nothing else.
    #: Deliberately separate from `poster_private_key`: a read-only card has no
    #: business being signed by the key that posts the oracle, and
    #: `build_role_signer("reader")` refuses to fall back to it. Empty → a caller
    #: mints no card and goes anonymous, which is a working state.
    reader_private_key: str = ""
    #: Private key the `owner` role signs with — the escalation authority, and
    #: nothing else.
    #:
    #: SEPARATE BECAUSE THE CONTRACT REQUIRES IT TO BE. `PolicyWallet.spendAsOwner`
    #: is the path a human takes to settle what the agent would not, and both the
    #: contract and `PolicyClient` refuse it when the owner is also the agent —
    #: an approval signed by the thing being approved is not an approval. With
    #: only `poster_private_key` to fall back on, both roles resolved to one
    #: address and the escalation queue could never be cleared: the configuration
    #: could not express the two parties the design is built on.
    #:
    #: Empty → the owner role falls back as before, which is correct for a
    #: deployment with no escalations and no PolicyWallet.
    owner_private_key: str = ""

    # --- Arc network (verified testnet facts) ---
    #: Arc testnet chain id. Arc makes USDC a native system contract that is
    #: also the gas token, so gasless UX is intrinsic (no ERC-4337 paymaster).
    arc_chain_id: int = 5042002
    #: USDC on Arc — a native system contract address (NOT a deployed ERC-20).
    usdc_address: str = "0x3600000000000000000000000000000000000000"
    #: CAIP-2 network id; empty → derived as ``eip155:{arc_chain_id}``.
    arc_network_caip2: str = ""
    #: Block-explorer base URL (tx/address links in the Terminal). Empty → the
    #: chain profile's; set it only to override.
    explorer_base: str = ""
    #: Send Gateway the Arc private-mainnet header. None → the profile's (False
    #: everywhere since the preview ended); ACR_ARC_PRIVATE_MAINNET=1 forces it on.
    arc_private_mainnet: bool | None = None
    #: The RPC a visitor's wallet is given for this chain. Empty → the profile's
    #: public endpoint. Never defaults to `arc_rpc_url`: that one may carry a key.
    public_rpc_url: str = ""

    # --- Circle Developer-Controlled Wallets (empty → raw-key / offline) ---
    circle_api_key: str = ""  # PREFIX:ID:SECRET
    circle_entity_secret: str = ""  # 32-byte hex; SDK RSA-encrypts a ciphertext per call
    circle_wallet_id: str = ""  # the oracle poster / deployer wallet
    circle_wallet_set_id: str = ""  # for wallet creation / deploys
    #: The venue's two custody wallets, kept SEPARATE from the poster above so
    #: one credential is not four jobs. They must also be distinct from each
    #: other: ``ACRFutures.trade`` reverts "maker cannot take" when the caller is
    #: the series maker, so a single wallet cannot both quote and trade the book.
    #: Empty → the caller falls back to whatever ``build_signer`` selects, which
    #: keeps every offline/anvil path working unchanged. See docs/WALLETS.md.
    circle_owner_wallet_id: str = ""  # governance: may open a series
    circle_maker_wallet_id: str = ""  # the book's counterparty
    circle_taker_wallet_id: str = ""  # the hourly heartbeat
    circle_base_url: str = "https://api.circle.com"
    #: Optional base64 DER (SPKI) ECDSA P-256 public key to verify Circle webhook
    #: signatures fully offline. Blank → the receiver fetches the key from Circle
    #: by key-id (needs circle_api_key) and caches it.
    circle_webhook_public_key: str = ""
    #: JSONL append-log of inbound Circle webhook events (one event per line).
    #: Survives restarts and rehydrates the /webhooks/recent feed. Blank disables
    #: the file (in-memory only). Relative paths resolve from the process cwd.
    webhook_log_path: str = "data/webhook_events.jsonl"

    # --- the autonomous hedger's two PUBLIC identities --------------------
    # Addresses, not credentials: /hedger derives the agent's whole state from
    # public chain data plus the receipt ledger and holds no key for either.
    # They belong here so `.env` reaches them the same way it already reaches
    # `futures_address`. Until it did, `index_api/hedger.py` read raw
    # os.environ only — so `make snapshot` from a checkout wrote a bundle
    # carrying the venue the agent trades on beside an agent that reported
    # itself as not configured, and the offline edition lost its protagonist.
    # Two addresses, one agent (docs/WALLETS.md):
    #: the SCA — what ``ACRFutures.trade`` records as the taker, because it
    #: reads ``msg.sender``.
    hedger_address: str = ""
    #: the backing EOA — what the x402 settlement records as the payer, because
    #: EIP-3009 needs a signature ``ecrecover`` can verify and Circle signs with
    #: that EOA. Empty → the surface reports itself as not configured rather
    #: than inventing an empty agent.
    hedger_payer: str = ""

    # --- x402 / Nanopayments (empty facilitator url → dev-mode gate) ---
    #: Facilitator selection: "auto" (Circle iff URL + PAY_TO are set), "dev"
    #: (force the mock gate even when Circle vars are present — demo loops),
    #: or "circle" (force the real gate; fails closed if unconfigured).
    x402_mode: str = "auto"
    x402_facilitator_url: str = ""  # Circle Gateway facilitator base URL
    x402_pay_to: str = ""  # our receiving wallet (the seller)
    x402_scheme: str = "exact"  # EIP-3009; GatewayWalletBatched carried in `extra`
    x402_max_timeout_seconds: int = 60
    x402_resource_base: str = ""  # public URL base; empty → derived from the request
    #: Per-query price in USDC (sub-cent — a Nanopayment).
    x402_price_usdc: float = 0.0001
    #: JSONL append-log of REAL x402 settlements (one receipt per line) — the
    #: authoritative settlement tape ReceiptSource reads. Survives restarts and
    #: rehydrates /marketplace/receipts + /revenue. Blank (default) disables the
    #: file (in-memory only) — keeps tests + the dev gate from writing to disk;
    #: enable in the live seller (ACR_RECEIPT_LOG_PATH=data/x402_receipts.jsonl).
    receipt_log_path: str = ""
    #: Circle GatewayWallet contract (the EIP-712 verifyingContract buyers sign
    #: against — `extra.verifyingContract` in PaymentRequirements). Default is
    #: the shared testnet GatewayWallet (all Gateway testnet chains, incl. Arc).
    x402_gateway_wallet: str = ""  # empty → the chain profile's GatewayWallet

    # --- World / AgentKit human proofs (empty verifier url → dev-mode gate) ---
    #: Verifier selection, mirroring `x402_mode`: "auto" (AgentKit iff the URL and
    #: app id look real), "dev" (force the mock verifier — demo loops and tests),
    #: or "agentkit" (force the real one; fails closed if unconfigured).
    humanid_mode: str = "auto"
    #: AgentKit proof-verification endpoint. Empty → the dev verifier.
    humanid_verifier_url: str = ""
    #: The World Developer Portal app a proof must be scoped to. A proof minted
    #: for another app must not authorize anything here.
    humanid_app_id: str = ""
    #: THE LINKABILITY SECRET. `clusterId = keccak256(nullifier ‖ salt ‖ window)`,
    #: so anyone holding this and a nullifier can confirm which wallets are that
    #: human's. Never log it, never return it, never let it reach `forge` (script
    #: arguments land in contracts/broadcast/, which is committed). Must hash to
    #: the deployed HumanIdMirror's immutable SALT_COMMITMENT — `humanid.py`
    #: checks that and says so on /health, because a mismatched salt derives
    #: cluster ids that match nothing and reads exactly like "this human has
    #: never traded".
    humanid_salt: str = ""
    #: Demo identities come from the World ID Sandbox, not from Orb-verified
    #: users. Surfaced on /humanid/info so nobody has to take the README's word
    #: for what "verified human" means in this deployment.
    humanid_sandbox: bool = True
    #: keccak256 of the salt above — the value HumanIdMirror was deployed with.
    #: Set both and a mismatch is caught before it can produce a silent zero.
    humanid_salt_commitment: str = ""
    #: Deployed HumanIdMirror (make deploy-humanid). Empty → the resolver stands
    #: down instead of writing nowhere, and no wallet is ever human-backed.
    humanid_mirror_address: str = ""

    # --- World Chain (read-only) ---
    #: AgentBook lives on World Chain, not Arc, so it needs its own endpoint —
    #: this is the first reader in the codebase that points at a second chain.
    #: Empty → the fixture AgentBook, which is what the demo runs on until
    #: Sandbox access exists.
    world_rpc_url: str = ""
    agentbook_address: str = ""
    #: Which AgentBook to read: "auto" (the real one iff an endpoint or address
    #: is named), "fixture" (the demo roster), or "worldchain" (the live
    #: contract, which needs no configuration — address and RPC are constants).
    agentbook_mode: str = "auto"

    # --- Model Armor: the screen on agent-to-agent traffic ------------------
    #: Which screen to run: "auto" (gcp iff project+template+credentials are all
    #: set, else local), "local" (deterministic, offline), "gcp" (force the real
    #: one and fail closed if unconfigured), or "off" (no screening, stated
    #: rather than silent). Mirrors `x402_mode` and `humanid_mode` so an operator
    #: learns one vocabulary.
    armor_mode: str = "auto"
    armor_project_id: str = ""
    #: Model Armor is REGIONAL and the endpoint carries the region twice, so a
    #: wrong value here fails as a 404 on the template rather than a connection
    #: error. No default: guessing a region is how a template that exists in
    #: asia-south1 gets reported as absent.
    armor_location: str = ""
    armor_template: str = ""
    #: A PATH to the service-account JSON, never the JSON itself. `ACRSettings`
    #: reads `.env`, and `.env` is the file most likely to be pasted into an
    #: issue. Model Armor is IAM-gated and rejects API keys, so this is a
    #: service-account key or nothing — there is no key-shaped credential for it.
    armor_credentials_file: str = ""
    #: Seconds. A screen sits in the request path, so this is a latency budget
    #: and not a generosity: on timeout the screen FAILS CLOSED, because a screen
    #: that admits traffic when it cannot inspect it is not a screen.
    armor_timeout_s: float = 10.0

    # --- Agent cards: who is calling -----------------------------------------
    #: Who cards must be addressed to. This is what stands in for
    #: `verifyingContract` in the card's EIP-712 domain: the domain deliberately
    #: names no contract (that is what permissionless means), so without an
    #: audience a card minted for another ACR-domain service would be presentable
    #: here. Never make this a wildcard.
    agent_audience: str = "acr-index-api"

    #: Comma-separated allowed CORS origins for the public API (so the dashboard
    #: /any browser can query it cross-origin). "*" = allow all (the testnet-demo
    #: default; the API serves public read data + the x402 gate); set to the
    #: Terminal's origin(s) to lock it down. Empty disables CORS entirely.
    cors_origins: str = "*"
    # --- the Desk feed pass: a plain on-chain USDC payment that buys timed access ---
    #: What a pass costs, in USDC, paid by the Desk's smart account to `x402_pay_to`.
    pass_price_usdc: float = 1.0
    #: How long a pass grants, in seconds. Short by design: re-buying is one PIN
    #: ceremony, and a signer compromise should cost days, not a quarter.
    pass_window_s: int = 86_400
    #: A claim must name a transfer mined within this many seconds — a receipt
    #: from last month is not a purchase made today.
    pass_claim_max_age_s: int = 3_600
    #: The faucet, the demo buyer and the attack lab. Honoured on testnet only;
    #: on mainnet `acr_core.mainnet_guard.testnet_surfaces_enabled` is False
    #: regardless of this value — see that module for why no env can flip it.
    testnet_surfaces: bool = True

    # --- The Graph ---
    #: Subgraph query URL (Subgraph Studio). Empty → every subgraph-backed
    #: surface reports itself unavailable rather than serving a zero, because a
    #: TCA of "0 bp" and a TCA of "we could not read the tape" are different
    #: facts and a reader must never see one rendered as the other.
    subgraph_url: str = ""
    #: Studio API key. SERVER-SIDE ONLY — it is why the read proxy exists; a key
    #: shipped to a browser is a key anyone can spend the quota of.
    graph_api_key: str = ""

    # --- tape source selection ---
    #: "sim" (default, calibrated simulator), "arc" (live Arc testnet USDC scan),
    #: "receipts" (the authoritative x402 settlement ledger — needs
    #: receipt_log_path populated by a live seller), "graph" (the indexed
    #: settlement tape, which is the only source carrying a per-seller unit
    #: price and so the only one TCA can be computed from), or "chutes"
    #: (realized inference prices from Bittensor subnet 64 — the only source
    #: whose prices someone outside this project actually paid; DAILY, and
    #: single-venue, so it needs a daily window and publishes no attack cost.
    #: See docs/BITTENSOR.md and index_api.store.default_source).
    tape_source: str = "sim"
    #: Events per service the simulator generates for the default sim tape. The
    #: rich local default (24k) is memory-heavy; small cloud instances (e.g. a
    #: 512MB Render free tier) should lower it (~3000) so the store build fits.
    sim_events_per_service: int = 24_000
    #: Sim economic horizon (seconds). Window density = events_per_service /
    #: (horizon/3600); the estimator needs a dense hourly window (~1000 events).
    #: On a small cloud box, shrink BOTH this and events_per_service together to
    #: keep density high (estimable) while cutting total events (memory). Default
    #: 24h matches acr_sim's SimConfig default.
    sim_horizon_seconds: float = 86_400.0
    #: Per-hour event count for the "Attack the Index" exhibit sims (attack.py)
    #: AND the live Attack Lab run (demo.py). These are the heaviest transient
    #: allocation in the app (a 12h error-series sim + two 1h attack sims), so a
    #: 512MB cloud box lowers it (~1250) to avoid an OOM spike. Floor ~800: below
    #: that the hour-0 cleaning stack leaves zero observations and the run errors
    #: (verified at 400). Default preserves the rich local exhibit.
    attack_sim_events_per_service: int = 2_500
    #: How many blocks back ArcSource scans for USDC transfer logs. Kept modest
    #: because USDC is Arc's native gas token → Transfer logs are dense; ArcSource
    #: also adaptively shrinks the range if the RPC still rejects it as too large.
    arc_tape_lookback_blocks: int = 800

    # --- Bittensor / Taostats (all optional; empty key → offline, no network) ---
    #: Taostats API key. Empty (the default) means every Taostats method returns
    #: None and the subnet still runs — the base install must stay deployable
    #: with no credentials, the same contract the Circle and Armor extras hold.
    #: Free tier is 5 requests/minute and 10,000/month, which is why
    #: taostats_max_calls_per_cycle exists.
    taostats_api_key: str = ""
    #: Base URL. Split from the route table (acr_tape.taostats_client.ROUTES) so
    #: a host change and a path change are separate edits — the `/api/...` paths
    #: retire 2026-11-09 and the replacements are not yet published.
    taostats_base_url: str = "https://api.taostats.io"
    #: Hard per-cycle call budget. 10,000/month over hourly cycles is ~13.9 an
    #: hour against a 5/minute cap, so a cycle that wants more than this should
    #: fail loudly rather than quietly spend the month.
    taostats_max_calls_per_cycle: int = 3
    #: ArcSource attested-market mode (1 = on): decode a real unit PRICE from
    #: each transfer amount (price = notional / IndexSpec.arc_unit_qty), resolve
    #: each event's service from the seller's on-chain attestation, and DROP
    #: events from non-attested sellers — attestation earns index inclusion.
    #: 0 = legacy audit decode (price pinned to the reference level, everything
    #: resolved to INFERENCE).
    arc_attested_only: int = 0

    # --- service ---
    #: Seconds between index_api store refreshes / oracle-post cycles.
    refresh_seconds: float = 30.0
    #: How stale the on-chain record must be before a FRESHLY STARTED process
    #: posts immediately instead of waiting out a full cycle. None → one full
    #: `refresh_seconds`, which is the behaviour every deployment had.
    #:
    #: It needs its own knob because the two timers answer to different things.
    #: `refresh_seconds` is the product's cadence: the Fixing is hourly. This one
    #: is a property of the HOST, and on 2026-09-27 the mainnet service made the
    #: difference concrete: on a 512 MiB free instance it was OOM-killed every few
    #: minutes, so the hourly timer never fired, and because a quick restart left
    #: the record younger than one hour the wake path did not fire either. The
    #: fixing froze at one reading on a press that was technically alive. Setting
    #: this below `refresh_seconds` makes every restart top the record up, which
    #: turns an unstable host from a silent outage into a slightly noisy press.
    wake_post_after_s: float | None = None

    #: The balance below which the press stops posting PRINTS, keeping what is
    #: left for receipt mirroring. Not an alarm — `PRESS_CRITICAL_FLOOR_USDC`
    #: (1.0) is the alarm, and it only ever reported. This is the brake.
    #:
    #: The press has two jobs and they are NOT equally recoverable. A missed
    #: print is a gap: the next one fills it, and the terminal already renders a
    #: print's age. A missed receipt mirror is lost EVIDENCE — an unmirrored
    #: settlement never reaches the subgraph, so `/tca` can never see it, and
    #: `/tca` outliving the restart that erased `/revenue`'s counter is the whole
    #: argument for ReceiptMirror. Both came out of one wallet with no priority,
    #: so the press would spend to zero and take the irreplaceable one down with
    #: the recoverable one.
    #:
    #: Sized from measured mainnet receipts, not guessed: a posting cycle is six
    #: transactions (three indices x both oracle generations) at 0.005191 USDC
    #: for v2 (~259k gas) and 0.003584 for v1 (~179k) = 0.0263 per cycle; a
    #: mirror is two transactions, ~0.0088. So 0.25 leaves roughly 22 mirrors
    #: plus a cycle's headroom. Set it to 0 to restore the old behaviour of
    #: spending until the wallet is empty.
    press_print_floor_usdc: float = 0.25

    # --- pricing / instrument ---
    #: Avellaneda–Stoikov inventory risk aversion.
    as_gamma: float = 0.1
    #: A-S order-book liquidity (fill-intensity decay). The quoter's base
    #: spread is (2/γ)·ln(1+γ/κ) of mid — κ=1.5 made that ~129% (a degenerate
    #: full-width corridor); κ=400 models the deep simulated book and lands
    #: the base spread at ~50bp, with the inventory term skewing on top.
    as_kappa: float = 400.0

    @property
    def wake_post_after(self) -> float:
        """The staleness that justifies posting on wake — never longer than a cycle."""
        if self.wake_post_after_s is None:
            return float(self.refresh_seconds)
        return min(float(self.wake_post_after_s), float(self.refresh_seconds))

    @property
    def chain_profile(self) -> ChainProfile:
        return CHAIN_PROFILES.get(int(self.arc_chain_id)) or ChainProfile(
            name=f"chain {self.arc_chain_id}", explorer="", public_rpc="", gateway_wallet="",
            circle_blockchain="", gateway_chain="", private_mainnet=False,
        )

    @property
    def chain_name(self) -> str:
        return self.chain_profile.name

    @property
    def circle_blockchain(self) -> str:
        """Circle Wallets' enum for this chain (`ARC-TESTNET`, `ARC`)."""
        return self.chain_profile.circle_blockchain

    @property
    def gateway_chain(self) -> str:
        """Circle Gateway SDK chain key (`arcTestnet`, `arc`)."""
        return self.chain_profile.gateway_chain

    @property
    def private_mainnet(self) -> bool:
        if self.arc_private_mainnet is not None:
            return bool(self.arc_private_mainnet)
        return self.chain_profile.private_mainnet

    @model_validator(mode="after")
    def _resolve_chain_defaults(self):
        """Fill the chain-shaped blanks from the profile, so every reader of
        `explorer_base` / `x402_gateway_wallet` keeps working unchanged and a
        testnet-only literal never has to be typed into an env for mainnet."""
        prof = self.chain_profile
        if not self.explorer_base.strip():
            self.explorer_base = prof.explorer
        if not self.x402_gateway_wallet.strip():
            self.x402_gateway_wallet = prof.gateway_wallet
        if not self.public_rpc_url.strip():
            self.public_rpc_url = prof.public_rpc
        return self

    def caip2(self) -> str:
        """CAIP-2 network id for x402 PaymentRequirements (e.g. ``eip155:5042002``)."""
        return self.arc_network_caip2 or f"eip155:{self.arc_chain_id}"


_settings: ACRSettings | None = None


def get_settings() -> ACRSettings:
    """Process-wide singleton settings (env-overridable).

    NOTE: this is a mutable process-wide singleton — changing environment
    variables after the first call has no effect until ``reset_settings()``.
    Tests that tweak env should call ``reset_settings()`` to force a reload.
    """
    global _settings
    if _settings is None:
        _settings = ACRSettings()
    return _settings


def reset_settings() -> None:
    """Drop the cached settings so the next ``get_settings()`` re-reads env."""
    global _settings
    _settings = None
