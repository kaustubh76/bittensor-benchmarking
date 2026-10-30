"""The operator never pays a payee that would make the payment meaningless or theft.

Found 2026-10-16 while wiring the mainnet PolicyWallet (0x3a5F…4e04). On mainnet:

- `/prints`-type bills pay ACR_X402_PAY_TO, which IS the agent (the press EOA),
  so a "payment" just moves the budget into the spender's own pocket;
- `/compute/*` bills pay DEMO_SELLERS, whose private keys are
  sha256("acr-attest::" + label): public, so anyone could sweep what lands there.

Nothing in `decide` refused either. A PAY to such a payee now escalates with the
reason, and is never sent.
"""
from __future__ import annotations

import pytest
from acr_core import reset_settings
from acr_oracle_client.demo_sellers import DEMO_SELLERS
from index_api.operator import ESCALATE, PAY, Obligation, run_obligation, unpayable_vendor

AGENT = "0x1547B0d3C334F264D28B087566B512AB5CD85379"
VENDOR = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20
THIRD = "0x" + "cc" * 20
RES = "https://acr.example/compute/inference"
NOW = 1_790_000_000.0
MAINNET, TESTNET = 5042, 5042002


@pytest.fixture()
def chain(monkeypatch):
    def use(chain_id: int):
        monkeypatch.setenv("ACR_ARC_CHAIN_ID", str(chain_id))
        reset_settings()
    yield use
    monkeypatch.undo()
    reset_settings()


# ── the rule ──────────────────────────────────────────────────────────────────

def test_paying_the_agent_itself_is_refused_on_any_chain():
    for chain_id in (MAINNET, TESTNET):
        why = unpayable_vendor(AGENT.lower(), agent=AGENT, chain_id=chain_id)
        assert why and "agent" in why


def test_a_demo_seller_is_refused_on_mainnet_only():
    demo = DEMO_SELLERS[0].address
    why = unpayable_vendor(demo, agent=AGENT, chain_id=MAINNET)
    assert why and "derivable" in why
    # On testnet the demo fleet IS the market being exercised.
    assert unpayable_vendor(demo, agent=AGENT, chain_id=TESTNET) is None


def test_an_ordinary_vendor_is_payable():
    assert unpayable_vendor(VENDOR, agent=AGENT, chain_id=MAINNET) is None
    assert unpayable_vendor(VENDOR, agent=None, chain_id=MAINNET) is None


# ── through the driver: escalated, never sent ─────────────────────────────────

class _Signer:
    address = AGENT


class _Policy:
    agent_signer = _Signer()

    def __init__(self):
        self.calls = []

    def budget(self, category):
        return {"category": category, "cap_usdc": 1_000.0, "spent_usdc": 0.0, "remaining_usdc": 100.0,
                "per_tx_limit_usdc": 50.0, "period_start": 0, "period_length": 0}

    def spend(self, category, to, amount, record):
        self.calls.append(to)
        return "0x" + "ab" * 32


def _run(vendor, tmp_path):
    catalog = {"items": [{"resource": RES, "accepts": [{"amount": "1000000", "payTo": v}]} for v in (vendor, OTHER, THIRD)]}
    receipts = [{"seller": vendor, "resource": RES, "quantity": 1_000.0, "amount_usdc": 1.0, "settled_at": NOW - 100}]
    pol = _Policy()
    d = run_obligation(
        Obligation(obligation_id="ob-1", vendor=vendor, billed_usdc=1.0, category="infra", resource=RES,
                   vendor_quantity=1_000.0),
        receipts=receipts, catalog=catalog, policy=pol, now=NOW, dry_run=False,
        log_path=str(tmp_path / "d.jsonl"),
    )
    return d, pol


def test_a_live_run_never_pays_the_agent(chain, tmp_path):
    chain(MAINNET)
    d, pol = _run(AGENT, tmp_path)
    assert d.intent == ESCALATE and d.recommended_intent == PAY
    assert pol.calls == [] and d.tx is None and d.paid_usdc == 0.0
    assert "agent" in d.rule


def test_a_live_mainnet_run_never_pays_a_derivable_demo_seller(chain, tmp_path):
    chain(MAINNET)
    d, pol = _run(DEMO_SELLERS[0].address, tmp_path)
    assert d.intent == ESCALATE and pol.calls == []
    assert "derivable" in d.rule


def test_an_ordinary_vendor_is_still_paid(chain, tmp_path):
    chain(MAINNET)
    d, pol = _run(VENDOR, tmp_path)
    assert d.intent == PAY and pol.calls == [VENDOR]


def test_a_live_run_on_a_host_without_the_agent_key_escalates(chain, tmp_path):
    # Budgets are public reads now, so a keyless host can reach PAY; it must say
    # why it cannot pay rather than fail inside spend().
    chain(5042002)

    class _Keyless(_Policy):
        def configured(self):
            return False

    pol = _Keyless()
    catalog = {"items": [{"resource": RES, "accepts": [{"amount": "1000000", "payTo": v}]} for v in (VENDOR, OTHER, THIRD)]}
    receipts = [{"seller": VENDOR, "resource": RES, "quantity": 1_000.0, "amount_usdc": 1.0, "settled_at": NOW - 100}]
    d = run_obligation(
        Obligation(obligation_id="ob-2", vendor=VENDOR, billed_usdc=1.0, category="infra", resource=RES,
                   vendor_quantity=1_000.0),
        receipts=receipts, catalog=catalog, policy=pol, now=NOW, dry_run=False, log_path=str(tmp_path / "d.jsonl"),
    )
    assert d.intent == ESCALATE and pol.calls == [] and "agent key" in d.rule
