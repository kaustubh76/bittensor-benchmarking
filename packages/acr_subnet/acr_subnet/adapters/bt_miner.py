"""A miner that serves its print over HTTP and tells the chain where it lives.

v11 has no transport. `ServeAxon` publishes an `ip:port` into the metagraph and
that is the whole of the chain's involvement in delivery — so the thing that
answers is an ordinary HTTP server, and this repo already depends on FastAPI.

The estimate itself comes from `FourPillarMiner`, unchanged. This module owns
the socket and nothing else: no scoring, no consensus, no opinion about what a
good print is. That separation is why the same miner object is exercised by 50
deterministic tests that never open a port.

SERVING AND ANNOUNCING ARE SEPARATE, DELIBERATELY
-------------------------------------------------
`serve` starts the HTTP server. `announce` submits the extrinsic. They are two
commands because they fail for unrelated reasons and at different rates: a
process restart is routine and must not cost TAO, while an address change is
rare and must be paid for. Announcing on every boot would burn a registration
fee every time a container restarted.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ..miner import FourPillarMiner
from ..protocol import RatePrint
from .bt_chain import Chain

# What `ServeAxon(protocol=...)` means. 4 is the SDK's own default and is what
# the chain will report to anyone reading the metagraph; it describes the
# transport, and ours is plain HTTP.
PROTOCOL_HTTP = 4


def build_app(miner: FourPillarMiner, source: Any, index_id: str) -> Any:
    """A FastAPI app with one honest route.

    `/print` recomputes from the tape on each request rather than serving a
    cached value. A validator asking twice in one epoch should get the same
    answer because the tape has not moved, not because a cache said so — and if
    the tape HAS moved, the fresher print is the true one. Caching here would
    make the miner's answer a function of its own uptime.
    """
    from fastapi import FastAPI, HTTPException  # noqa: PLC0415

    app = FastAPI(title="ACR subnet miner", docs_url=None, redoc_url=None)

    @app.get("/print", response_model=RatePrint)
    def print_() -> RatePrint:
        events = source.collect()
        if not events:
            # 503, not an empty print. "I have no tape" is a statement about
            # this miner's inputs; a print with a made-up value would be a
            # statement about the market, and the validator scores the latter.
            raise HTTPException(
                status_code=503,
                detail="no tape available: this miner has nothing to estimate from",
            )
        rp = miner.submit(
            index_id,
            epoch=0,
            events=events,
            attestations=source.attestations(),
            # `requires_single_venue` is the source's own name for it. Guessing
            # `single_venue` here silently defaulted to False on every real
            # source, which would configure the estimator for a funding graph
            # the tape does not have -- the exact mis-configuration the
            # single-venue guard exists to prevent.
            single_venue=bool(getattr(source, "requires_single_venue", False)),
        )
        if rp is None:
            raise HTTPException(
                status_code=503,
                detail="the estimator declined to print on this tape",
            )
        return rp

    @app.post("/estimate", response_model=RatePrint)
    def estimate(window: list[dict]) -> RatePrint:
        """Estimate on a window the CALLER supplies. This is how resistance is measured.

        Without this route the subnet cannot pay for resistance at all. The
        in-process validator measures it by re-running each miner's estimator on
        an attacked window (`validator.measure_miner_resistance`), which it can
        do because it holds the miner object. A networked validator holds only a
        URL, so the only way it can learn whether manipulation moves THIS
        miner's estimate is to hand over the tape and compare the answer to the
        miner's own clean print. Measured consequence of not having it: with
        `truth=None` (no latent on a real chain), `measured_cost_per_bp=None`
        and `bp_per_usd=None`, all three pillars drop, every score is 0, and
        `weights_from_scores` correctly pays nobody — a validator that can never
        pay anyone.

        KNOWN GAMING VECTOR, stated rather than hidden
        ----------------------------------------------
        A miner can tell this route from `/print` and behave differently on it —
        for instance by answering an attacked window with its clean value, which
        would measure as perfect resistance. The in-process harness has no such
        hole because the validator controls the estimator directly.

        A canary now catches the simplest form of this: see
        `bt_validator.probe_resistance`, which answers an unmoved estimate with a
        price-rescaled window that any real estimator must answer differently.
        The only way to pass it is to actually estimate the window you were
        given. It catches echo-by-constant and zeroes it.

        It does NOT catch a miner that genuinely estimates but special-cases
        windows carrying injected wash flow, and the second mitigation --
        interleaving unperturbed windows so a miner cannot know which probe
        counts -- is still unbuilt. Resistance measured over the wire remains
        weaker than resistance measured in process.
        """
        from acr_core import TapeEvent  # noqa: PLC0415

        if not window:
            raise HTTPException(status_code=422, detail="empty window")
        try:
            events = [TapeEvent.model_validate(e) for e in window]
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(
                status_code=422, detail=f"unparseable window: {exc}"
            ) from exc
        rp = miner.submit(
            index_id,
            epoch=0,
            events=events,
            attestations=source.attestations(),
            single_venue=bool(getattr(source, "requires_single_venue", False)),
        )
        if rp is None:
            # Abstaining under attack is the correct move and the in-process
            # validator treats it as maximally resistant, so it must be
            # expressible here rather than looking like a server fault.
            raise HTTPException(
                status_code=409,
                detail="the estimator declined to print on the supplied window",
            )
        return rp

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def announce(
    chain: Chain, wallet: Any, netuid: int, ip: str, port: int
) -> str:
    """Publish this miner's address on chain. Costs TAO.

    Refuses mainnet for the same reason the validator does: nothing here has
    been exercised there, and the first time it is should be somebody's
    decision rather than a default.
    """
    if chain.is_mainnet():
        raise ValueError(
            "refusing to announce on mainnet from this adapter: it has never "
            "been exercised there. Announce on 'test' first."
        )
    return str(
        chain.serve_axon(wallet, netuid, ip, port, protocol=PROTOCOL_HTTP)
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="run the HTTP server (no chain, no cost)")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8091)
    s.add_argument("--index-id", default="ACR-INF")  # the Chutes tape is inference

    a = sub.add_parser("announce", help="publish ip:port on chain (costs TAO)")
    a.add_argument("--network", default="test")
    a.add_argument("--netuid", type=int, required=True)
    a.add_argument("--wallet", required=True)
    a.add_argument("--hotkey", default="default")
    a.add_argument("--ip", required=True, help="the address OTHERS can reach")
    a.add_argument("--port", type=int, required=True)

    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn  # noqa: PLC0415
        from acr_tape.chutes_source import ChutesSource  # noqa: PLC0415

        app = build_app(FourPillarMiner(uid=0), ChutesSource(), args.index_id)
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    chain = Chain(network=args.network)
    # 127.0.0.1 would publish an address no other validator can dial. Caught
    # here rather than discovered later as a subnet full of unreachable miners.
    if args.ip.startswith(("127.", "0.0.0.0", "localhost")):
        print(
            f"refusing to announce {args.ip}: a loopback address is reachable "
            "only from the miner's own machine, so the chain would carry a "
            "promise nobody can keep. Publish a routable address.",
            file=sys.stderr,
        )
        return 2
    h = chain.head()
    receipt = announce(
        chain, chain.wallet(args.wallet, args.hotkey), args.netuid, args.ip, args.port
    )
    print(f"announced {args.ip}:{args.port} on netuid {args.netuid} at {h.cite()}")
    print(f"receipt: {receipt}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
