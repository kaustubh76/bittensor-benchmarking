#!/usr/bin/env python3
"""Render docs/acr-openapi.md from the live OpenAPI schema — which is what that
file has claimed to be since it was written, and never was.

    uv run python scripts/gen_openapi_doc.py            # rewrite the doc
    uv run python scripts/gen_openapi_doc.py --check    # exit 1 if it is stale

THE FILE WAS A HAND-EDITED DOC WEARING A GENERATED ONE'S LABEL. docs/README.md says
"rendered from the live OpenAPI 3.1 schema"; there was no script, no Makefile
target, and the one commit that touched it added a single line by hand. It listed
24 routes when the service had 43 — every route added since the hedger, including
the whole agent gate, was simply absent from "the endpoint reference". The house
rule at Makefile `deck:` names this exact shape: a generated file with an
unrecorded recipe is a hand-edited file that nobody admits to.

Source of truth is `app.openapi()` — the same object `tests/test_endpoint_register_
parity.py` reads — so this cannot disagree with the service. The paid/free split
comes from GATED_ENDPOINTS, the list `/x402/info` serves, not from a second copy —
which is also why `/compute/{label}` had to join that list: a per-seller paid route
the service's own descriptor called free. `--check` makes staleness a CI failure like `golden-check` and `anchors-check`.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "acr-openapi.md"

#: Where a reader should actually send a request, and what each host serves.
#:
#: THIS WAS ONE HARDCODED HOST AND IT WAS THE WRONG ONE. `API` read
#: `https://acr-api-mainnet.onrender.com` while the route list below was
#: rendered from the LOCAL checkout — so this document named a host, listed
#: fifty routes, and that host answered **404** for six of them (`/par` and all
#: five `/operator/*`). Measured 2026-10-12 with `make verify-drift`: 44 served
#: of 50 built. `--check` compares this file to the repo, so the gap was
#: invisible to the gate by construction; it took asking the hosts.
#:
#: TESTNET IS LISTED FIRST, deliberately. It serves the whole surface, it has a
#: faucet, and nobody's real money is involved in a stranger's first call. The
#: mainnet pair is below it, with the incomplete one named as incomplete rather
#: than as the canonical address.
#:
#: `tests/test_doc_hosts.py` asserts that no reader-facing document names a host
#: alongside a route that host does not serve.
HOSTS = [
    (
        "https://acr-api-1fto.onrender.com",
        "Arc testnet (5042002)",
        "**start here** — all 50 routes, and a faucet, so nothing costs real money",
    ),
    (
        "https://acr-probe-free.onrender.com",
        "Arc mainnet (5042)",
        "all 50 routes, reading real mainnet state. Holds no signing key",
    ),
    (
        "https://acr-api-mainnet.onrender.com",
        "Arc mainnet (5042)",
        "44 of 50: `/par` and `/operator/*` answer 404 until its image is rebuilt",
    ),
]

#: The host every runnable example in this document points at.
API = HOSTS[0][0]

#: One call a reader can paste, because there was not one anywhere in the repo.
#: `/par` takes three REQUIRED query params and 422s without them, and its unit
#: needs percent-encoding (`$/1k tokens`), so "GET /par" on its own is not
#: something anybody can run. The only complete URL in the tree was an internal
#: probe path in `apps/terminal/lib/endpoints.ts`.
PAR_EXAMPLE = (
    "curl -s '{api}/par?unit=%24%2F1k%20tokens&billed_usdc=0.02&quantity=10'"
)

#: Routes the reference deliberately omits, each with the reason. Mirrors the
#: exclusion dict in tests/test_endpoint_register_parity.py: an operator surface
#: and a seller's metered endpoint are not things a reader types.
OMIT = {
    "/ops/verify": "operator console, token-gated",
    "/ops/actions": "operator console, token-gated, spends money",
    # The owner payment inbox (#66): bearer-token routes for the treasury App,
    # off (401) until ACR_TREASURY_API_TOKEN is set. Owner tooling, not the API.
    "/operator/payments": "owner payment inbox, token-gated",
    "/operator/payments/prepare": "owner payment inbox, token-gated, stages a payment for the owner to sign",
    "/operator/payments/verify": "owner payment inbox, token-gated",
}



def render() -> str:
    from index_api.app import GATED_ENDPOINTS, app, price_usdc

    spec = app.openapi()
    paths = spec["paths"]
    gated = set(GATED_ENDPOINTS)

    def lines(paid: bool) -> list[str]:
        out = []
        for path in sorted(paths):
            if path in OMIT or (path in gated) != paid:
                continue
            for method, op in sorted(paths[path].items()):
                summary = str(op.get("summary") or "").strip() or path.strip("/").replace("/", " ").title()
                m = method.upper()
                out.append(f"**`{m} {path}`** — {summary}" if paid else f"`{m} {path}` — {summary}")
        return out

    paid, free = lines(True), lines(False)
    price = price_usdc()
    return "\n".join([
        "---",
        "marp: true",
        "paginate: true",
        "---",
        "",
        f"# {spec['info']['title']}",
        f"## OpenAPI {spec['openapi']} · endpoint reference",
        "",
        "## Where to send it",
        "",
        "| host | chain | serves |",
        "|---|---|---|",
        *[f"| `{h}` | {chain} | {what} |" for h, chain, what in HOSTS],
        "",
        f"Live spec: `{API}/openapi.json`",
        f"Catalog: `{API}/marketplace/catalog`",
        "",
        "### One call, runnable as written",
        "",
        "`/par` prices a real invoice against published third-party list prices.",
        "It needs no key, no card and no payment:",
        "",
        "```bash",
        PAR_EXAMPLE.format(api=API),
        "```",
        "",
        "All three query params are required (it answers 422 without them), and the",
        "unit is one of `$/1k tokens`, `$/GPU-sec`, `$/MB`, percent-encoded.",
        "",
        "Read `basket.status` before the verdict: **`ok`** and **`aging`** both price",
        "against the market, the SCREAMING values (`STALE`, `ABSENT`, `NO_ROWS`) mean",
        "it fell back to our own sellers, which `benchmarked_against` names. The basket",
        "carries `age_s` beside `max_age_s`, so its age is readable without guessing",
        "the window.",
        "",
        f"Index endpoints: **${price} USDC per request**, x402 `exact` scheme,",
        # THE NETWORK FOLLOWS THE HOST. This read "network `eip155:5042` (Arc
        # mainnet)" flat, which was true of the one host the doc used to name
        # and is false of the testnet host it now starts a reader on — the
        # x402 guard refuses a testnet facilitator on 5042 and vice versa. A
        # payment built against the wrong chain id fails in the EIP-712 domain,
        # which is a confusing place to learn it.
        "Circle Gateway (GatewayWalletBatched), on the chain the host you called is",
        "on: `eip155:5042` for Arc mainnet, `eip155:5042002` for testnet. `GET",
        "/x402/info` on that host states its own, and is the field to trust.",
        "Fleet sellers (`/compute/{label}`) price per unit; the catalog carries each one's terms.",
        "",
        "Agent-to-agent calls may present an `AGENT-CARD` (see `GET /agent/challenge`);",
        "carded tape reads pass Google Cloud Model Armor in both directions (`GET /armor/info`).",
        "",
        "---",
        "",
        f"## Paid endpoints (x402-gated) · {len(paid)}",
        "",
        *paid,
        "",
        "---",
        "",
        f"## Free endpoints (no payment required) · {len(free)}",
        "",
        *free,
        "",
        "---",
        "",
        f"*Rendered by `scripts/gen_openapi_doc.py` from `app.openapi()` — {len(paid) + len(free)} routes;*",
        f"*{len(OMIT)} operator routes omitted on purpose. `make openapi-doc-check` fails when this is stale.*",
        "",
    ])


def main() -> int:
    text = render()
    if "--check" in sys.argv:
        current = OUT.read_text() if OUT.exists() else ""
        if current != text:
            print(f"✗ {OUT.relative_to(ROOT)} is stale — run `make openapi-doc`")
            return 1
        print(f"✓ {OUT.relative_to(ROOT)} matches app.openapi()")
        return 0
    OUT.write_text(text)
    routes = sum(1 for ln in text.splitlines() if ln.startswith(("`GET ", "`POST ", "**`GET ", "**`POST ")))
    print(f"wrote {OUT.relative_to(ROOT)}: {routes} routes from app.openapi()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
