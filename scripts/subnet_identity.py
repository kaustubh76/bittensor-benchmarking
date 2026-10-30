#!/usr/bin/env python
"""Give netuid 602 a name, so it stops reading as `unknown` to everyone else.

Measured before this existed: `subnets.subnet_identity(netuid=602)` returned
`None`. The subnet had a mechanism, two neurons and a reward that had demonstrably
moved -- and no name. That is not cosmetic when the goal is for somebody else to
join, because registering costs a real burn and nobody pays it into a slot number.

Owner-only: the signing coldkey must own the netuid. `SetSubnetIdentity` IS a real
Intent in v11, so this goes through `execute` and the policy rail, unlike the
commitment write which has no intent and must use the raw hatch.

Fields left EMPTY are left empty on purpose. An identity that advertises a Discord
nobody runs or a repo that 404s is worse than one that says nothing: the blank
field is honest, the wrong one wastes somebody's afternoon. `--subnet-url` is
therefore opt-in, to be set once the subnet's own page actually exists rather than
pointed at a different product that happens to share this repo.
"""

from __future__ import annotations

import argparse
import sys

from acr_subnet.adapters.bt_chain import NETWORK_MAIN, NETWORK_TEST, Chain


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--netuid", type=int, default=602)
    ap.add_argument("--network", default=NETWORK_TEST)
    ap.add_argument("--wallet", default="acr-test")
    ap.add_argument("--hotkey", default="miner")
    ap.add_argument("--name", default="ACR Compute Rate")
    ap.add_argument(
        "--description",
        default="Constant-quality price index for AI inference compute.",
    )
    ap.add_argument("--subnet-url", default="")
    ap.add_argument("--github-repo", default="")
    ap.add_argument("--publish", action="store_true")
    args = ap.parse_args()
    sys.stdout.reconfigure(line_buffering=True)

    if args.network == NETWORK_MAIN:
        raise SystemExit("refusing mainnet: the only wallet here is a testnet key.")

    chain = Chain(network=args.network)
    print(f"chain   : {chain.head().cite()}")
    print(f"before  : {chain._st.subnets.subnet_identity(netuid=args.netuid)!r}")
    print(f"name    : {args.name!r} ({len(args.name.encode())} bytes)")
    print(f"desc    : {args.description!r} ({len(args.description.encode())} bytes)")
    print(f"url     : {args.subnet_url!r}   repo: {args.github_repo!r}")

    if not args.publish:
        print("\ndry run. Nothing signed. Pass --publish to write it.")
        return 0

    wallet = chain.wallet(args.wallet, args.hotkey)
    ck = wallet.coldkeypub.ss58_address
    before_bal = chain._st.balances.balance(coldkey_ss58=ck)
    print(f"coldkey : {ck}\nbalance : {before_bal}")

    receipt = chain.set_subnet_identity(
        wallet,
        args.netuid,
        subnet_name=args.name,
        description=args.description,
        subnet_url=args.subnet_url,
        github_repo=args.github_repo,
    )
    print(f"receipt : {receipt}")

    # The only thing that settles it.
    after = chain._st.subnets.subnet_identity(netuid=args.netuid)
    after_bal = chain._st.balances.balance(coldkey_ss58=ck)
    print(f"after   : {after!r}")
    print(f"balance : {after_bal}   (spent {before_bal - after_bal})")
    if after is None:
        print("\nSTILL None: the receipt is not the storage. Do not claim this landed.")
        return 1
    print("\nVERIFIED: the chain now carries an identity for this subnet.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
