"""A document may not name a host beside a route that host does not serve.

THE FAILURE THIS EXISTS FOR, measured 2026-10-12 with `make verify-drift`:

    acr-probe-free    mainnet 5042      50 of 50 served
    acr-api-1fto      testnet 5042002   50 of 50 served
    acr-api-mainnet   mainnet 5042      44 of 50  <- 404s /par and all /operator/*

and `docs/acr-openapi.md` — the repo's only endpoint reference, linked from
`docs/README.md` as "the seller API, endpoint by endpoint" — named
`acr-api-mainnet` as its live spec and catalog, then listed all six routes that
host answers 404 for. A stranger following our own documentation to the one
surface they can exercise without a key got a 404.

WHY THE EXISTING GATES COULD NOT SEE IT. `gen_openapi_doc.py --check` compares
the document to `app.openapi()` — the LOCAL route table — so a doc generated
from this checkout and labelled with a host that serves six fewer routes matches
perfectly. `verify_deploy_drift.py` does ask the hosts, and it is a manual
`make verify-drift` that is not in CI. Neither was wrong; they were answering a
different question, and nobody was asking this one.

This gate is hermetic on purpose, so it runs in CI with no network: the
incomplete host and what it lacks are recorded below as measurements, and the
check is that no document presents that host as somewhere to send a request
without saying so. When the image is rebuilt, delete the entry and the gate
relaxes by itself.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Hosts that do NOT serve the whole route set, and the routes they lack.
#: Measured, not assumed — `VERIFY_DRIFT_HOSTS=… uv run python
#: scripts/verify_deploy_drift.py` prints exactly this.
INCOMPLETE = {
    "acr-api-mainnet.onrender.com": {
        "/par",
        "/operator/audit",
        "/operator/businesses",
        "/operator/ledger",
        "/operator/statement",
        "/operator/traction",
    },
}

#: Hosts that serve everything this checkout builds.
COMPLETE = {
    "acr-api-1fto.onrender.com",
    "acr-probe-free.onrender.com",
}

#: Words that mean the document has told the reader about the gap. A doc may
#: absolutely discuss the incomplete host — `docs/DEPLOY.md` does it well — the
#: rule is only that it must not present it as a working address in silence.
QUALIFIERS = ("404", "44 ", "44 of", "does not serve", "no `/par`", "no /par",
              "predates", "not on this press", "until its image", "incomplete")

#: Documents a reader is expected to follow. Operational runbooks are not here:
#: `docs/MAINNET_RUNBOOK.md` and `docs/DEPLOY.md` are written for whoever
#: operates that host, where naming it is the whole point.
READER_FACING = [
    "README.md",
    "IMPLEMENTATION.md",
    "docs/acr-openapi.md",
    "docs/ARCHITECTURE-DIAGRAM.md",
    "docs/GLOSSARY.md",
    "docs/TAMEION.md",
    # The submission update: it hands judges a runnable curl, so the host in it
    # has to be one that serves the route. Added when it was written rather
    # than after somebody followed it into a 404.
    "docs/CANTEEN-UPDATE.md",
    "mcp/README.md",
]


def _docs() -> list[tuple[str, str]]:
    out = []
    for rel in READER_FACING:
        p = ROOT / rel
        if p.exists():
            out.append((rel, p.read_text(encoding="utf-8")))
    return out


def test_the_endpoint_reference_sends_readers_to_a_host_that_serves_it():
    """The generated reference's own example host must be a complete one.

    This is the specific thing that was wrong: `API` in `gen_openapi_doc.py` was
    the 44-route host, and every `curl` and `Live spec:` line in the document
    inherited it.
    """
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_openapi_doc as gen

    host = gen.API
    bare = host.replace("https://", "").replace("http://", "").rstrip("/")
    assert bare not in INCOMPLETE, (
        f"the endpoint reference points its examples at {bare}, which does not serve "
        f"{sorted(INCOMPLETE[bare])[:3]}… — a reader following this document gets a 404"
    )
    assert bare in COMPLETE, f"{bare} is not a host known to serve the whole route set"


def test_the_host_table_admits_which_host_is_incomplete():
    """A table may list the incomplete host — it must not list it silently."""
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import gen_openapi_doc as gen

    for host, _chain, what in gen.HOSTS:
        bare = host.replace("https://", "").rstrip("/")
        if bare in INCOMPLETE:
            assert any(q.strip() in what for q in QUALIFIERS), (
                f"{bare} is listed as a place to send requests with no mention that it "
                f"serves {len(INCOMPLETE[bare])} fewer routes: {what!r}"
            )


@pytest.mark.parametrize("rel", READER_FACING)
def test_no_runnable_example_points_at_an_incomplete_host(rel: str):
    """A command inside a code fence is held to a stricter rule than prose.

    THE HOLE THIS CLOSES, found by trying to make the check below fail and
    watching it pass. That one asks per FILE: name the incomplete host and the
    gap must be mentioned somewhere in the document. Reasonable for prose, and
    far too loose for a `curl` — `docs/CANTEEN-UPDATE.md` legitimately explains
    the 44-route host in its delta section, so a runnable example aimed at that
    host inherited the explanation and passed.

    A reader does not read the qualifier before pasting the command. So inside a
    fenced block the rule is absolute: no incomplete host, no exceptions.
    """
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} does not exist")

    offenders = []
    in_fence = False
    for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("```"):
            in_fence = not in_fence
            continue
        if not in_fence:
            continue
        for bare, missing in INCOMPLETE.items():
            if bare not in line:
                continue
            # THE PATH IS WHAT MATTERS, NOT THE HOST. The first version banned
            # the host outright and flagged `curl …/health` in README.md — a
            # command that works perfectly, because this host serves /health
            # and 44 other routes. Banning a host for the routes it lacks would
            # make the gate wrong about a working example, which is the same
            # mistake as the false failures the page gate produced.
            after = line.split(bare, 1)[1]
            path = "/" + after.split("/", 1)[1].split()[0].split("?")[0] if "/" in after else ""
            if any(path == m or path.startswith(m + "/") for m in missing):
                offenders.append((n, bare, path, line.strip()[:70]))

    assert offenders == [], "\n".join(
        f"{rel}:{n} runs {path} against {bare}, which 404s it — {text}"
        for n, bare, path, text in offenders
    ) + (
        "\n\nA command in a code fence is pasted, not read around. Point it at "
        f"a complete host ({', '.join(sorted(COMPLETE))})."
    )


@pytest.mark.parametrize("rel", READER_FACING)
def test_no_reader_facing_doc_offers_an_incomplete_host_in_silence(rel: str):
    """If a document names the incomplete host, it must name the gap too.

    Checked per FILE rather than per line: the qualifier does not have to sit on
    the same line as the URL, because the honest way to write this is usually a
    sentence nearby. What fails is a document that hands over the address and
    never mentions that six routes are missing.
    """
    p = ROOT / rel
    if not p.exists():
        pytest.skip(f"{rel} does not exist")
    text = p.read_text(encoding="utf-8")

    for bare, missing in INCOMPLETE.items():
        if bare not in text:
            continue
        # Does this document actually tell a reader to use it, or merely mention
        # it? A bare hostname inside a code span or a URL is an address.
        offers = re.search(rf"https?://{re.escape(bare)}", text) is not None
        if not offers:
            continue
        assert any(q in text for q in QUALIFIERS), (
            f"{rel} gives the address https://{bare} and never says it answers 404 for "
            f"{sorted(missing)[:3]}… — either name the gap or point at a complete host "
            f"({', '.join(sorted(COMPLETE))})"
        )
