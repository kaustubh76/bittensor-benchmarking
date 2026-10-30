"""Circle webhook receiver — ACK/record + ECDSA P-256 signature verification."""

from __future__ import annotations

import base64
import json

import pytest
from fastapi.testclient import TestClient
from index_api import webhooks
from index_api.app import app
from index_api.webhooks import WebhookStore, get_webhook_store, reset_webhook_store

client = TestClient(app)


@pytest.fixture(autouse=True)
def _fresh_store(monkeypatch):
    # In-memory only by default — never touch the real data/webhook_events.jsonl.
    monkeypatch.setattr(webhooks.get_settings(), "webhook_log_path", "")
    reset_webhook_store()
    yield
    reset_webhook_store()


def test_webhook_acks_and_records():
    body = {"notificationType": "webhooks.test", "subscriptionId": "sub_1", "notification": {}}
    r = client.post("/webhooks/circle", json=body)
    assert r.status_code == 200
    assert r.json() == {"received": True}

    recent = client.get("/webhooks/recent").json()
    assert recent["received"] == 1
    ev = recent["events"][-1]
    assert ev["type"] == "webhooks.test"
    assert ev["subscription_id"] == "sub_1"
    # No signature headers → unverifiable, recorded as such but still ACKed.
    assert ev["verified"] is None


def test_webhook_non_json_body_still_acks():
    r = client.post(
        "/webhooks/circle", content=b"not json", headers={"content-type": "text/plain"}
    )
    assert r.status_code == 200
    assert client.get("/webhooks/recent").json()["received"] == 1


def test_webhook_signature_roundtrip(monkeypatch):
    crypto = pytest.importorskip("cryptography")  # noqa: F841
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    priv = ec.generate_private_key(ec.SECP256R1())
    der = priv.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    # Pin the public key so verification runs fully offline (no Circle fetch).
    from index_api import webhooks

    monkeypatch.setattr(
        webhooks.get_settings(), "circle_webhook_public_key", base64.b64encode(der).decode()
    )
    reset_webhook_store()

    body = json.dumps({"notificationType": "transactions.outbound", "subscriptionId": "s"}).encode()
    sig = priv.sign(body, ec.ECDSA(hashes.SHA256()))
    headers = {
        "X-Circle-Signature": base64.b64encode(sig).decode(),
        "X-Circle-Key-Id": "key-1",
        "content-type": "application/json",
    }
    r = client.post("/webhooks/circle", content=body, headers=headers)
    assert r.status_code == 200
    assert get_webhook_store().recent()[-1]["verified"] is True

    # A tampered body fails verification but is still ACKed (never retried).
    reset_webhook_store()
    monkeypatch.setattr(
        webhooks.get_settings(), "circle_webhook_public_key", base64.b64encode(der).decode()
    )
    bad = client.post("/webhooks/circle", content=b'{"notificationType":"forged"}', headers=headers)
    assert bad.status_code == 200
    assert get_webhook_store().recent()[-1]["verified"] is False


def test_webhook_persists_and_rehydrates(tmp_path, monkeypatch):
    """Events append to the JSONL log and a fresh store reloads them (restart)."""
    logfile = tmp_path / "webhook_events.jsonl"
    monkeypatch.setattr(webhooks.get_settings(), "webhook_log_path", str(logfile))
    reset_webhook_store()

    for i in range(2):
        client.post(
            "/webhooks/circle",
            json={"notificationType": "transactions.outbound", "subscriptionId": f"s{i}"},
        )

    # (a) two JSONL lines written and parseable
    lines = logfile.read_text().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "transactions.outbound"

    # (b) a brand-new store reading the same file rehydrates both events
    revived = WebhookStore(log_path=str(logfile))
    assert revived.received == 2
    assert revived.recent()[-1]["subscription_id"] == "s1"


def test_webhook_emits_info_log(caplog):
    with caplog.at_level("INFO", logger="index_api.webhooks"):
        client.post(
            "/webhooks/circle",
            json={"notificationType": "transactions.inbound", "subscriptionId": "s"},
        )
    assert any("circle webhook · transactions.inbound" in r.message for r in caplog.records)
