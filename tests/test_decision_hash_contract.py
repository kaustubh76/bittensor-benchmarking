"""The decision hash is a contract with another company. Pin it like one.

WHY THIS FILE EXISTS. The Warns integration commits an on-chain payment to a
decision they have signed, and `PolicyWallet` makes that cheap: `decisionHash`
is a `bytes32` **parameter** on all three spend paths and the only on-chain rule
is `require(decisionHash != bytes32(0))`. The contract never hashes anything.
So the encoding in `acr_oracle_client.policy.decision_hash` is the whole
agreement, and it lives entirely in our Python.

THE CONFLICT THIS PREVENTS, measured 2026-10-12 rather than predicted:

    python  {"at":1791801414.0,"metered_quantity":10.0,...}
    node    {"at":1791801414,  "metered_quantity":10,  ...}

Python renders a whole-valued float with its `.0` and JavaScript does not, and
**from JSON alone a reimplementation cannot tell which fields are floats**. Four
hashed fields hold whole floats in the live archive today. A TypeScript port
written in good faith would therefore produce silently different hashes, and the
symptom would be "the commitment does not match" with nothing to point at.

The agreed shape is that **Warns signs the hash we compute** — one
implementation, no cross-language canonicalisation to keep in step. These tests
hold that agreement to its word in three ways: the historical rows still verify,
the encoding still produces the bytes we published, and nobody has quietly added
a second implementation.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from acr_oracle_client.policy import decision_hash
from index_api.operator import UNHASHED, ObligationDecision, hashable_record

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "tests" / "fixtures" / "decision_hash_vectors.json"
ARCHIVE = ROOT / "services" / "index_api" / "index_api" / "operator_decisions.jsonl"

#: One real archived decision that already carries an on-chain payment, and the
#: hash it produced BEFORE `ObligationDecision` gained the Warns fields.
#:
#: FROZEN ON PURPOSE. Adding a dataclass field changes `asdict()`, so every NEW
#: record hashes differently — intended, and harmless. What must never change is
#: re-derivation from an OLD row, because six payments on Arc testnet committed
#: to hashes derived this way and a record that no longer matches its own
#: commitment is indistinguishable from one that was edited after the fact.
#: `hashable_record` filters a dict that never had the new keys, which is the
#: reason this holds; this test is what turns that reasoning into evidence.
FROZEN_OBLIGATION = "acr-fleet:0x0721821F:compute-acr-seller-data-small"
FROZEN_HASH = "0xc7f72a0956819222971a89968b5a7d019fbbbd71a8ffde53ebf91359eb13f0df"


def _archived_rows() -> list[dict]:
    if not ARCHIVE.exists():
        pytest.skip("no archived decisions in this checkout")
    return [json.loads(line) for line in ARCHIVE.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_an_already_committed_decision_still_derives_its_original_hash():
    """The migration's whole safety argument, as a measurement."""
    rows = [r for r in _archived_rows() if r.get("obligation_id") == FROZEN_OBLIGATION]
    assert rows, f"the frozen row {FROZEN_OBLIGATION} is gone from the archive"

    got = "0x" + decision_hash(hashable_record(rows[0])).hex()
    assert got == FROZEN_HASH, (
        "an archived decision no longer hashes to the value that was committed on "
        f"chain.\n  expected {FROZEN_HASH}\n  got      {got}\n"
        "Either the encoding in `decision_hash` moved, or a field was added to the "
        "archived ROW rather than to `ObligationDecision`. A new DATACLASS field is "
        "safe (old rows never had the key); a new field written into the stored rows "
        "is not, and it silently invalidates every commitment already on chain."
    )


def test_the_published_vectors_still_describe_the_encoding():
    """Warns holds these bytes. If this fails, the agreement changed."""
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    for v in doc["vectors"]:
        got = "0x" + decision_hash(v["record"]).hex()
        assert got == v["hash"], (
            f"vector {v['name']!r} changed: expected {v['hash']}, got {got}.\n"
            f"That vector exists because: {v['why']}\n"
            "These are published to an integration partner. Changing the encoding "
            "is a breaking change to a contract with another company, not a refactor."
        )


def test_the_vectors_name_the_unhashed_fields_the_code_actually_strips():
    """A partner reading the fixture must not be told a different exclusion set
    from the one `as_record` applies. Three places already have to agree about
    `UNHASHED`; the fixture is the fourth, and it is the one outside this repo's
    control once it has been sent."""
    doc = json.loads(VECTORS.read_text(encoding="utf-8"))
    assert sorted(doc["unhashed_fields"]) == sorted(UNHASHED), (
        f"the fixture says {sorted(doc['unhashed_fields'])} is excluded from the hash "
        f"but the code excludes {sorted(UNHASHED)}"
    )


def test_the_warns_fields_sit_on_the_correct_side_of_the_hash():
    """`decisionId` inside, `signature` outside, and neither is an accident.

    The id belongs in the hash: the on-chain commitment should name which Warns
    decision authorised the payment. The signature cannot be in the hash — it
    signs the hash, so a signature inside the record it signs is self-referential
    and simply cannot be produced.
    """
    fields = set(ObligationDecision.__dataclass_fields__)
    assert "warns_decision_id" in fields
    assert "warns_signature" in fields

    d = ObligationDecision(
        at=1.0, obligation_id="o", vendor="0x" + "ab" * 20, category="infra",
        billed_usdc=1.0, intent="pay", rule="r",
        warns_decision_id="warns-123", warns_signature="0xdeadbeef",
    )
    rec = d.as_record()
    assert rec.get("warns_decision_id") == "warns-123", (
        "the Warns decision id is not in the hashed record, so the chain would "
        "commit to a payment without naming the decision that authorised it"
    )
    assert "warns_signature" not in rec, (
        "the Warns signature is inside the hashed record. It signs that hash, so "
        "this is circular and unproducible — it belongs in UNHASHED"
    )
    # And the id genuinely moves the hash, or putting it there bought nothing.
    other = ObligationDecision(
        at=1.0, obligation_id="o", vendor="0x" + "ab" * 20, category="infra",
        billed_usdc=1.0, intent="pay", rule="r", warns_decision_id="warns-456",
    )
    assert decision_hash(rec) != decision_hash(other.as_record())


def test_nobody_has_reimplemented_the_hash_in_another_language():
    """The port that would break the integration, gated.

    Python renders a whole-valued float as `10.0` and JavaScript as `10`, and a
    reimplementation reading JSON cannot know which fields are floats. So a
    TypeScript or Solidity version of this encoding is not a convenience, it is
    a second answer to a question that must have one.

    The supported move is to call the Python helper, or to ask ACR for the hash
    and sign the 32 bytes — which is exactly what the Warns integration does.
    """
    offenders = []
    for pattern in ("*.ts", "*.tsx", "*.sol", "*.js", "*.go", "*.rs"):
        for path in ROOT.rglob(pattern):
            parts = set(path.parts)
            if parts & {"node_modules", ".venv", "dist", "out", "build", ".next", "generated"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if "acr.decision::" in text:
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == [], (
        "these files implement the `acr.decision::` hash outside Python:\n  "
        + "\n  ".join(offenders)
        + "\nPython writes a whole-valued float as `10.0` and JavaScript writes `10`, "
        "and from JSON alone neither can tell which fields are floats — so this port "
        "will produce different hashes for real records and the failure is silent. "
        "Call `acr_oracle_client.policy.decision_hash`, or sign the 32 bytes ACR "
        "hands you. See tests/fixtures/decision_hash_vectors.json."
    )
