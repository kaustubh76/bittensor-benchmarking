#!/usr/bin/env python
"""Check that the numbers in the README are still true.

This project's whole argument is that its claims are true, and its front door
states them. But numbers written into prose rot silently: every figure this
script found stale was correct the day it was typed, and nothing in the repo
noticed when the suites grew past it. `verify_live.py` proves the deployed
product; this proves the documentation.

The claims are **parsed out of the docs** and re-measured, rather than compared
against a second hardcoded list — a checker that carries its own copy of the
answer is a mirror, and drifts in step with whatever it was meant to catch.

Measuring costs real time (it collects the test suites), so the expensive checks
are skippable for a quick pass:

    uv run python scripts/verify_claims.py          # == make verify-claims
    CLAIMS_FAST=1 …                                 # skip suite collection

Hackathon-era documents (hackathon/) were audited once and are now frozen;
only what a user reads is held to reality.

Exit codes: 0 = every claim still holds; 1 = the README is lying.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
#: The README is the one document this audit holds to reality. It is the first
#: thing a user opens, and every number it states — suite sizes, the glossary,
#: the receipts archive, the CI shape, the deployed addresses — is re-measured
#: here so it cannot drift. Hackathon-era documents (now under hackathon/) were
#: once audited too; they are frozen and no longer checked.
README = ROOT / "README.md"
FAST = os.environ.get("CLAIMS_FAST", "") not in ("", "0", "false")

_failures: list[str] = []


def check(ok: bool, label: str) -> bool:
    print(f"  {'✓' if ok else '✗'} {label}")
    if not ok:
        _failures.append(label)
    return ok


#: Why the last measurement could not be made, per label. A measurement that
#: fails has to be able to say HOW, or every failure reads as the same shrug.
LAST_FAILURE: dict[str, str] = {}


def run(cmd: list[str], cwd: Path | None = None, timeout: int = 900) -> str:
    """Capture a command's output; '' when it cannot run (never raises, so one
    missing toolchain reports itself instead of hiding every other claim)."""
    try:
        p = subprocess.run(
            cmd, cwd=cwd or ROOT, capture_output=True, text=True, timeout=timeout
        )
        return (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return f"__ERROR__ timed out after {timeout}s running {' '.join(cmd[:3])}…"
    except Exception as exc:  # noqa: BLE001 — a verdict beats a traceback
        return f"__ERROR__ {exc}"


def git_lines(args: list[str]) -> list[str] | None:
    """stdout lines for a git command, or None when git REFUSED.

    `run()` deliberately folds stderr into stdout so a missing toolchain reports
    itself. For measurements that is the wrong trade: in CI this turned
    `git log --oneline v1.0-submission..0aff288` — which fails outright in the
    shallow clone `actions/checkout` makes by default — into a three-line error
    message that got counted as THREE COMMITS, and the continuity claims were
    reported STALE against numbers that were never measured.

    A failure must not be able to look like a small answer. None means "could not
    measure", which is a different fact from "measured and it disagrees", and the
    caller is obliged to say which.
    """
    try:
        p = subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, timeout=120
        )
    except Exception:  # noqa: BLE001 — a verdict beats a traceback
        return None
    if p.returncode != 0:
        return None
    return p.stdout.splitlines()


def claim(text: str, pattern: str) -> int | None:
    """The first integer a documented claim asserts, or None if the doc no
    longer phrases it that way — which is itself worth reporting, because a
    check silently matching nothing is a check that always passes."""
    m = re.search(pattern, text)
    return int(m.group(1).replace(",", "")) if m else None


#: The architecture canvas states the suite sizes on two of its cards, and
#: docs/ARCHITECTURE-DIAGRAM.md advertises that canvas as "implementation-accurate" — so those are
#: claims, not decoration. They rotted to 289 py / 81 terminal against a suite of
#: 367 / 95 for exactly the reason the deck rotted before it was parsed here:
#: nothing read them. The sentence lives in three committed files (the generator,
#: the canvas it writes, and the SVG the deck renders from it); the canvas and
#: the SVG are both checked, because "edited the generator, forgot `make
#: diagram`" has already happened twice in this history.
DIAGRAM = ROOT / "acr_architecture.excalidraw"
DIAGRAM_SVG = ROOT / "docs" / "assets" / "acr_architecture.preview.svg"
#: Matches both spellings the canvas uses — "TESTS · N py + N forge + …" on the
#: verification card and "N py · N forge · … — green" on the metrics card. Only
#: the separator differs.
SUITES_RE = re.compile(
    r"(\d+)\s*py\s*[·+]\s*(\d+)\s*forge\s*[·+]\s*(\d+)\s*terminal\s*[·+]\s*(\d+)\s*agent"
)


def diagram_suite_claims(path: Path) -> list[tuple[int, ...]]:
    """Every (py, forge, terminal, agent) tuple an artefact states.

    A list rather than a first match on purpose: two cards say this, and a card
    edited alone must report as a disagreement instead of hiding behind its twin.
    """
    if not path.exists():
        return []
    text = path.read_text()
    if path.suffix == ".excalidraw":
        doc = json.loads(text)
        text = "\n".join(
            e.get("text", "") for e in doc["elements"] if e.get("type") == "text"
        )
    return [tuple(int(g) for g in m) for m in SUITES_RE.findall(text)]


# --- the measurements -------------------------------------------------------


@cache
def measured_pytest() -> int | None:
    """Collected, not executed — cheap, and it counts the anvil-gated tests that
    SKIP on a machine without a node. Deliberately not `-q`: that suppresses the
    very summary line this needs."""
    out = run(["uv", "run", "pytest", "packages", "services", "tests",
               "-p", "no:cacheprovider", "--collect-only"])
    m = re.search(r"(\d+)\s+tests? collected", out)
    return int(m.group(1)) if m else None


@cache
def pytest_verdict() -> tuple[int, int] | None:
    """(passed, failed) from a REAL run — the question `--collect-only` cannot ask.

    `measured_pytest` counts COLLECTION, and that is the right number for the
    docs: it is stable across machines and it includes the anvil-gated tests
    that skip without a node. What it cannot do is notice that a test failed.
    The documents it gates say "**N passed**" and print a ✅, so for as long as
    this did not exist a fully red suite reported green on every count claim in
    every document — which is exactly what happened on 2026-09-10, when a
    genuinely broken test sat behind a green audit.

    Costly by construction, so CLAIMS_FAST skips it and says so rather than
    passing silently. In CI that is covered: the python job runs the suite
    itself and goes red on its own. The gap this closes is the local one, where
    a green audit is read as "the suite passes".
    """
    # NO `-q` HERE. pyproject's addopts already supplies one, so adding a
    # second makes it `-qq`, and `-qq` suppresses the very summary line this
    # parses — the run goes green, the parse finds nothing, and the check
    # reports "could not run it", which is indistinguishable from a missing
    # toolchain. Measured: that is exactly what happened on the first attempt.
    # ITS OWN BUDGET. `run`'s 900 s default was sized when this suite took ~336 s
    # (2026-09-28). It is now 565–805 s under `.venv/bin/python -m pytest` plus
    # ~60 s of `uv run` startup, so the measurement became MARGINAL: on
    # 2026-10-01 the same tree passed this gate twice and reported "could not
    # measure" three times, which sent me chasing a venv lock that did not
    # exist. A budget that trips on a healthy suite makes "could not measure"
    # meaningless, and the gate's whole value is that it means something.
    out = run(["uv", "run", "pytest", "packages", "services", "tests",
               "-p", "no:cacheprovider", "--tb=no"], timeout=2400)
    if out.startswith("__ERROR__"):
        LAST_FAILURE["python suite"] = out.removeprefix("__ERROR__ ").strip()
        return None
    failed = int(m.group(1)) if (m := re.search(r"(\d+) failed", out)) else 0
    passed = int(m.group(1)) if (m := re.search(r"(\d+) passed", out)) else 0
    errors = int(m.group(1)) if (m := re.search(r"(\d+) errors?", out)) else 0
    if passed == 0 and failed == 0 and errors == 0:
        return None  # could not read a summary at all — not "nothing failed"
    return passed, failed + errors


@cache
def measured_forge() -> int | None:
    """Plain `forge test` — `--summary` prints a table and moves the one-line
    total out of reach."""
    out = run(["forge", "test"], cwd=ROOT / "contracts")
    m = re.search(r"(\d+) tests? passed", out)
    return int(m.group(1)) if m else None


@cache
def measured_terminal() -> int | None:
    out = run(["npm", "test"], cwd=ROOT / "apps" / "terminal")
    # `node --test` reports TAP ("# pass 12") on node 20, which is what CI pins,
    # and the spec reporter ("i pass 12") on newer node. Accept both: matching
    # only CI's format meant a developer on a current runtime saw every terminal
    # claim reported as unmeasurable, which reads exactly like documentation
    # that has drifted — and the fix for that looks like editing the docs.
    m = re.search(r"^# pass (\d+)$", out, re.M) or re.search(r"^\D? ?pass (\d+)$", out, re.M)
    return int(m.group(1)) if m else None


@cache
def measured_glossary() -> int | None:
    out = run(["uv", "run", "python", "scripts/check_glossary_coverage.py"])
    m = re.search(r"all (\d+) uncommon diagram terms", out)
    return int(m.group(1)) if m else None


def measured_matchstick() -> int | None:
    """The subgraph mappings' own suite.

    A fifth measurement rather than a fifth column across seven docs: the file
    already argues that a claim should be parsed out of the docs rather than
    carried twice, and the same logic says one new checker beats one new number
    in every deck. Costly — `graph test` compiles WASM — so CLAIMS_FAST skips it.
    """
    out = run(["npx", "graph", "test"], cwd=ROOT / "graph")
    m = re.search(r"All (\d+) tests passed", out)
    return int(m.group(1)) if m else None


def measured_ci_jobs() -> int:
    """Jobs in ci.yml — top-level keys under `jobs:`, counted from the file so a
    new job shows up here without anyone remembering to say so."""
    text = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    body = text.split("\njobs:", 1)[-1]
    return len(re.findall(r"^  ([a-z][\w-]*):", body, re.M))


def measured_workflows() -> int:
    return len(list((ROOT / ".github" / "workflows").glob("*.yml")))


# --- the checks -------------------------------------------------------------


def main() -> None:
    sys.stdout.reconfigure(line_buffering=True)
    readme = README.read_text()

    print("ACR claim audit — does the README still tell the truth?")
    if FAST:
        print("  (CLAIMS_FAST — forge/terminal re-runs skipped; python count still measured)")

    print("\ntest counts (README · Measured numbers)")
    # `costly` decides what CLAIMS_FAST may skip. The python count is NOT
    # costly — `--collect-only` executes nothing — and skipping it once cost
    # the audit its sharpest check: CI ran green for days over a doc that said
    # 248 against a suite of 260. Only `forge test`, `npm test` and matchstick,
    # which genuinely re-run suites other CI jobs already ran, are skippable.
    measured_values: dict[str, int] = {}
    for label, pattern, measure, costly in (
        ("python suite", r"\*\*(\d+) py\*\*", measured_pytest, False),
        ("forge suite", r"\*\*(\d+) forge\*\*", measured_forge, True),
        ("terminal suite", r"\*\*(\d+) terminal\*\*", measured_terminal, True),
        ("subgraph suite", r"\*\*(\d+) matchstick\*\*", measured_matchstick, True),
        ("glossary", r"glossary \*\*(\d+)/\d+\*\*", measured_glossary, False),
    ):
        stated = claim(readme, pattern)
        if stated is None:
            check(False, f"{label}: no claim found in README — has it been reworded?")
            continue
        if FAST and costly:
            print(f"  · {label}: claims {stated} (not measured)")
            continue
        actual = measure()
        if actual is None:
            # Name WHICH failure. A timeout on a healthy suite and an absent
            # toolchain are different problems, and printing one sentence for
            # both is how a real stall gets read as a missing binary — the same
            # trap `git_lines` above was written to avoid.
            why = LAST_FAILURE.get(label) or "toolchain missing, or the output did not parse"
            check(False, f"{label}: could not measure — {why}")
            continue
        measured_values[label] = actual
        check(actual == stated, f"{label}: README says {stated}, measured {actual}")

    # Every OTHER place a doc states the python count. The loop above only reads
    # the README's own badge line, and that blind spot had already cost
    # something: `docs/BITTENSOR.md` carried "the 1651-test suite" through four
    # separate count bumps, staying wrong for the whole session, because no gate
    # looked outside the three sites a bump touches by hand. A number nobody
    # measures is a number that rots.
    py_actual = measured_values.get("python suite")
    if py_actual is None:
        print("  · stray python counts: not measured (no python count available)")
    else:
        strays = []
        for path in [README, *sorted((ROOT / "docs").rglob("*.md")),
                     *sorted((ROOT / "docs").rglob("*.svg"))]:
            for m in re.finditer(
                r"(\d{3,5})-test suite|(\d{3,5}) py\b", path.read_text()
            ):
                stated_stray = int(m.group(1) or m.group(2))
                if stated_stray != py_actual:
                    strays.append(
                        f"{path.relative_to(ROOT)} says {stated_stray}"
                    )
        check(
            not strays,
            "stray python counts: every mention outside the README badge agrees "
            f"with the measured {py_actual}"
            + (f" — but {', '.join(strays)}" if strays else ""),
        )

    # The question every count above is incapable of asking: does the suite
    # actually PASS? A count produced by `--collect-only` executes nothing, so
    # a red suite and a green one are identical to it.
    if FAST:
        print("  · python suite actually passes: not measured (CLAIMS_FAST)")
    else:
        verdict = pytest_verdict()
        if verdict is None:
            check(False, "python suite: could not run it to see whether it passes")
        else:
            passed, failed = verdict
            check(failed == 0,
                  f"python suite actually PASSES: {passed} passed, {failed} failed")

    print("\nthe architecture diagram (README calls it implementation-accurate)")
    canvas = diagram_suite_claims(DIAGRAM)
    svg = diagram_suite_claims(DIAGRAM_SVG)
    if not canvas:
        check(False, "diagram: no '<n> py · <n> forge · <n> terminal · <n> agent' "
                     "claim found — has a card been reworded?")
    else:
        # Both cheap, and both on even under CLAIMS_FAST: they catch a skipped
        # `make diagram`, which is how the SVG once kept 289 after the canvas
        # was fixed. Neither runs a suite.
        check(len(set(canvas)) == 1,
              f"diagram: its two cards agree with each other {sorted(set(canvas))}")
        check(sorted(set(svg)) == sorted(set(canvas)),
              f"diagram: docs/assets/*.svg matches the canvas (svg says {sorted(set(svg))})")
        py, forge, term, _agent = canvas[0]
        for label, stated, measure, costly in (
            ("diagram python count", py, measured_pytest, False),
            ("diagram forge count", forge, measured_forge, True),
            ("diagram terminal count", term, measured_terminal, True),
        ):
            if FAST and costly:
                print(f"  · {label}: claims {stated} (not measured)")
                continue
            actual = measure()
            if actual is None:
                check(False, f"{label}: could not measure (toolchain missing?)")
                continue
            check(actual == stated, f"{label}: diagram says {stated}, measured {actual}")

    print("\nthe receipts archive (the number a reader is most likely to spot-check)")
    # This once rotted to 11 in three places while the archive held 31, because
    # no check read the file. The ledger is the source; the README states it.
    ledger = ROOT / "services" / "index_api" / "index_api" / "receipts_live.jsonl"
    rows, payers = [], {}
    for ln in (ledger.read_text().splitlines() if ledger.exists() else []):
        if not ln.strip():
            continue
        try:
            who = str(json.loads(ln).get("payer", "")).lower()
        except Exception:
            continue
        rows.append(ln)
        payers[who] = payers.get(who, 0) + 1
    stated = claim(readme, r"\*\*(\d+)\*\* real Gateway x402 settlements")
    if stated is None:
        check(False, "receipts: no count found in README — has it been reworded?")
    else:
        check(stated == len(rows), f"receipts: README says {stated}, the archive holds {len(rows)}")
    stated_payers = claim(readme, r"from \*\*(\d+)(?:\*\*)? distinct payers")
    if stated_payers is None:
        check(False, "receipts: README states no payer count I can parse")
    else:
        check(stated_payers == len(payers),
              f"receipts: README says {stated_payers} payer(s), the archive has {len(payers)}")

    print("\nCI shape")
    stated_jobs = claim(readme, r"\*\*(\d+) jobs\*\*")
    jobs = measured_ci_jobs()
    if stated_jobs is None:
        check(False, "CI: no '**<n> jobs**' claim found in README")
    else:
        check(jobs == stated_jobs, f"CI jobs: README says {stated_jobs}, ci.yml defines {jobs}")
    check(
        "keeper" in readme,
        f"README says what keeps the venue alive ({measured_workflows()} workflow files exist)",
    )

    print("\naddresses named in the README match the live deploy")
    from acr_core import get_settings

    s = get_settings()
    for label, addr in (
        ("oracle", s.oracle_address),
        ("oracle v2", getattr(s, "oracle_v2_address", "")),
        ("registry", s.registry_address),
        ("futures venue", s.futures_address),
        ("feed-access attestor", s.attestor_address),
        ("receipt mirror", getattr(s, "receipt_mirror_address", "")),
        ("human-id mirror", getattr(s, "humanid_mirror_address", "")),
    ):
        if not addr:
            continue
        check(addr.lower() in readme.lower(), f"{label} {addr[:12]}… appears in the README")

    print("\nevidence integrity")
    # An on-chain identifier that differs from another by one character is a
    # typo in evidence — the worst kind, because it still looks like evidence.
    tokens: dict[str, set[str]] = {}
    scanned = [README] + [
        q
        for pattern in ("*.md", "*.svg")
        for q in sorted((ROOT / "docs").rglob(pattern))
    ]
    for path in scanned:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:  # pragma: no cover - unreadable file reports as no tokens
            continue
        for tok in re.findall(r"0x[0-9a-fA-F]{8,}", text):
            tokens.setdefault(tok.lower(), set()).add(str(path.relative_to(ROOT)))

    divergent: list[str] = []
    keys = sorted(tokens)
    for i, a in enumerate(keys):
        for b in keys[i + 1 :]:
            if len(a) != len(b):
                continue
            if sum(1 for x, y in zip(a, b, strict=True) if x != y) == 1:
                divergent.append(
                    f"{a[:14]}… ({', '.join(sorted(tokens[a]))}) vs "
                    f"{b[:14]}… ({', '.join(sorted(tokens[b]))})"
                )
    check(not divergent,
          f"no two on-chain identifiers in README or docs/ differ by one character "
          f"({len(keys)} scanned)")
    for d in divergent:
        print(f"      {d}")

    print()
    if _failures:
        print(f"claims: {len(_failures)} STALE — the README is ahead of, or behind, reality")
        for f in _failures:
            print(f"    ✗ {f}")
        sys.exit(1)
    print("claims: EVERY DOCUMENTED NUMBER STILL HOLDS")
    sys.exit(0)


if __name__ == "__main__":
    main()
