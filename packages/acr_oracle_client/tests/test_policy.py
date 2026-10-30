"""PolicyClient — the arithmetic, and the refusals that keep a threshold real.

Hermetic: nothing here touches a chain. The claims are the ones that would be
silently wrong rather than loudly broken — an amount rounded instead of refused,
a decision hash that moves when a dict is rebuilt in another order, and an
operator holding both keys so the escalation threshold approves itself.
"""

from __future__ import annotations

import pytest
from acr_oracle_client.policy import (
    POLICY_ABI,
    USDC,
    PolicyClient,
    category_id,
    decision_hash,
    usdc_units,
)

# --- amounts ---------------------------------------------------------------

def test_whole_and_fractional_usdc_convert_exactly():
    assert usdc_units(1) == USDC
    assert usdc_units("1.5") == 1_500_000
    assert usdc_units("0.000001") == 1
    assert usdc_units(0) == 0


def test_a_float_the_caller_typed_is_not_punished_for_being_a_float():
    """Decimal(0.1) is 0.1000000000000000055…; routing through str keeps 0.1 exact."""
    assert usdc_units(0.1) == 100_000
    assert usdc_units(12.34) == 12_340_000


def test_an_amount_finer_than_six_decimals_is_refused_not_rounded():
    """Rounding here would make the agent pay a number nobody chose."""
    with pytest.raises(ValueError, match="finer than USDC's 6 decimals"):
        usdc_units("0.0000001")
    with pytest.raises(ValueError, match="finer than USDC's 6 decimals"):
        usdc_units("1.23456789")


def test_a_negative_amount_is_refused():
    with pytest.raises(ValueError, match="negative"):
        usdc_units("-1")


def test_nonsense_is_refused_as_an_amount():
    with pytest.raises(ValueError, match="not an amount"):
        usdc_units("about ten dollars")


# --- the decision commitment ----------------------------------------------

def test_the_same_record_hashes_the_same_however_the_dict_was_built():
    """The chain stores this hash. If key order moved it, a record would stop
    matching its own commitment for a reason that is not tampering."""
    a = {"billed": 1.0, "metered": 0.98, "par": 0.95, "rule": "over-par-reroute"}
    b = {"rule": "over-par-reroute", "par": 0.95, "metered": 0.98, "billed": 1.0}
    assert decision_hash(a) == decision_hash(b)


def test_changing_any_field_changes_the_hash():
    base = {"billed": 1.0, "metered": 0.98, "par": 0.95, "rule": "pay"}
    assert decision_hash(base) != decision_hash({**base, "par": 0.96})
    assert decision_hash(base) != decision_hash({**base, "rule": "hold"})
    assert decision_hash(base) != decision_hash({**base, "extra": 1})


def test_a_decision_hash_is_never_zero():
    """PolicyWallet rejects bytes32(0), so an encoding that could produce it
    would turn a valid decision into an unpayable one."""
    assert decision_hash({}) != b"\x00" * 32
    assert len(decision_hash({"a": 1})) == 32


# --- categories ------------------------------------------------------------

def test_long_category_names_stay_distinct():
    """Padding would truncate at 31 bytes and silently merge two budgets."""
    a = "model inference for the research team, europe"
    b = "model inference for the research team, america"
    assert len(a) > 31 and len(b) > 31
    assert category_id(a) != category_id(b)
    assert len(category_id(a)) == 32


def test_a_category_id_is_stable():
    assert category_id("infra") == category_id("infra")
    assert category_id("infra") != category_id("Infra")


# --- configuration refusals -----------------------------------------------

def _client(**kw) -> PolicyClient:
    return PolicyClient(rpc_url="http://127.0.0.1:1", wallet_address=None, **kw)


def test_an_unconfigured_client_reports_itself_and_refuses_to_spend():
    c = _client(agent_signer=None, owner_signer=None)
    assert c.configured() is False
    assert c.can_escalate() is False
    with pytest.raises(RuntimeError, match="not configured"):
        c.spend("infra", "0x" + "11" * 20, 1, {"rule": "pay"})


def test_reads_are_none_rather_than_a_guess_when_there_is_no_chain():
    """A budget this client cannot see must not read as a budget of zero: zero is
    a real answer that would make every payment look over-budget.

    The balance is the same claim and the sharper one. `budget()` returning 0
    makes a payment look forbidden, which stops the agent; `balance_usdc()`
    returning 0 makes the WALLET look empty, which escalates every bill to a
    person on the strength of an RPC that happened to time out.
    """
    c = _client(agent_signer=None, owner_signer=None)
    assert c.budget("infra") is None
    assert c.approval_nonce() is None
    assert c.paused() is None
    assert c.balance_usdc() is None


# --- the balance, and the 1e12 trap ---------------------------------------

class _Fn:
    """One contract function that answers a fixed value."""

    def __init__(self, value):
        self._value = value

    def call(self):
        return self._value


class _Functions:
    def __init__(self, token: str, raw: int):
        self._token, self._raw = token, raw
        self.asked: list[str] = []

    def usdc(self):
        return _Fn(self._token)

    def balanceOf(self, account):  # noqa: N802 — the ERC-20 spelling
        self.asked.append(account)
        return _Fn(self._raw)


class _Contract:
    def __init__(self, fns):
        self.functions = fns


class _Eth:
    def __init__(self, fns):
        self._fns = fns
        self.addresses: list[str] = []

    def contract(self, address=None, abi=None):
        self.addresses.append(address)
        return _Contract(self._fns)


class _W3:
    """Just enough web3 to answer two view calls and nothing else."""

    def __init__(self, token: str, raw: int):
        self.eth = _Eth(_Functions(token, raw))

    @staticmethod
    def to_checksum_address(a):
        # The REAL normaliser, not a passthrough. `_contract()` checksums via
        # web3 directly while `balance_usdc` goes through `w3`, and a fake that
        # returned the string unchanged would make the two disagree in the test
        # and nowhere else — hiding exactly the mismatch a reader would look
        # here to rule out.
        from web3 import Web3

        return Web3.to_checksum_address(a)


TOKEN = "0x3600000000000000000000000000000000000000"


def _reader(raw_units: int) -> PolicyClient:
    c = PolicyClient(
        rpc_url="http://127.0.0.1:1",
        wallet_address="0x" + "cc" * 20,
        agent_signer=object(),
    )
    c._w3 = _W3(TOKEN, raw_units)
    return c


def test_the_balance_is_the_six_decimal_erc20_view_and_not_the_native_eighteen():
    """THE ONE NUMBER THAT MUST BE RIGHT.

    On Arc, USDC is the native gas token at 18 decimals AND an ERC-20 at
    `0x3600…` at 6. `PolicyWallet` holds and `transfer`s the ERC-20, so that is
    the pile `spend` can move; `eth_getBalance` would answer about the other
    one. `PolicyWallet.sol`'s own comment states the stakes: "Mixing them is a
    1e12 error that looks like a fat finger."

    A wallet holding 1.5 USDC reads 1.5. Read through the 18-decimal view the
    same wallet would read 1,500,000, and every bill on earth would look
    affordable.
    """
    assert _reader(1_500_000).balance_usdc() == pytest.approx(1.5)
    assert _reader(1).balance_usdc() == pytest.approx(1 / USDC)
    assert _reader(0).balance_usdc() == 0.0
    # The guard sentence: 1.5 USDC must not read as a number in the millions.
    assert _reader(1_500_000).balance_usdc() < 2.0


def test_a_chain_that_refuses_gives_none_and_never_a_zero_balance():
    """The branch that decides what an operator does on a Monday morning.

    Zero is a real answer: it means the wallet is empty and every dated bill
    should go to a person. A node that hung up means nothing of the kind, and
    the two must not arrive at the ladder as the same float."""

    class _Angry:
        def __init__(self):
            self.eth = self

        def contract(self, address=None, abi=None):
            raise RuntimeError("no contract at this address on this chain")

        @staticmethod
        def to_checksum_address(a):
            from web3 import Web3

            return Web3.to_checksum_address(a)

    c = PolicyClient(
        rpc_url="http://127.0.0.1:1",
        wallet_address="0x" + "cc" * 20,
        agent_signer=object(),
    )
    c._w3 = _Angry()
    assert c.balance_usdc() is None


def test_the_token_address_comes_from_the_wallet_not_from_configuration():
    """The wallet names the token it can actually move, so the balance cannot
    drift from what `spend` transfers — and no USDC address is hardcoded in this
    client for an operator to get wrong on a second chain."""
    from web3 import Web3

    c = _reader(2_000_000)
    wallet = Web3.to_checksum_address(c.wallet_address)
    assert c.balance_usdc() == pytest.approx(2.0)
    # Two contracts built: the wallet at its own address, then the token at the
    # address the wallet named. TOKEN has no letters, so its checksum form is
    # itself — which is why that address can be written as a plain literal here
    # and still be the thing web3 would accept in production, where `usdc()`
    # comes back already checksummed from the ABI decoder.
    assert c._w3.eth.addresses == [wallet, TOKEN]
    # And the balance asked about is the WALLET's, not the token's or ours.
    assert c._w3.eth._fns.asked == [wallet]


class _Sig:
    """Minimal Signer stand-in: an address is all these refusals examine."""

    def __init__(self, address: str) -> None:
        self._a = address

    @property
    def address(self) -> str:
        return self._a

    def sign_typed_data(self, domain, types, message, primary_type):  # pragma: no cover
        raise AssertionError("must not be reached: the refusal comes first")

    def send_transaction(self, w3, tx):  # pragma: no cover
        raise AssertionError("must not be reached: the refusal comes first")


SAME = "0x" + "aa" * 20
OTHER = "0x" + "bb" * 20


def test_one_key_holding_both_roles_cannot_approve_its_own_payment():
    """The whole point of the threshold is that a second party signs. An operator
    that is also the owner has no threshold, and should say so rather than
    produce an approval that looks like oversight."""
    c = PolicyClient(
        rpc_url="http://127.0.0.1:1",
        wallet_address="0x" + "cc" * 20,
        agent_signer=_Sig(SAME),
        owner_signer=_Sig(SAME),
    )
    with pytest.raises(RuntimeError, match="same key"):
        c.spend_approved("infra", OTHER, 500, {"rule": "pay"}, deadline=1)


def test_with_no_owner_key_a_large_payment_goes_to_a_human():
    c = PolicyClient(
        rpc_url="http://127.0.0.1:1",
        wallet_address="0x" + "cc" * 20,
        agent_signer=_Sig(SAME),
        owner_signer=None,
    )
    assert c.configured() is True, "it still runs; it just cannot clear large payments"
    assert c.can_escalate() is False
    with pytest.raises(RuntimeError, match="must go to a human"):
        c.spend_approved("infra", OTHER, 500, {"rule": "pay"}, deadline=1)


def test_the_wallet_address_actually_arrives_from_the_environment(monkeypatch):
    """A settings field read through a getattr default swallows its own env var:
    the client reports unconfigured while the operator looks wired. conftest
    strips every ACR_* var, so set it and rebuild the settings."""
    from acr_core import reset_settings

    addr = "0x" + "11" * 20
    monkeypatch.setenv("ACR_POLICY_WALLET_ADDRESS", addr)
    reset_settings()
    try:
        c = PolicyClient(agent_signer=None, owner_signer=None)
        assert c.wallet_address == addr
    finally:
        reset_settings()


# --- the owner's own wallet ------------------------------------------------

def test_the_owner_path_refuses_when_one_key_holds_both_roles():
    """Same refusal as spend_approved, for the same reason: an operator that is
    also the owner has no threshold, and should say so rather than pay itself."""
    c = PolicyClient(
        rpc_url="http://127.0.0.1:1",
        wallet_address="0x" + "cc" * 20,
        agent_signer=_Sig(SAME),
        owner_signer=_Sig(SAME),
    )
    with pytest.raises(RuntimeError, match="same key"):
        c.spend_as_owner("infra", OTHER, 500, {"rule": "pay"})


def test_the_owner_path_refuses_with_no_owner_key():
    c = PolicyClient(
        rpc_url="http://127.0.0.1:1",
        wallet_address="0x" + "cc" * 20,
        agent_signer=_Sig(SAME),
        owner_signer=None,
    )
    with pytest.raises(RuntimeError, match="must go to a human"):
        c.spend_as_owner("infra", OTHER, 500, {"rule": "pay"})


def test_the_owner_path_refuses_when_unconfigured():
    c = _client(agent_signer=None, owner_signer=None)
    with pytest.raises(RuntimeError, match="not configured"):
        c.spend_as_owner("infra", OTHER, 1, {"rule": "pay"})


def test_every_contract_call_the_client_makes_is_declared_in_its_abi():
    """The ABI must carry every function the client actually calls.

    `balance_usdc()` reads the token address from the wallet's own `usdc()`
    getter — deliberately, so the figure cannot drift from what `spend`
    transfers. That getter was never declared in POLICY_ABI, so every live call
    raised `The function 'usdc' was not found in this contract's abi` and the
    cash rung reported "not measured" against a wallet holding 20 USDC on Arc
    testnet.

    The existing balance tests could not catch it: they stub web3, so the fake
    answers a call the real ABI cannot even build. `test_a_chain_that_refuses…`
    asserts `balance_usdc() is None`, which passed for the wrong reason — a
    missing ABI entry looks exactly like an unreachable node.

    So this checks the ABI against the call sites instead of against a mock.
    """
    declared = {e["name"] for e in POLICY_ABI if e.get("type") == "function"}

    # Only calls made against the POLICY contract, i.e. through `self._contract()`.
    # `erc20.functions.balanceOf(...)` targets the USDC token with its own ABI and
    # is correctly absent from this one — which is why the pattern is anchored on
    # `_contract()` rather than on any `functions.` call.
    import re
    from pathlib import Path

    src = Path(__file__).resolve().parents[1] / "acr_oracle_client" / "policy.py"
    called = set(
        re.findall(r"_contract\(\)\.functions\.([A-Za-z_][A-Za-z0-9_]*)\(", src.read_text())
    )
    assert called, "the pattern matched nothing; the call style changed"

    missing = sorted(called - declared)
    assert not missing, (
        f"policy.py calls {missing} but POLICY_ABI does not declare them, so the "
        "call raises at runtime and looks like an unreachable chain"
    )
    assert "usdc" in declared, "the getter balance_usdc() depends on"


@pytest.mark.parametrize("amount", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_amount_is_refused(amount):
    with pytest.raises(ValueError, match="finite"):
        usdc_units(amount)


def test_large_exact_amount_never_uses_decimal_context_rounding():
    assert usdc_units("123456789012345678901234567890.123456") == 123456789012345678901234567890123456
    with pytest.raises(ValueError, match="6 decimals"):
        usdc_units("123456789012345678901234567890.1234567")
