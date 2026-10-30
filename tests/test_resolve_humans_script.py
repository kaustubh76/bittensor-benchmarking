"""`make resolve-humans` fails in one sentence when Circle cannot serve the chain.

On 2026-10-16 the owner ran `make resolve-humans ARGS=--commit` on testnet and got
sixty lines of SDK traceback ending in a 403. The `.env` held a LIVE key for a
testnet chain. These drive `main()` with a real `CircleWalletSigner` over a fake
Circle client, so the path is the script's own, end to end, with no network.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from acr_oracle_client.signer import CircleWalletSigner

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("resolve_humans", ROOT / "scripts" / "resolve_humans.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ForbiddenException(Exception):
    status = 403


class _Circle:
    """A Circle client that refuses every call, and counts them."""

    def __init__(self):
        self.calls = 0

    def wallet_address(self, wallet_id):
        self.calls += 1
        raise ForbiddenException('(403)\nReason: Forbidden\nHTTP response body: {"code":3}')


def _settings(api_key: str, chain_id: int = 5042002):
    return SimpleNamespace(
        humanid_mirror_address="0x7f41faA38F35F1FABfc76Df5B1618fC8d0c0d8e5",
        humanid_salt="0x" + "22" * 32,
        circle_api_key=api_key,
        arc_chain_id=chain_id,
    )


def _run(monkeypatch, capsys, api_key: str):
    rh = _load()
    circle = _Circle()
    signer = CircleWalletSigner(wallet_id="52251d34-9575-5bab", client=circle)

    class Client:
        def __init__(self, settings):
            self.signer = signer
            self.rpc_url = "http://rpc.test"
            self.mirror_address = settings.humanid_mirror_address

        def chain_window(self):
            return 2963

        def signer_authorized(self):
            _ = self.signer.address  # what the real client does first
            return True

    monkeypatch.setattr(rh, "get_settings", lambda: _settings(api_key))
    monkeypatch.setattr(rh, "HumanIdMirrorClient", lambda settings: Client(settings))
    monkeypatch.setattr(sys, "argv", ["resolve_humans.py", "--commit"])
    rc = rh.main()
    return rc, capsys.readouterr().out, circle


def test_a_live_key_on_testnet_is_named_before_circle_is_asked(monkeypatch, capsys):
    rc, out, circle = _run(monkeypatch, capsys, "LIVE_API_KEY:abc:def")
    assert rc == 1
    assert "LIVE_API_KEY" in out and "TEST_API_KEY" in out and "5042002" in out
    assert circle.calls == 0, "the mismatch is in the prefix; Circle need not be asked"
    assert "Traceback" not in out and "abc" not in out


def test_a_refused_key_of_the_right_kind_is_one_line(monkeypatch, capsys):
    rc, out, circle = _run(monkeypatch, capsys, "TEST_API_KEY:abc:def")
    assert rc == 1
    assert circle.calls == 1
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert len(lines) == 1 and "Circle refused the API key (HTTP 403)" in lines[0]
    assert "revoked" in lines[0]


@pytest.mark.parametrize("api_key", ["TEST_API_KEY:abc:def"])
def test_the_shared_core_is_what_the_script_runs(api_key):
    src = (ROOT / "scripts" / "resolve_humans.py").read_text(encoding="utf-8")
    # One loop, in the package the press image carries; the script only prints.
    assert "resolve_window(" in src and "roster_pairs(" in src
    assert "client.record(" not in src, "the record loop is back in the script"
