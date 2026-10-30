#!/usr/bin/env python
"""What it costs to join netuid 602 right now, and the commands to do it.

Reads the price off the chain instead of printing a table, because this
project's own notes record the table being wrong by ~1800x: τ0.0005 is the
`min_burn` of an ESTABLISHED subnet, while a fresh one starts near `max_burn`
and decays toward it over `burn_half_life`. Registering uid 1 on 602 minutes
after creation cost τ0.902987384. Three hours later the same burn was
τ0.539024711. The rule that came out of that is the reason this script exists:
price the burn at the moment you intend to register.

Read-only. It signs nothing and costs nothing.
"""

from __future__ import annotations

import argparse

from acr_subnet.adapters.bt_chain import NETWORK_TEST, Chain


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--netuid", type=int, default=602)
    ap.add_argument("--network", default=NETWORK_TEST)
    ap.add_argument("--wallet", default="", help="optional: check affordability")
    ap.add_argument("--hotkey", default="default")
    args = ap.parse_args()

    chain = Chain(network=args.network)
    head = chain.head()
    print(f"netuid {args.netuid} on {head.cite()}")

    ident = chain.subnet_identity(args.netuid)
    if ident and ident.get("subnet_name"):
        print(f"  name        : {ident['subnet_name']}")
        if ident.get("description"):
            print(f"  description : {ident['description']}")
    else:
        print("  name        : (none set)")

    hp = chain.hyperparameters(args.netuid)
    allowed = hp.get("registration_allowed")
    print(f"  registration_allowed : {allowed}")
    if not allowed:
        print("\n  Burned registration is OFF. Only the owner can turn it on, and")
        print("  `registration_allowed` governs BURNED registration only -- PoW needs")
        print("  a separate per-subnet opt-in that is not among the hyperparameters.")

    burn = chain.registration_burn(args.netuid)
    print(f"  burn NOW    : {burn if burn is not None else 'UNKNOWN'}")
    print(f"  min_burn    : {hp.get('min_burn')}   max_burn: {hp.get('max_burn')}")
    print(f"  burn_half_life: {hp.get('burn_half_life')} blocks")

    if args.wallet:
        w = chain.wallet(args.wallet, args.hotkey)
        bal = chain._st.balances.balance(coldkey_ss58=w.coldkeypub.ss58_address)
        print(f"\n  your balance: {bal}")
        if burn is None:
            # An unknown price is NOT an affordable one. Saying "affordable"
            # here off a None would be the same class of error as a scan that
            # reports "none enabled" when it had no way to detect one enabled.
            print("  AFFORDABILITY UNKNOWN: could not read the live burn, so this")
            print("  script will not tell you it is affordable. Re-read before signing.")
        else:
            # Balance supports `<` against another Balance and carries an
            # integer `.rao`; it does NOT cast to float, so `float(bal)` raises
            # and would have been caught as "unknown" on a price we could read.
            try:
                short = int(bal.rao) < int(burn.rao)
            except AttributeError:
                try:
                    short = bool(bal < burn)
                except TypeError:
                    short = None
            if short is None:
                print("  AFFORDABILITY UNKNOWN: balance and burn would not compare.")
            elif short:
                print(f"  NOT AFFORDABLE at block {head.block}: the burn exceeds the balance.")
                print("  It decays toward min_burn -- re-read it later rather than topping up.")
            else:
                print(f"  affordable at block {head.block} (the burn moves; re-read before signing).")

    print(
        f"""
  commands
  --------
  uv sync --extra bittensor
  btcli wallet new_hotkey --wallet.name <yours> --wallet.hotkey miner
  btcli subnet register --netuid {args.netuid} --network {args.network}

  # serve a print (no chain, no cost)
  uv run python -m acr_subnet.adapters.bt_service --host 0.0.0.0 --port 8091

  # then publish WHERE it answers, so a validator can find it
  uv run python scripts/subnet_publish.py --endpoint https://your-host.example/ --publish

  An ENDPOINT COMMITMENT, not an axon, if you are on a managed host. `ServeAxon`
  stores an `ip` and a `port` and has no hostname field, so Render/Fly/Vercel --
  which route by Host/SNI behind shared addresses -- cannot be announced as an
  axon at all. A bare IP there reaches the platform's router, never your app.
"""
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
