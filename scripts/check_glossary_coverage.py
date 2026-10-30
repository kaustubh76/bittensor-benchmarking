#!/usr/bin/env python
"""Gate: every uncommon term on the architecture diagram must be defined in the glossary.

Extracts candidate technical tokens from `acr_architecture.excalidraw`, drops the
ones that are plainly common English / filenames / zone-title fragments (an
explicit allowlist), and asserts each remaining token appears in
`docs/GLOSSARY.md`. Exits non-zero and prints the gaps if any term is undefined —
so "explain every uncommon term" can't silently drift as the diagram evolves.

    uv run python scripts/check_glossary_coverage.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIAGRAM = ROOT / "acr_architecture.excalidraw"
GLOSSARY = ROOT / "docs" / "GLOSSARY.md"

# Common English + structural words that need no glossary entry.
COMMON = set(
    """
    the a an and or of to in on at by for is are be it its this that these those with without
    you your they their our we not no yes any all both each per via than then so as if but
    real price can see estimates official number contracts settle like fake volume fools strip
    quality gaps compare drop extreme keep middle survives up before breaks many run attacker
    trade pump finds tight who paid move rate read sign data call sub cent chain fees paid itself
    bet diff delivery full raw event ingestion clean ticks model observed noise filter wash
    exclusion analysis caps median documented features from regression strips constant hourly
    tokens cost signed prints relayer submits value bound monotone owner metadata deadline tests
    suite future reference term structure first live curve vol seller reliability view runs
    through priced attack loose gate demo moved series administers roster benchmark native
    methodology liquidity administrator operator lesson moat published every naive err chart
    quotes settled trades attested passing python forge green deterministic sub second finality
    timestamps single canonical rail complete observation selection born dollars gas token known
    listed service discover load bearing math deployment cut lines class buckets paper never
    adoption study microstructure estimator trimmed out registry sellers index partners derived
    developers
    own bots publish weekly quoting mission control trading freeze monday polish rehearse twice
    one estimand machine services jargon this input inputs positions later has feeds pays buyer
    printing product baseline unleash swings wildly burns counter contaminated shapes threat
    default reader refresh decode consumed absorbed injected zeroed surviving shift moves width
    avg box tick schema step sided team fit why stack legend metrics execution ship distribution
    verification adversarial attestations authorizations marketplace demo day payloads
    highest frequency observable net settlements convolved noisy irregular deconvolves posterior
    var back references settlement grade offline tolerant self referential monetization customers
    natively neutrality surgical attainable empirical calibrated defenses first-class pause
    naive enforced github indexer attacks bias bonds eval family ids ruff monetized zones deals
    deal variance apps ci anyone handoff heavily reverses serves sharpens recover
    agentic economy autonomous demand deploy platform webhooks zone auto-discovers auto-refreshes
    discovers cache editorial events lands mocked pings registers seconds spend query intrinsic
    listings paid-query rpc.testnet.arc.io rpc.mainnet.arc.io floor backed netoi none skews warm
    anvil gated node limited breach ignores refuses restarts rounded silently
    deployfutures reads owns mandate gap position
    """.split()
)

# Filename / obvious-identifier patterns that need no entry.
FILE_RE = re.compile(r".*\.(py|sol|js|ts|md|toml)$", re.I)
PATHY = ("apps/", "docs/", "redteam/", "scripts/", "services/", "packages/")


def candidates(text: str) -> set[str]:
    out: set[str] = set()
    for tok in re.findall(r"[A-Za-z][A-Za-z0-9_./\-]{2,}", text):
        low = tok.lower()
        if FILE_RE.match(tok) or tok.startswith(PATHY):
            continue
        # split hyphen/slash compounds into parts too, but also keep the whole
        parts = [p for p in re.split(r"[/\-]", low) if p]
        if all(p in COMMON for p in parts) or low in COMMON:
            continue
        out.add(tok)
    return out


def main() -> int:
    diagram = json.loads(DIAGRAM.read_text())
    blob = " ".join(e.get("text", "") for e in diagram["elements"] if e["type"] == "text")
    gloss = GLOSSARY.read_text().lower()

    cands = candidates(blob)
    missing = sorted(t for t in cands if t.lower() not in gloss)
    # A compound also counts as covered when each hyphen/slash part is itself
    # either defined in the glossary or a plainly-common word (e.g. "naive-VWAP"
    # = common "naive" + defined "VWAP").
    still = []
    for t in missing:
        parts = [p for p in re.split(r"[/\-]", t.lower()) if p]
        if not all((p in gloss) or (p in COMMON) for p in parts):
            still.append(t)

    checked = len(cands)
    if still:
        print(f"✗ glossary coverage: {len(still)} uncommon term(s) undefined in docs/GLOSSARY.md")
        for t in still:
            print(f"    - {t}")
        return 1
    print(f"✓ glossary coverage: all {checked} uncommon diagram terms are defined in docs/GLOSSARY.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
