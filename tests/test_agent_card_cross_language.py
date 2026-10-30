"""A card signed in TypeScript must verify in Python.

THE CARD CROSSES A LANGUAGE BOUNDARY AND NOTHING ELSE CHECKS IT. The buyer agent
mints cards with viem (`apps/agent/src/card.ts`); the gate verifies them with
`eth_account` (`agentgate.py` → `agentcard.recover_agent`). Two independent EIP-712
implementations have to agree on a digest built from a domain that deliberately
OMITS `verifyingContract` — the one part of the encoding where implementations are
most likely to differ, because each has to infer the `EIP712Domain` type from the
fields actually present rather than from a fixed list.

A golden vector rather than a live round trip: running tsx from pytest would make
this test depend on node being installed and on npm state, and a cross-language
check that skips when a toolchain is missing is a check that silently stops
running. The vector below was produced by `apps/agent/src/card.ts` and is verified
here on every push.

IT ASSERTS THE SIGNATURE, NOT THE GATE. Cards expire within 15 minutes by design,
so a frozen vector is permanently expired and `AgentGate.verify` would correctly
refuse it. Expiry is already covered in `test_agentgate.py`; what only this file
can establish is that the two languages hash the same bytes.
"""

from __future__ import annotations

from acr_oracle_client.agentcard import decode_header, recover_agent

CHAIN = 5042002

#: Minted by `apps/agent/src/card.ts` with the private key 0x11…11 (32 bytes of
#: 0x11 — a throwaway test key, never used anywhere else). Regenerate only if the
#: card struct or the domain changes, and treat a mismatch here as the TypeScript
#: and Python encoders having diverged rather than as a stale fixture.
TS_HEADER = (
    "eyJjYXJkIjp7ImFnZW50IjoiMHgxOUU3RTM3NkU3QzIxM0I3RTdlN2U0NmNjNzBBNWREMDg2REFmZjJBIiwiYXVkaWVuY2Ui"
    "OiJhY3ItaW5kZXgtYXBpIiwiZXhwaXJlc19hdCI6MTc4OTIwMzY4OSwiaHVtYW5fY2x1c3RlciI6IjB4MDAwMDAwMDAwMDAw"
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMCIsImlzc3VlZF9hdCI6MTc4OTIw"
    "MzM4OSwibmFtZSI6ImFjci1idXllci1hZ2VudCIsInJvbGUiOiJ0YWtlciIsInNjb3BlX2hhc2giOiIweDAwMDAwMDAwMDAw"
    "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAifSwic2lnbmF0dXJlIjoiMHhm"
    "ODY4YmE4ZmMyMDE3MmFkMzc2NTZhM2VlZTJmNzljMmE2ZWU5ZmViNzZlYjk3YzRlNmY0MmVjYzFlZTI0ZWQyNjQ2YmRiMDFi"
    "Yjg5N2YxMzRkMTI0OTYxMWQ1MTQ5ZmI0NWZkYWRiZDk2ZWU0M2JkYjY5ZTcwNTFkYWI5YjZjODFiIn0="
)

#: The address 0x11…11 derives to. Written out rather than recomputed so a change
#: in key derivation cannot quietly move both sides of the comparison together.
EXPECTED_AGENT = "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"


def test_a_viem_signed_card_recovers_to_the_right_agent_in_python():
    """The assertion the whole file exists for: viem's digest equals eth_account's
    over a 3-field domain."""
    card, signature = decode_header(TS_HEADER)
    assert card.agent == EXPECTED_AGENT
    assert recover_agent(card, signature, CHAIN).lower() == EXPECTED_AGENT.lower()


def test_the_typescript_encoder_uses_the_field_names_the_gate_reads():
    """`to_json` is snake_case and the struct is camelCase. Two encoders that
    disagree about key ORDER still verify; two that disagree about NAMES do not, so
    the names are what this pins."""
    card, _ = decode_header(TS_HEADER)
    assert card.role == "taker"
    assert card.audience == "acr-index-api"
    assert card.name == "acr-buyer-agent"
    # An agent that is not resolved to a human must claim nothing: a claim the
    # chain cannot confirm is a refusal, not a downgrade.
    assert card.claims_human is False
    assert card.ttl_s == 300


def test_a_tampered_vector_recovers_to_somebody_else():
    """The control. Without it this file would also pass if `recover_agent` had
    been quietly reduced to returning `card.agent`."""
    card, signature = decode_header(TS_HEADER)
    forged = card.__class__(**{**card.to_json(), "role": "owner"})
    assert recover_agent(forged, signature, CHAIN).lower() != EXPECTED_AGENT.lower()
