#!/usr/bin/env python
"""Register a Circle entity secret for developer-controlled wallets.

Follows Circle's documented flow
(https://developers.circle.com/wallets/dev-controlled/register-entity-secret):
generate a 32-byte entity secret, register its ciphertext with Circle (using
``ACR_CIRCLE_API_KEY``), and save the **recovery file** — the only way to reset
the secret if it is lost (Circle cannot recover it).

This is a one-time account setup and is effectively irreversible without a
Console reset. Two deviations from Circle's sample, on purpose:
  * it does NOT auto-append to ``.env`` (operator-owned; ACR uses the
    ``ACR_CIRCLE_ENTITY_SECRET`` name) — you paste it in yourself, and
  * the secret is written to a gitignored local file + only a masked preview is
    printed, so the full secret never lands in a terminal transcript.

    uv run python scripts/register_entity_secret.py
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
from pathlib import Path

from acr_core import get_settings


def main() -> None:
    s = get_settings()
    api_key = (s.circle_api_key or "").strip()
    if not api_key or api_key.startswith("#"):
        print("\n  ✗ ACR_CIRCLE_API_KEY is not set — add your Circle API key to .env first.\n")
        sys.exit(1)
    if s.circle_entity_secret and not s.circle_entity_secret.strip().startswith("#"):
        print(
            "\n  ACR_CIRCLE_ENTITY_SECRET is already set in .env — refusing to register another.\n"
            "  To rotate, reset it in the Circle Console (Configurator → Entity Secret), then re-run.\n"
        )
        sys.exit(0)

    from circle.web3 import utils

    # Generate the secret ourselves (Circle's documented way). The SDK's
    # generate_entity_secret() *prints* the secret and returns None, which would
    # both leak it and pass None to register — so we don't use it.
    entity_secret = os.urandom(32).hex()  # 32-byte hex

    # recoveryFileDownloadPath is a DIRECTORY (Circle's sample os.makedirs it).
    recovery_dir = Path("data/circle_recovery")
    recovery_dir.mkdir(parents=True, exist_ok=True)  # data/ is gitignored

    # Suppress the SDK's stdout — it echoes the secret in a banner + example.
    hushed = io.StringIO()
    try:
        with contextlib.redirect_stdout(hushed):
            utils.register_entity_secret_ciphertext(
                api_key=api_key,
                entity_secret=entity_secret,
                recoveryFileDownloadPath=str(recovery_dir),
                base_url=(s.circle_base_url or "https://api.circle.com"),
            )
    except Exception as exc:  # noqa: BLE001 - surface the Circle API error clearly
        print(f"\n  ✗ registration failed: {exc}")
        print(
            "  If an entity secret is ALREADY registered for this API key, reset it in the "
            "Circle Console (Configurator → Entity Secret) and re-run.\n"
        )
        sys.exit(1)

    # Write the secret to a gitignored file (chmod 600) — never to stdout.
    secret_file = Path("data/ACR_CIRCLE_ENTITY_SECRET.txt")
    secret_file.write_text(entity_secret + "\n")
    secret_file.chmod(0o600)

    masked = f"{entity_secret[:6]}…{entity_secret[-4:]}"
    recovery_files = sorted(p.name for p in recovery_dir.iterdir()) or ["(none written?)"]
    print("\n  ✓ entity secret registered with Circle.\n")
    print(f"  entity secret (masked): {masked}   ({len(entity_secret)} hex chars)")
    print(f"  full secret written to: {secret_file}   (gitignored, chmod 600)")
    print("     → paste it into .env as  ACR_CIRCLE_ENTITY_SECRET=<value>  then delete that file.")
    print(f"  recovery file(s):       data/circle_recovery/{recovery_files[0]}")
    print("     → move to your password manager / secure store — it is the ONLY reset path.\n")
    print("  Next: create a wallet set + wallet → ACR_CIRCLE_WALLET_SET_ID / ACR_CIRCLE_WALLET_ID.\n")


if __name__ == "__main__":
    main()
