"""Terminal payload chain block, /health wiring card, sim ledger, offline bundle.

Covers the frontend contract for the crypto-rich offline edition:
  * build_terminal_payload's ``chain`` block (exact keys — the Terminal's
    network identity card),
  * ``build_sim_receipts`` == ``build_receipts`` shape (honest sim labeling),
  * the enriched /health fields,
  * ``scripts/gen_snapshot.py``'s embedded bundle sections
    (marketplace / revenue / x402 / x402_exchange_sample).
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest
from acr_sim import SimConfig
from acr_tape import SimSource
from fastapi.testclient import TestClient
from index_api.app import app, build_terminal_payload, reset_poster, set_poster
from index_api.fleet import FLEET
from index_api.onchain import get_reader
from index_api.poster import OraclePoster
from index_api.store import PrintStore
from index_api.x402 import PAY_TO, DevFacilitator

# gen_snapshot lives in scripts/ (not a package) — imported the same way
# tests/test_claims.py imports eval.py.
_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "scripts"))

import gen_snapshot  # noqa: E402

client = TestClient(app)

#: The frontend contract — build_terminal_payload's chain block, exactly.
CHAIN_KEYS = {
    "name", "chain_id", "caip2", "rpc_url", "public_rpc_url", "explorer_base", "usdc_address",
    "gateway_wallet", "oracle_address", "registry_address", "futures_address",
    # The fourth contract. Deployed and exercised long before anything named
    # it, which is exactly why this set is frozen: a card that quietly grows
    # or shrinks is a frontend contract nobody is holding.
    "attestor_address",
    # Circle's identifiers for the chain, so the frontend's payer and Desk never
    # carry a testnet literal — and the private-mainnet header flag the paywall
    # and the browser payer both key on.
    "circle_blockchain", "gateway_chain", "private_mainnet",
    # The fifth, and the same story again: HumanIdMirror publishes the rotated
    # cluster ids the human-denominated bound rests on, and was on chain before
    # any surface named it. Widened here deliberately and in the same change as
    # the payload — which is the whole point of freezing the set, because
    # lib/chain.ts holds a SECOND copy of this contract and nothing but a red
    # test connects the two across the language boundary.
    "humanid_address",
    "gate", "tape_source", "signer", "poster",
}


@pytest.fixture()
def small_store(monkeypatch):
    # A tiny tape + a stubbed attack exhibit so the payload builds in ~a second.
    monkeypatch.setattr(
        "index_api.attack.attack_snapshot", lambda: {"per_index": [], "series": []}
    )
    st = PrintStore(source=SimSource(config=SimConfig(seed=4, horizon=3600.0, events_per_service=800)))
    st.refresh(ts=3600.0)
    return st


def test_terminal_payload_chain_block_exact_keys(small_store):
    poster = OraclePoster(small_store)
    poster.post_latest()  # offline → per-index {"tx": None, ..., "note": "offline"}
    payload = build_terminal_payload(small_store, get_reader(), poster=poster, fac=DevFacilitator())
    chain = payload["chain"]
    assert set(chain) == CHAIN_KEYS
    assert chain["name"] == "Arc Testnet"
    assert chain["chain_id"] == 5042002
    assert chain["caip2"] == "eip155:5042002"
    assert chain["explorer_base"] == "https://testnet.arcscan.app"
    assert chain["usdc_address"] == "0x3600000000000000000000000000000000000000"
    assert chain["gateway_wallet"] == "0x0077777d7EBA4688BDeF3E311b846F25870A19B9"
    assert chain["circle_blockchain"] == "ARC-TESTNET"
    assert chain["gateway_chain"] == "arcTestnet"
    assert chain["private_mainnet"] is False
    # The payload is public: both RPC fields are the chain's PUBLIC endpoint, so a
    # keyed provider URL in ACR_ARC_RPC_URL never reaches a visitor's wallet.
    assert chain["public_rpc_url"] == "https://rpc.testnet.arc.io"
    assert chain["rpc_url"] == "https://rpc.testnet.arc.io"
    assert chain["oracle_address"] is None and chain["registry_address"] is None
    assert chain["futures_address"] is None
    assert chain["gate"] == "dev"
    assert chain["tape_source"] == "sim"
    assert chain["signer"] is None  # no key configured → offline signer
    assert chain["poster"]["posts"] == len(small_store.latest)
    entry = chain["poster"]["last"]["ACR-INF"]
    assert entry["tx"] is None and entry["block"] is None
    assert entry["note"] == "offline" and entry["at_wall"] > 0


def test_terminal_payload_without_poster_has_null_poster_block(small_store):
    payload = build_terminal_payload(small_store, get_reader())
    assert payload["chain"]["poster"] is None
    assert payload["chain"]["signer"] is None


def test_sim_receipts_shape_matches_live_ledger():
    import asyncio

    from fastapi import Response
    from index_api.marketplace import build_receipts, build_sim_receipts

    fac = DevFacilitator()
    asyncio.run(fac.process(None, "x402 0xagent:0.0001", Response()))
    live = build_receipts(fac)
    sim = build_sim_receipts()
    assert set(sim) == set(live)  # same top-level keys
    # A sim row must carry every field the UI reads off a real one, so the two
    # render identically — that is the guarantee. It may legitimately lack
    # `settled_at`: a simulated receipt never settled, and stamping it with a
    # made-up time would be precisely the dishonesty the sim labelling exists to
    # prevent. The same goes for `tier`: a real row records which tier the
    # buyer's card earned at the gate, and a simulated purchase met no gate, so
    # stamping it "anonymous" would invent a verdict nobody gave. So: subset, not
    # equality, and nothing real-only beyond the time and the tier.
    sim_keys, live_keys = set(sim["receipts"][0]), set(live["receipts"][0])
    assert sim_keys <= live_keys
    assert live_keys - sim_keys <= {"settled_at", "tier"}
    # Deterministic, honestly labeled, realistically addressed.
    assert sim == build_sim_receipts()
    assert [r["seq"] for r in sim["receipts"]] == list(range(24, 0, -1))  # newest first
    for r in sim["receipts"]:
        assert r["scheme"] == "sim" and r["tx_ref"].startswith("sim-")
        assert r["payer"].startswith("0x") and len(r["payer"]) == 42
        assert r["network"] == "eip155:5042002"
        assert r["amount_usdc"] == 0.0001


def test_health_reports_chain_and_facilitator_wiring():
    reset_poster()
    try:
        body = client.get("/health").json()
        assert body["status"] == "ok"  # existing keys intact
        assert body["gate"] == "dev" and body["tape"] == "sim"
        assert body["chain_id"] == 5042002
        assert body["oracle_address"] is None and body["registry_address"] is None
        # Dev gate → the dummy PAY_TO, shortened 0x1234…abcd style.
        assert body["pay_to"] == f"{PAY_TO[:6]}…{PAY_TO[-4:]}"
        assert body["facilitator_host"] is None  # no facilitator URL configured
        assert body["tape_source"] == "sim"
        assert body["poster_last_tx"] is None  # nothing posted on-chain yet
    finally:
        reset_poster()


def test_health_poster_last_tx_surfaces_most_recent_post(small_store):
    poster = OraclePoster(small_store)
    poster.last_posts = {
        "ACR-INF": {"tx": "0xold", "block": 1, "at_wall": 100.0},
        "ACR-GPU": {"tx": "0xnew", "block": 2, "at_wall": 200.0},
        "ACR-DATA": {"tx": None, "block": None, "at_wall": 300.0, "note": "offline"},
    }
    set_poster(poster)
    try:
        assert client.get("/health").json()["poster_last_tx"] == "0xnew"
    finally:
        reset_poster()


def test_poster_records_last_posts_markers(small_store):
    class _FlakyClient:
        last_receipt = None

        def post(self, p):
            if p.index_id == "ACR-GPU":
                raise RuntimeError("boom")
            return None  # offline marker

        def can_post(self):
            return False

    poster = OraclePoster(small_store, client=_FlakyClient())
    poster.post_latest()
    assert poster.last_posts["ACR-GPU"]["note"] == "error: boom", "the reason rides on the record"
    assert poster.last_posts["ACR-INF"]["note"] == "offline"
    assert all(e["tx"] is None for e in poster.last_posts.values())


def test_poster_rehydrates_provenance_from_onchain_events(small_store):
    """Cold-start re-hydration: PricePosted events (as OracleClient.recent_posts
    returns them, chronological) seed last_posts so the provenance panel never
    says "awaiting first live post" over a chain full of real posts."""
    poster = OraclePoster(small_store)
    events = [
        {"index_id": "ACR-INF", "tx": "0xold", "block": 1, "signer": "0xs", "at_wall": 100.0},
        {"index_id": "ACR-INF", "tx": "0xnew", "block": 5, "signer": "0xs", "at_wall": 500.0},
        {"index_id": "ACR-GPU", "tx": "0xgpu", "block": 6, "signer": "0xs", "at_wall": 600.0},
    ]
    assert poster.rehydrate(events) == 3
    assert poster.last_posts["ACR-INF"] == {"tx": "0xnew", "block": 5, "at_wall": 500.0}
    assert poster.last_posts["ACR-GPU"]["tx"] == "0xgpu"
    assert poster.posts == 3
    # Never clobbers live state, and an empty chain read is a clean no-op.
    assert poster.rehydrate([{"index_id": "ACR-DATA", "tx": "0xd", "block": 9, "at_wall": 9.0}]) == 0
    assert "ACR-DATA" not in poster.last_posts
    assert OraclePoster(small_store).rehydrate([]) == 0


def test_provenance_rehydrate_survives_a_failed_first_attempt(small_store):
    """The retry is the fix, not the rehydrate itself.

    This ran exactly once, at startup — the least reliable moment in the
    process's life, when the RPC is busiest and a cold box is warming
    everything at once. One throttled call there left /health reporting
    `poster_last_tx: null` while real posts sat on chain, until the next hourly
    post repopulated it by accident. That state was live in production.
    """
    import asyncio

    from index_api.app import _rehydrate_provenance

    poster = OraclePoster(small_store)
    calls = {"n": 0}

    def flaky_recent_posts():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("429 Too Many Requests")
        return [{"index_id": "ACR-INF", "tx": "0xok", "block": 3, "at_wall": 30.0}]

    poster.client.recent_posts = flaky_recent_posts  # type: ignore[method-assign]

    asyncio.run(_rehydrate_provenance(poster))  # throttled — must not raise
    assert poster.last_posts == {}, "a failed attempt must leave nothing behind"

    asyncio.run(_rehydrate_provenance(poster))  # the next tick repairs it
    assert poster.last_posts["ACR-INF"]["tx"] == "0xok"

    # And once populated it costs nothing, which is what makes it safe on a timer.
    before = calls["n"]
    asyncio.run(_rehydrate_provenance(poster))
    assert calls["n"] == before


def test_snapshot_builder_embeds_bundle_sections():
    payload: dict = {}
    gen_snapshot.embed_bundle_sections(payload)
    assert {"marketplace", "revenue", "x402", "x402_exchange_sample"} <= set(payload)

    # Marketplace: the live catalog builder at resource base "" + sim ledger.
    cat = payload["marketplace"]["catalog"]
    assert cat["x402Version"] == 2 and len(cat["items"]) == 13 + len(FLEET)
    assert all(i["resource"].startswith("/") for i in cat["items"])  # host-less
    # EVERY row must be honestly labelled — which is a stronger guarantee than
    # the old "row 0 is sim". The ledger now leads with the real Circle Gateway
    # settlements from data/x402_receipts_live.jsonl, so a positional assertion
    # would have to be relaxed; instead pin the invariant that actually
    # protects a reader: a row claims `sim` if and only if its ref is a sim ref.
    # Nothing may look real that isn't, and nothing real may be buried as sim.
    for r in payload["marketplace"]["receipts"]["receipts"]:
        assert (r["scheme"] == "sim") == r["tx_ref"].startswith("sim-"), r

    # Revenue: the /revenue shape with counters that AGREE with the embedded
    # sim ledger (the offline /developers page shows both — they must never
    # contradict each other), plus the first 5 sim receipts.
    rev = payload["revenue"]
    ledger = payload["marketplace"]["receipts"]
    assert rev["paid_queries"] == ledger["paid_queries"]
    assert rev["revenue_usdc"] == ledger["revenue_usdc"]
    assert rev["price_usdc"] == 0.0001
    assert [r["tx_ref"] for r in rev["recent"]] == [f"sim-{i}" for i in range(1, 6)]

    # Futures tape: key always present ([] hermetically — no venue configured).
    assert payload["futures_trades"] == []

    # x402: the dev gate descriptor exactly as /x402/info serves it.
    assert payload["x402"]["facilitator"] == "dev"
    assert payload["x402"]["payment_header"] == "PAYMENT-SIGNATURE"
    assert len(payload["x402"]["gated_endpoints"]) == 6  # five index routes + /compute/{label}


def test_snapshot_exchange_sample_is_recorded_not_handwritten():
    ex = gen_snapshot.record_x402_exchange()
    ch = ex["challenge"]
    assert ch["status"] == 402
    assert ch["body"]["x402Version"] == 2
    assert ch["body"]["resource"]["mimeType"] == "application/json"  # top-level resource OBJECT
    # The b64 header decodes to the same spec-shaped envelope as the body —
    # the invariant x402.py maintains; a hand-written sample would drift.
    envelope = json.loads(base64.b64decode(ch["headers"]["PAYMENT-REQUIRED"]))
    assert envelope == ch["body"]
    assert envelope["accepts"][0]["resource"] == "/prints"  # resource base ""

    st = ex["settled"]
    assert st["status"] == 200 and st["body_note"] == "prints payload served"
    assert st["payer"].startswith("0x") and len(st["payer"]) == 42
    confirmation = json.loads(base64.b64decode(st["headers"]["PAYMENT-RESPONSE"]))
    assert confirmation["success"] is True and confirmation["payer"] == st["payer"]
    assert st["headers"]["X-PAYMENT-RESPONSE"] == st["headers"]["PAYMENT-RESPONSE"]


def test_snapshot_chain_guard_refuses_local_chains():
    # A configured oracle + an anvil chain id must refuse to embed (the committed
    # fallback would ship a stale local address); an unreachable RPC (None) can't
    # prove persistence either. The property is PERSISTENCE, so both Arc networks
    # pass — the guard used to name testnet alone and refused the very bundle a
    # mainnet build needs.
    gen_snapshot.check_oracle_commit_guard(5042002)  # Arc testnet → allowed
    gen_snapshot.check_oracle_commit_guard(5042)     # Arc mainnet → allowed
    for cid in (31337, 1337, 12345, None):
        with pytest.raises(SystemExit):
            gen_snapshot.check_oracle_commit_guard(cid)
