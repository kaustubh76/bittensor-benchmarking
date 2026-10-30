"""Seller-side concentration of Bittensor netuid 64, measured from the metagraph.

`ChutesSource` can publish a price from single-venue flow but not an attack cost,
and `acr_estimator.cleaning.clean` explains why: every sale on Chutes is one
aggregate buyer paying many sellers, the funding graph is a star, and a star has
one Louvain community. There is nothing for the sybil test or the cluster cap to
compare against, so `attack_cost_per_bp` is published as `None` rather than as
the cap's artefact. Checked and settled — `docs/BITTENSOR.md` §3 records that no
public endpoint joins chutes to the miners serving them, so this does not get
fixed by looking harder.

What *is* observable is the thing an attack cost is ultimately a claim about:
**how many independent sellers the venue actually has.** `/miner/metagraph` is
public, needs no key, and gives every netuid-64 hotkey with the coldkey that
controls it. One coldkey behind many hotkeys is one economic actor wearing
several faces, which is exactly the shape a sybil defence exists to find.

So this module publishes a concentration diagnostic, and the distinction from an
attack cost is load-bearing rather than pedantic:

* an **attack cost** says what an adversary must spend to move the print, and
  depends on cleaning arithmetic this tape cannot support;
* a **concentration diagnostic** says how concentrated the seller side is right
  now, is a direct measurement, and carries no claim about what moving the print
  would cost.

It is denominated in identities, which is the argument `acr_estimator.human_caps`
already makes about wallets — the fees are rounding error and the scarce input is
distinct identities. Here, for once, the identity structure is directly visible.

Measured 2026-10-29: 256 hotkeys, 207 coldkeys, 26 coldkeys controlling more than
one hotkey, the largest controlling 8. **Taostats independently reports 207
coldkeys for netuid 64** from a different source, which is the strongest
corroboration anything in this integration has.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

#: Committed offline input. Refresh with
#: ``uv run python scripts/bt_snapshot.py --capture-fixture``.
_DEFAULT_FIXTURE = (
    Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "bt" / "chutes_metagraph.json"
)

#: Chutes serves subnet 64. Asserted rather than assumed, because a metagraph for
#: some other netuid would describe a different venue entirely.
CHUTES_NETUID = 64

#: `ip` placeholders seen in the live payload. Measured 2026-10-29: 256 of 256
#: rows carry an `ip`, but only 9 distinct values and **248 of them are the
#: string "0"**. IP co-location is a standard second sybil signal and it is NOT
#: available here, so it is reported as unusable rather than quietly averaged
#: into a score that would then look better than its evidence.
_IP_PLACEHOLDERS = frozenset({"0", "0.0.0.0", "", "None", "null"})


@dataclass(frozen=True)
class VenueConcentration:
    """How concentrated the seller side of a subnet is. A measurement, not a bound."""

    netuid: int
    n_hotkeys: int
    n_coldkeys: int
    #: Coldkeys controlling more than one hotkey — the multi-identity actors.
    multi_hotkey_coldkeys: int
    #: Hotkeys controlled by the single largest coldkey.
    largest_cluster: int
    #: {hotkeys controlled: how many coldkeys control that many}.
    hotkeys_per_coldkey: dict[int, int] = field(default_factory=dict)
    #: Herfindahl index over coldkey hotkey-share, in [0, 1]. 1/n_coldkeys when
    #: every coldkey holds exactly one hotkey; 1.0 when one coldkey holds all.
    identity_hhi: float = 0.0
    #: Herfindahl index over stake. Reported separately because stake is far more
    #: concentrated than identity on this subnet, and the two tell different
    #: stories: many sellers, very little of the stake.
    stake_hhi: float = 0.0
    #: Hotkeys carrying any stake at all. Measured: 58 of 256.
    staked_hotkeys: int = 0
    #: False when the `ip` field is placeholder-filled and therefore useless as a
    #: co-location signal. Measured: False on live Chutes data.
    ip_usable: bool = False
    distinct_ips: int = 0

    @property
    def sybil_headroom(self) -> int:
        """Extra faces the existing multi-hotkey actors already wear.

        ``n_hotkeys - n_coldkeys``: the number of hotkeys that are *not* a fresh
        economic actor. Measured 49 of 256, so roughly one hotkey in five on this
        subnet is a second face rather than a second seller. Not a cost, and
        deliberately not called one.
        """
        return max(0, self.n_hotkeys - self.n_coldkeys)

    def summary(self) -> str:
        """One line for a scoreboard or a print's diagnostic field."""
        return (
            f"netuid {self.netuid}: {self.n_hotkeys} hotkeys / {self.n_coldkeys} coldkeys, "
            f"{self.multi_hotkey_coldkeys} multi-hotkey (largest {self.largest_cluster}), "
            f"identity HHI {self.identity_hhi:.4f}, stake HHI {self.stake_hhi:.4f}"
            f"{'' if self.ip_usable else ', ip unusable'}"
        )


def _hhi(weights: list[float]) -> float:
    """Herfindahl index: sum of squared shares. 0.0 on no weight at all."""
    total = sum(w for w in weights if w > 0)
    if total <= 0:
        return 0.0
    return sum((w / total) ** 2 for w in weights if w > 0)


def _netuid_of(row: dict) -> int:
    """A row's netuid, or -1 when it does not state one.

    Missing and zero are distinguished deliberately. ``int(row.get("netuid") or -1)``
    reads naturally and is wrong: ``0`` is falsy, so a row on **netuid 0 — the
    root network** collapses to -1 and is silently filtered out. The caller then
    gets a venue with no sellers rather than a measurement or a refusal, which is
    the worst of the three outcomes.
    """
    v = row.get("netuid")
    if v is None:
        return -1
    try:
        return int(v)
    except (TypeError, ValueError):
        return -1


def concentration(rows: list[dict], netuid: int = CHUTES_NETUID) -> VenueConcentration:
    """Measure seller concentration from raw `/miner/metagraph` rows.

    Rows for other netuids are dropped rather than merged: concentration is a
    property of one venue, and averaging two subnets would describe neither.
    """
    rows = [r for r in rows if _netuid_of(r) == netuid]
    if not rows:
        return VenueConcentration(
            netuid=netuid, n_hotkeys=0, n_coldkeys=0,
            multi_hotkey_coldkeys=0, largest_cluster=0,
        )

    hotkeys = {str(r.get("hotkey")) for r in rows if r.get("hotkey")}
    per_coldkey = Counter(str(r.get("coldkey")) for r in rows if r.get("coldkey"))
    counts = list(per_coldkey.values())

    # Stake by coldkey, not by hotkey: the question is how much economic weight
    # each ACTOR carries, and an actor's hotkeys pool into one position.
    stake_by_coldkey: dict[str, float] = {}
    staked = 0
    for r in rows:
        ck = str(r.get("coldkey") or "")
        st = float(r.get("stake") or 0.0)
        if st > 0:
            staked += 1
        if ck:
            stake_by_coldkey[ck] = stake_by_coldkey.get(ck, 0.0) + st

    ips = [str(r.get("ip")) for r in rows if r.get("ip") is not None]
    real_ips = {i for i in ips if i not in _IP_PLACEHOLDERS}
    # Usable only if most rows carry a distinct-ish real address. One shared IP
    # across 248 of 256 rows is a redaction, not a co-location finding.
    ip_usable = len(real_ips) > 1 and len(real_ips) >= 0.5 * len(hotkeys)

    return VenueConcentration(
        netuid=netuid,
        n_hotkeys=len(hotkeys),
        n_coldkeys=len(per_coldkey),
        multi_hotkey_coldkeys=sum(1 for c in counts if c > 1),
        largest_cluster=max(counts) if counts else 0,
        hotkeys_per_coldkey=dict(sorted(Counter(counts).items())),
        identity_hhi=_hhi([float(c) for c in counts]),
        stake_hhi=_hhi(list(stake_by_coldkey.values())),
        staked_hotkeys=staked,
        ip_usable=ip_usable,
        distinct_ips=len(real_ips),
    )


def load_metagraph(path: Path | str | None = None) -> list[dict]:
    """Read the committed metagraph fixture. Missing file is an empty list."""
    p = Path(path) if path is not None else _DEFAULT_FIXTURE
    if not p.exists():
        return []
    # Tolerant for the same reason `load_gpu_counts` is: this raises from OUTSIDE
    # the caller's per-index try, so a corrupt fixture took down the whole
    # process rather than degrading one diagnostic. An unreadable metagraph means
    # "no concentration data", which `venue_concentration` already handles by
    # returning None and refusing to publish an attack cost.
    try:
        data = json.loads(p.read_text())
    except (OSError, ValueError):  # pragma: no cover - a corrupt fixture
        return []
    return data if isinstance(data, list) else []


def venue_concentration(
    path: Path | str | None = None, netuid: int = CHUTES_NETUID
) -> VenueConcentration | None:
    """The diagnostic, from the offline fixture. ``None`` when there is no data."""
    rows = load_metagraph(path)
    if not rows:
        return None
    return concentration(rows, netuid=netuid)
