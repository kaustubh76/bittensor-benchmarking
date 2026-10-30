"""The mainnet guard — what must be true before the API is allowed to start on Arc mainnet.

Both gates fail OPEN in ``auto`` mode: with a facilitator URL that does not look real the
x402 gate becomes the free dev gate, and with no World app id the human tier is granted by a
verifier that cannot prove a human exists. On testnet that is a convenience. On mainnet it
means one missing environment variable gives prints away and dissolves the per-person
budget, with a log warning as the only signal. Nobody reads a log warning.

So on chain ``5042`` the service refuses to boot unless every mode is explicit and every
backend it names is configured — the failure is a stack trace at deploy time, not a quiet
week of free prints. Off mainnet the guard is a no-op, so local development is untouched.

The testnet-only money surfaces (the faucet, the demo buyer, the attack lab) are a separate
property: ``testnet_surfaces_enabled`` is FALSE on mainnet no matter what the environment
says. No variable can turn them on there, which is the point — a kill-switch that can be
flipped by a typo is not a kill-switch.
"""

from __future__ import annotations

from .config import ACRSettings

#: Arc public mainnet (eip155:5042). Genesis 2026-09-16.
MAINNET_CHAIN_ID = 5042


class MainnetGuardError(RuntimeError):
    """Raised at startup with every violation listed, so a deploy fixes them in one pass."""


def is_mainnet(settings: ACRSettings) -> bool:
    return int(settings.arc_chain_id) == MAINNET_CHAIN_ID


def testnet_surfaces_enabled(settings: ACRSettings) -> bool:
    """The faucet, the demo buyer and the attack lab exist only off mainnet.

    ``ACR_TESTNET_SURFACES=0`` turns them off on testnet too (a staging host that
    should not drip). Nothing turns them on on mainnet.
    """
    if is_mainnet(settings):
        return False
    return bool(settings.testnet_surfaces)


def violations(settings: ACRSettings) -> list[str]:
    """Every reason this configuration must not serve on mainnet. Empty means go."""
    if not is_mainnet(settings):
        return []
    out: list[str] = []
    x402 = settings.x402_mode.strip().lower()
    if x402 != "circle":
        out.append(
            f"ACR_X402_MODE={x402 or '<unset>'!r}: mainnet requires 'circle' explicitly — "
            "'auto' falls back to the FREE dev gate when Circle looks unconfigured"
        )
    if not settings.x402_facilitator_url.strip().startswith("http"):
        out.append("ACR_X402_FACILITATOR_URL is not a URL: the paywall would have no facilitator")
    if not settings.x402_pay_to.strip().startswith("0x"):
        out.append("ACR_X402_PAY_TO is not an address: paid prints would be paid to nobody")
    human = settings.humanid_mode.strip().lower()
    if human != "agentkit":
        out.append(
            f"ACR_HUMANID_MODE={human or '<unset>'!r}: mainnet requires 'agentkit' explicitly — "
            "the dev verifier grants the human tier to anyone who asks"
        )
    if not settings.humanid_app_id.strip():
        out.append("ACR_HUMANID_APP_ID is unset: a proof scoped to no app authorizes nothing")
    rpc = settings.arc_rpc_url.strip()
    if not rpc.startswith("https://") or "127.0.0.1" in rpc or "localhost" in rpc:
        out.append("ACR_ARC_RPC_URL is not a mainnet https endpoint (the public one is "
                   "https://rpc.mainnet.arc.io; a keyed provider URL also works)")
    pub = settings.public_rpc_url.strip()
    if not pub.startswith("https://"):
        out.append("ACR_PUBLIC_RPC_URL resolved to nothing a visitor's wallet can use: "
                   "wallet_addEthereumChain would add a chain with no RPC")
    if not settings.x402_facilitator_url.strip().startswith("https://gateway-api.circle.com"):
        out.append(
            "ACR_X402_FACILITATOR_URL is not Circle's mainnet Gateway "
            "(https://gateway-api.circle.com): the testnet facilitator does not know eip155:5042"
        )
    if not settings.explorer_base.strip():
        out.append("ACR_EXPLORER_BASE resolved empty: every transaction link would be dead")
    prof = settings.chain_profile
    if settings.x402_gateway_wallet.strip().lower() != prof.gateway_wallet.lower():
        out.append(
            f"ACR_X402_GATEWAY_WALLET is not Arc mainnet's GatewayWallet ({prof.gateway_wallet}): "
            "a testnet Gateway address left in the environment fails every payment"
        )
    if not settings.circle_blockchain:
        out.append("no Circle blockchain enum for this chain id: the Desk cannot open wallets")
    if settings.cors_origins.strip() in ("", "*"):
        out.append("ACR_CORS_ORIGINS is '*': name the Terminal's origin on mainnet")
    return out


def assert_mainnet_ready(settings: ACRSettings) -> None:
    """Raise with the full list, or return quietly. Called once, at startup."""
    bad = violations(settings)
    if bad:
        lines = "\n  - ".join(bad)
        raise MainnetGuardError(
            f"refusing to start on Arc mainnet (chain {MAINNET_CHAIN_ID}) — "
            f"{len(bad)} violation(s):\n  - {lines}"
        )
