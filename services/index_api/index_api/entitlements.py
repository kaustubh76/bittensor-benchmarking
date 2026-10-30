"""Bills a vendor sent us, on a register the cadence can read by itself.

`obligations_from_entitlements` has existed, with sixteen tests, since the
agoranomoi work: it turns "fifty seats billed, twelve opened" into an
obligation carrying the independent count that judges it. **Nothing could call
it without a human.** Its only caller was `scripts/operator_run.py
--entitlements <file>` — no HTTP route, no Makefile target, not imported by
`operator_keeper`. So the half of the product that reads invoices was the half
that needed somebody to type.

That is not a cosmetic gap. It is why three things are structurally impossible
on the autonomous path:

    check 7, the timing     `obligations_for` never sets `due_at`; only this
                            feeder does. `due_at` is absent on all nineteen
                            archived rows and `hold` has never been written.
    settled_on_time         0 of 0 for the same single reason — RFB 4 asks for
                            "obligations settled on time without a human
                            touching them" and the denominator is empty.
    the duplicate check     `is_duplicate` matches on id OR `invoice_ref`, and
                            only this feeder supplies an `invoice_ref`.

Plus `early_pay_discount`, which is the brief's "worth paying early for the
discount" and is read nowhere else.

WHY A COMMITTED REGISTER AND NOT AN HTTP POST. An invoice is a claim that money
is owed. Accepting one over an endpoint would let whoever can reach the
endpoint mint an obligation the agent may then pay, which is the same mistake as
putting the budget in the prompt instead of the contract. This repo already
onboards a business by git commit (`businesses.py` says so outright) and an
agreement by git commit (`operator_commitments.jsonl`). A bill belongs in the
same place, under review, for the same reason.

Two tiers, the same shape as the decision log and the commitment register and
for the same reason: ``data/`` is untracked AND in ``.dockerignore``, so a
register written only there is present in development and absent in production.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger("acr.entitlements")

#: The committed register, inside the package, and the runtime one beside it.
ARCHIVE_PATH = Path(__file__).with_name("operator_entitlements.jsonl")
LOG_PATH = os.environ.get("ACR_ENTITLEMENTS_PATH", "data/operator_entitlements.jsonl")


def _read_one(target: Path) -> list[dict]:
    """One JSONL file's rows, tolerantly — a missing file is an empty list.

    The same shape as `commitments._read_one`, including skipping a malformed
    line rather than treating it as the end of the file: an interrupted write
    leaves half a line, and reading that as EOF would silently drop every bill
    behind it.
    """
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except Exception as exc:  # noqa: BLE001 — an unreadable register is not a crash
        log.warning("entitlements: %s unreadable (%s)", target.name, exc)
        return []
    rows: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except ValueError:
            log.warning("entitlements: skipping a malformed line in %s", target.name)
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def load(path: str | Path | None = None) -> tuple[dict, ...]:
    """Every bill on the register: the committed one, then the runtime one.

    An explicit ``path`` reads only that file, which is what a test wants.
    Rows are returned as read — validation belongs to
    `operator.obligations_from_entitlements`, which already drops a row with no
    payee, no resource or a non-positive amount, and is tested for it. Doing it
    twice in two places is how the two drift apart.
    """
    if path is not None:
        return tuple(_read_one(Path(path)))
    return tuple(_read_one(ARCHIVE_PATH) + _read_one(Path(LOG_PATH)))


def for_business(slug: str, path: str | Path | None = None) -> list[dict]:
    """The bills addressed to one business.

    Keyed on a ``business`` field per row, the way `operator_commitments.jsonl`
    is, so one register serves every tenant and onboarding a second is a commit
    rather than a deploy. A row with no ``business`` is dropped rather than
    treated as everyone's: a bill that does not say who owes it is exactly the
    row that should not reach a wallet.
    """
    if not slug:
        return []
    want = slug.strip().lower()
    return [
        row
        for row in load(path)
        if str(row.get("business") or "").strip().lower() == want
    ]
