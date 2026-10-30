#!/usr/bin/env python
"""Verify the Arc-testnet deploy — the read-only preflight for the live loop.

Loads settings from ``.env`` (``acr_core.get_settings``), connects to the Arc
RPC, and checks — with friendly ✓/✗ output and **no writes**:

  * the RPC's chain id matches ``ACR_ARC_CHAIN_ID``,
  * bytecode exists at ``ACR_ORACLE_ADDRESS`` and ``ACR_REGISTRY_ADDRESS``,
  * the poster signer ``isSigner()`` on the oracle — the raw
    ``ACR_POSTER_PRIVATE_KEY`` EOA, or (key blank) the Circle custody wallet,
  * ``latestPrint`` reads back per index (tolerating "no print" — a fresh
    deploy has none until ``make post-once``).

Prints arcscan URLs for both contracts and (best-effort) the most recent
``PricePosted`` tx. Exit codes: 0 = everything verified; 1 = a check failed —
including "no oracle configured", because an unconfigured ``.env`` means the
deploy hasn't happened yet, so a preflight gate must not pass.

    uv run python scripts/verify_deploy.py        # or: make verify-testnet
"""

from __future__ import annotations

import os
import sys

from acr_core import ALL_INDEX_IDS, get_settings
from acr_oracle_client.client import ORACLE_ABI, WAD, index_id_to_bytes32

FALLBACK_EXPLORER = "https://explorer.arc.io"  # Arc mainnet; the chain profile overrides it

# `isSigner(address)` isn't part of the client's post/read ABI — add it here.
IS_SIGNER_ABI = {
    "type": "function",
    "name": "isSigner",
    "stateMutability": "view",
    "inputs": [{"name": "", "type": "address"}],
    "outputs": [{"name": "", "type": "bool"}],
}
PRICE_POSTED_SIG = "PricePosted(bytes32,uint256,uint256,uint256,uint256,uint64,address)"

OWNER_ABI = [
    {"type": "function", "name": "owner", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    {"type": "function", "name": "pendingOwner", "stateMutability": "view", "inputs": [],
     "outputs": [{"name": "", "type": "address"}]},
    IS_SIGNER_ABI,
]
ZERO = "0x0000000000000000000000000000000000000000"
MAINNET_CHAIN_ID = 5042


def _deployer_address() -> str | None:
    """The deploy key's ADDRESS, never the key. `ACR_DEPLOYER_ADDRESS` wins; else it is
    derived from `DEPLOYER_PRIVATE_KEY` in-process and the key is discarded."""
    addr = os.environ.get("ACR_DEPLOYER_ADDRESS", "").strip()
    if addr:
        return addr
    key = os.environ.get("DEPLOYER_PRIVATE_KEY", "").strip()
    if not key:
        return None
    try:
        from eth_account import Account

        return Account.from_key(key).address
    except Exception:
        return None


def _custody(w3, s, hard: bool) -> bool:
    """④ Who owns the contracts, and who may sign the price.

    The pre-mainnet audit (docs/SECURITY-AUDIT.md, C1) read the live chain and found
    the DEPLOY KEY owning five contracts and signing both oracles. On mainnet that is
    the whole risk, so there it is a hard failure; on testnet it is reported as ⚠ so
    the preflight keeps passing while the operator fixes the rehearsal too.

    Checks, per contract that is configured:
      * `pendingOwner()` is zero — no half-finished ownership transfer;
      * `owner()` equals `ACR_EXPECTED_OWNER` when that is set (the multisig, or the
        custody wallet until one exists);
      * on both oracles: the deploy key is NOT a signer, and the expected signer is.
    """
    from web3 import Web3

    mark = "✗" if hard else "⚠"
    okk = True

    def rep(cond: bool, label: str) -> bool:
        nonlocal okk
        if hard:
            return _check(cond, label)
        print(f"  {'✓' if cond else mark} {label}")
        return True

    expected_owner = os.environ.get("ACR_EXPECTED_OWNER", "").strip()
    contracts = [
        ("oracle", s.oracle_address, True),
        ("oracle v2", getattr(s, "oracle_v2_address", ""), True),
        ("futures venue", getattr(s, "futures_address", ""), False),
        ("feed-access attestor", getattr(s, "attestor_address", ""), False),
        ("receipt mirror", getattr(s, "receipt_mirror_address", ""), False),
        ("human-id mirror", getattr(s, "humanid_mirror_address", ""), False),
    ]
    deployer = _deployer_address()
    signer_addr, _ = _resolve_signer_address(s)
    owners: dict[str, str] = {}
    for label, addr, is_oracle in contracts:
        if not addr:
            continue
        c = w3.eth.contract(address=Web3.to_checksum_address(addr), abi=OWNER_ABI)
        try:
            owner = c.functions.owner().call()
            pending = c.functions.pendingOwner().call()
        except Exception:
            okk &= rep(False, f"{label}: owner()/pendingOwner() unreadable at {addr[:10]}… — "
                             "no such contract on this chain? (a testnet address left in .env)")
            continue
        owners[label] = owner
        okk &= rep(pending == ZERO, f"{label}: no pending ownership transfer")
        if expected_owner:
            okk &= rep(owner.lower() == expected_owner.lower(),
                       f"{label}: owner is ACR_EXPECTED_OWNER ({owner[:10]}…)")
        else:
            print(f"    {label}: owner {owner}  (set ACR_EXPECTED_OWNER to assert it)")
        if deployer:
            okk &= rep(owner.lower() != deployer.lower(),
                       f"{label}: owner is NOT the deploy key")
        if is_oracle:
            if deployer:
                try:
                    dep_signs = bool(c.functions.isSigner(
                        Web3.to_checksum_address(deployer)).call())
                except Exception:
                    dep_signs = True
                okk &= rep(not dep_signs, f"{label}: the deploy key is NOT a signer")
            if signer_addr:
                try:
                    press = bool(c.functions.isSigner(
                        Web3.to_checksum_address(signer_addr)).call())
                except Exception:
                    press = False
                okk &= rep(press, f"{label}: the press wallet IS a signer")
    if not deployer:
        print("    (deploy key not identifiable — set ACR_DEPLOYER_ADDRESS to assert it "
              "is neither owner nor signer)")
    distinct = {o.lower() for o in owners.values()}
    if len(distinct) > 1:
        print(f"    note: {len(distinct)} distinct owners across contracts")
    return okk


def _explorer(settings) -> str:
    """ACR_EXPLORER_BASE (settings field or raw env) with the arcscan fallback."""
    base = getattr(settings, "explorer_base", "") or os.environ.get("ACR_EXPLORER_BASE", "")
    return (base or FALLBACK_EXPLORER).rstrip("/")


def _check(ok: bool, label: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {label}")
    return ok


def _resolve_signer_address(settings) -> tuple[str | None, str]:
    """The address that will actually sign prints — custody-aware.

    A raw ``ACR_POSTER_PRIVATE_KEY`` signs as its own EOA (the default). With the
    key blank, prints are signed by the Circle developer-controlled wallet, whose
    on-chain address is a live Circle lookup via ``build_signer(...).address``.
    Returns ``(checksummed_address | None, label)`` — mirroring the same signer
    selection the poster uses, so the preflight checks the address that will post.
    """
    key = settings.poster_private_key or ""
    if key and not key.lstrip().startswith("#"):
        from eth_account import Account

        return Account.from_key(key).address, "poster (raw key)"
    # Circle custody path — never fails the whole preflight on a lookup hiccup.
    try:
        from acr_oracle_client import build_signer

        signer = build_signer(settings)
        if signer is not None:
            return signer.address, "poster (Circle custody)"
    except Exception as exc:  # pragma: no cover - needs live Circle creds
        print(f"  (Circle signer address lookup failed: {exc})")
    return None, ""


def main() -> None:
    s = get_settings()
    if not s.oracle_address:
        print(
            "\n  no oracle configured — set ACR_ORACLE_ADDRESS in .env; "
            "run the deploy first (make deploy-testnet).\n"
        )
        sys.exit(1)

    try:
        from web3 import Web3

        w3 = Web3(Web3.HTTPProvider(s.arc_rpc_url, request_kwargs={"timeout": 10}))
        if not w3.is_connected():
            raise ConnectionError
    except Exception:
        print(f"\n  ✗ RPC unreachable at {s.arc_rpc_url} — check ACR_ARC_RPC_URL.\n")
        sys.exit(1)

    explorer = _explorer(s)
    print(f"\n  verifying the ACR deploy via {s.arc_rpc_url}\n")
    ok = True

    # ① chain id
    chain_id = w3.eth.chain_id
    ok &= _check(
        chain_id == s.arc_chain_id,
        f"chain id {chain_id} matches ACR_ARC_CHAIN_ID {s.arc_chain_id}",
    )

    # ② bytecode at both addresses
    oracle = Web3.to_checksum_address(s.oracle_address)
    ok &= _check(len(w3.eth.get_code(oracle)) > 0, f"bytecode at oracle    {oracle}")
    registry = None
    if s.registry_address:
        registry = Web3.to_checksum_address(s.registry_address)
        ok &= _check(len(w3.eth.get_code(registry)) > 0, f"bytecode at registry  {registry}")
    else:
        ok &= _check(False, "ACR_REGISTRY_ADDRESS not set in .env")

    # ③ the poster signer is an authorized oracle signer (raw key OR Circle custody)
    contract = w3.eth.contract(address=oracle, abi=[*ORACLE_ABI, IS_SIGNER_ABI])
    signer_addr, signer_label = _resolve_signer_address(s)
    if signer_addr:
        signer_addr = Web3.to_checksum_address(signer_addr)
        try:
            allowed = bool(contract.functions.isSigner(signer_addr).call())
        except Exception:
            allowed = False
        hint = "" if allowed else "  (cast send setSigner — docs/TESTNET_RUNBOOK.md step 3)"
        ok &= _check(allowed, f"{signer_label} {signer_addr} isSigner() on the oracle{hint}")
    else:
        ok &= _check(
            False,
            "no poster signer configured — set ACR_POSTER_PRIVATE_KEY, or Circle "
            "custody creds (ACR_CIRCLE_API_KEY + ACR_CIRCLE_WALLET_ID)",
        )

    # ④ latest print per index — "no print yet" is expected on a fresh deploy
    print(f"\n  {'INDEX':<9} {'on-chain latest':>16}")
    print("  " + "-" * 56)
    any_print = False
    for iid in ALL_INDEX_IDS:
        try:
            v, _lo, _hi, _bound, ts, posted_at, exists = contract.functions.latestPrint(
                index_id_to_bytes32(iid)
            ).call()
        except Exception:  # the contract reverts "no print" until the first post
            print(f"  {iid:<9} {'no print yet':>16}   (run `make post-once`)")
            continue
        if not exists:  # pragma: no cover - the revert path above is the real guard
            print(f"  {iid:<9} {'no print yet':>16}")
            continue
        any_print = True
        print(f"  {iid:<9} {v / WAD:>16.5f}   ts {ts}  posted_at {posted_at}")

    # ⑤ explorer links (+ best-effort last PricePosted tx — views carry no tx hash,
    #    so scan the recent logs; skip quietly if the RPC declines the range)
    print("\n  explorer:")
    print(f"    oracle    {explorer}/address/{oracle}")
    if registry:
        print(f"    registry  {explorer}/address/{registry}")
    if any_print:
        try:
            head = w3.eth.block_number
            logs = w3.eth.get_logs(
                {
                    "address": oracle,
                    "topics": [Web3.to_hex(Web3.keccak(text=PRICE_POSTED_SIG))],
                    "fromBlock": max(0, head - 50_000),
                    "toBlock": "latest",
                }
            )
            if logs:
                tx = Web3.to_hex(logs[-1]["transactionHash"])
                print(f"    last post {explorer}/tx/{tx}")
        except Exception:
            print("    (PricePosted log scan skipped — the RPC declined the range)")

    # ④ custody — hard on mainnet, advisory on testnet
    hard = chain_id == MAINNET_CHAIN_ID or os.environ.get("ACR_CUSTODY_STRICT", "") == "1"
    print("\n  custody" + ("" if hard else "  (advisory off mainnet; ACR_CUSTODY_STRICT=1 to enforce)"))
    ok &= _custody(w3, s, hard)

    if not ok:
        print("\n  ✗ verify FAILED — fix the ✗ checks above before going live.\n")
        sys.exit(1)
    print("\n  ✓ deploy verified — next: make api, then make post-once.\n")


if __name__ == "__main__":
    main()
