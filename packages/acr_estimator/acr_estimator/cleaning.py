"""Cleaning Stack — the contamination filter that precedes estimation.

The estimator assumes contaminated data. Four defenses, all computed on the
funding graph (who paid whom, weighted by USDC notional):

  1. self-dealing   — buyer == seller.
  2. wash cycles    — reciprocal funding (A pays B *and* B pays A): a ring
                      whose net on-chain flow is ~zero but which prints volume.
  3. sybil clusters — small, self-contained communities (Louvain) that trade
                      mostly with themselves rather than the wider market.
  4. cluster caps   — no single community may contribute more than a fixed
                      fraction of window weight, bounding any one actor's pull.

The output is a per-event weight vector (zeroed for excluded flow, scaled down
for capped clusters) plus the diagnostic sets the manipulation bound consumes.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

import networkx as nx
import numpy as np
from acr_core import TapeEvent

#: A community is flagged sybil if it is small and this share of the notional on
#: edges *touching* it stays internal (``I / (I + B)``, each edge counted once).
SYBIL_INTERNAL_RATIO = 0.6
SYBIL_MAX_SIZE = 40

#: Bumped whenever the cleaning ALGORITHM changes in a way that alters which
#: events survive, even if no parameter moved. Without it a code change would
#: keep the same policy hash and a re-derivation would silently disagree with
#: the keeper for reasons the hash claimed were impossible.
#:
#: 2 (2026-09-06): the estimator seed joined the hash. It was always part of the
#: rule — Louvain's partition depends on it, and the partition decides which
#: events are excluded — but it was a hidden default argument, so the hash
#: claimed to cover something it did not. Nothing on chain is orphaned by the
#: bump: the 131 backfilled prints carry BACKFILL_POLICY (a fixed sentinel in
#: scripts/backfill_oracle_v2.py, never this hash), and a live v2 print
#: legitimately carries the hash of the policy it was actually made under.
#: ACROracleV2 stores policyHash per print, so a verifier can always tell which
#: rule applied to which print.
POLICY_VERSION = 2


def policy_hash(settings=None) -> str:
    """A stable digest of the cleaning policy — what makes it re-derivable.

    "The keeper decides what is wash" is the fair objection to any cleaned
    benchmark. The answer is not to ask for trust but to publish the rule: every
    print carries this hash, and ``make recompute`` (scripts/recompute.py) re-runs
    the same stack from on-chain funding edges and reports whether its exclusions
    match. That argument only holds if the hash covers everything that can change
    an exclusion — so it spans the tunable parameters AND the algorithm version,
    and is canonically ordered so two machines agree byte for byte.
    """
    from acr_core import get_settings

    s = settings or get_settings()
    policy = {
        "version": POLICY_VERSION,
        "sybil_internal_ratio": SYBIL_INTERNAL_RATIO,
        "sybil_max_size": SYBIL_MAX_SIZE,
        "cluster_volume_cap": s.cluster_volume_cap,
        "trim_alpha": s.trim_alpha,
        "estimator_seed": s.estimator_seed,
    }
    canonical = json.dumps(policy, sort_keys=True, separators=(",", ":"))
    return "0x" + hashlib.sha256(canonical.encode()).hexdigest()


@dataclass
class CleaningResult:
    events: list[TapeEvent]
    #: Per-event weight aligned to ``events`` (0 = excluded).
    weights: np.ndarray
    flagged_self_dealing: set[str] = field(default_factory=set)
    flagged_wash: set[str] = field(default_factory=set)
    flagged_sybil: set[str] = field(default_factory=set)
    communities: list[set[str]] = field(default_factory=list)
    sybil_communities: list[set[str]] = field(default_factory=list)
    #: Fraction of raw notional removed by cleaning (excluded + cap-scaled).
    removed_fraction: float = 0.0
    #: Fraction zeroed by the exclusion filters (self-deal/wash/sybil).
    excluded_fraction: float = 0.0
    #: Fraction scaled away by the per-cluster volume caps.
    cap_scaled_fraction: float = 0.0
    #: True when the funding graph collapsed to a single community, so neither
    #: the sybil test nor the cluster cap had anything to compare against. Set
    #: only on the ``single_venue`` path; the default path never reaches here
    #: because it zeroes or caps the window first. See ``clean``.
    degenerate_graph: bool = False

    @property
    def kept_mask(self) -> np.ndarray:
        return self.weights > 0

    @property
    def all_flagged(self) -> set[str]:
        return self.flagged_self_dealing | self.flagged_wash | self.flagged_sybil


def _funding_graphs(events: list[TapeEvent]) -> tuple[nx.DiGraph, nx.Graph]:
    """Directed funding graph (for cycles) + undirected weighted (for Louvain)."""
    di = nx.DiGraph()
    un = nx.Graph()
    for e in events:
        if di.has_edge(e.buyer, e.seller):
            di[e.buyer][e.seller]["weight"] += e.notional
        else:
            di.add_edge(e.buyer, e.seller, weight=e.notional)
        if un.has_edge(e.buyer, e.seller):
            un[e.buyer][e.seller]["weight"] += e.notional
        else:
            un.add_edge(e.buyer, e.seller, weight=e.notional)
    return di, un


def _detect_sybil_communities(
    un: nx.Graph, communities: list[set[str]], node_comm: dict[str, int]
) -> list[set[str]]:
    """Flag small communities whose notional stays mostly internal.

    One pass over the undirected edges accumulates, per community c, the internal
    weight ``I_c`` (both endpoints in c) and the boundary weight ``B_c`` (exactly
    one endpoint in c). The sybil score is ``I_c / (I_c + B_c)`` — each edge
    counted once, so a purely-internal cluster scores 1.0 and a community that
    trades half externally scores 0.5 (correctly *below* the threshold).
    """
    internal: dict[int, float] = {}
    boundary: dict[int, float] = {}
    for u, v, data in un.edges(data=True):
        w = data.get("weight", 0.0)
        cu, cv = node_comm[u], node_comm[v]
        if cu == cv:
            internal[cu] = internal.get(cu, 0.0) + w
        else:
            boundary[cu] = boundary.get(cu, 0.0) + w
            boundary[cv] = boundary.get(cv, 0.0) + w

    sybil: list[set[str]] = []
    for i, comm in enumerate(communities):
        if len(comm) > SYBIL_MAX_SIZE:
            continue
        i_c = internal.get(i, 0.0)
        denom = i_c + boundary.get(i, 0.0)
        if denom > 0 and (i_c / denom) >= SYBIL_INTERNAL_RATIO:
            sybil.append(comm)
    return sybil


class DegenerateGraphError(ValueError):
    """The funding graph has one community, so cleaning measured nothing.

    Raised instead of letting the window fall through to ``robust.estimate``,
    which reports "cannot estimate from zero observations" two modules away from
    the actual cause. A ``ValueError`` subclass on purpose, so callers that
    already catch ``ValueError`` (``index_api.store.refresh``) keep working.
    """


def clean(
    events: list[TapeEvent],
    cluster_cap: float = 0.05,
    seed: int = 0,
    single_venue: bool = False,
) -> CleaningResult:
    """Zero the excludable flow and cap any one cluster's share of the window.

    ``single_venue`` changes the rule for a window whose funding graph has **one
    community**, which is what a single-venue tape looks like: every sale is one
    aggregate buyer paying many sellers, so the graph is a star and Louvain
    returns one community containing all of it.

    On the default path such a window is destroyed two different ways, and which
    one fires depends on crossing ``SYBIL_MAX_SIZE``:

    * ``> 41`` nodes — the community is too big to be flagged sybil, but it is
      also the only community, so the cluster cap scales **every** weight to
      ``cluster_cap``. Measured: a 170-node star keeps 5% of its notional, and
      the manipulation bound is understated ~20x because ``bound.py`` searches
      against post-cap weight while counting identities against raw.
    * ``<= 41`` nodes — the community is small enough to test, has no boundary
      edges by construction, so ``I/(I+B) == 1.0`` clears
      ``SYBIL_INTERNAL_RATIO`` and the **entire window is flagged sybil and
      zeroed**.

    Neither is a defence. Both are a structural test being handed a population
    with no structure and reporting the artefact. With ``single_venue=True`` the
    sybil exclusion and the cluster cap are skipped for a one-community window
    and ``degenerate_graph`` is set, so the caller can publish a price while
    refusing to publish an attack cost — which is the honest pair, because an
    attack cost is a claim about graph diversity and there is no graph here.

    Self-dealing and reciprocal wash exclusion still apply: those are per-edge
    tests and stay meaningful on a star.
    """
    if not events:
        return CleaningResult(events=[], weights=np.empty(0))

    weights = np.array([e.notional for e in events], dtype=float)
    total = float(weights.sum())
    di, un = _funding_graphs(events)

    # Community structure via Louvain on the weighted funding graph.
    try:
        communities = [
            set(c) for c in nx.community.louvain_communities(un, weight="weight", seed=seed)
        ]
    except Exception:  # pragma: no cover - fallback if backend missing
        communities = [set(c) for c in nx.connected_components(un)]
    node_comm = {n: i for i, c in enumerate(communities) for n in c}

    # A one-community graph has nothing to compare a community against. Under
    # the single-venue policy, say so and disarm the two self-referential
    # stages rather than reporting their artefact as a defence.
    degenerate = single_venue and len(communities) <= 1
    if degenerate:
        sybil_comms: list[set[str]] = []
        sybil_nodes: set[str] = set()
    else:
        sybil_comms = _detect_sybil_communities(un, communities, node_comm)
        sybil_nodes = set().union(*sybil_comms) if sybil_comms else set()

    # Reciprocal (wash-cycle) pairs.
    reciprocal: set[frozenset[str]] = set()
    for u, v in di.edges():
        if di.has_edge(v, u):
            reciprocal.add(frozenset((u, v)))

    fd_self: set[str] = set()
    fd_wash: set[str] = set()
    fd_sybil: set[str] = set()

    for i, e in enumerate(events):
        excluded = False
        if e.buyer == e.seller:
            fd_self.add(e.event_id)
            excluded = True
        elif frozenset((e.buyer, e.seller)) in reciprocal:
            fd_wash.add(e.event_id)
            excluded = True
        elif e.buyer in sybil_nodes and e.seller in sybil_nodes:
            fd_sybil.add(e.event_id)
            excluded = True
        if excluded:
            weights[i] = 0.0

    excluded_weight = total - float(weights.sum())

    # Per-cluster volume caps on the survivors. A community's weight is the
    # notional it touches as *either* buyer or seller, so a buyer cluster that
    # funnels flow through many disjoint sellers is capped too (the seller-only
    # version missed this). Each event is scaled by the tighter of its two
    # endpoints' community caps.
    if total > 0 and cluster_cap < 1.0 and not degenerate:
        cap = cluster_cap * total
        comm_weight: dict[int, float] = {}
        for i, e in enumerate(events):
            if weights[i] <= 0:
                continue
            cs = node_comm.get(e.seller, -1)
            cb = node_comm.get(e.buyer, -1)
            comm_weight[cs] = comm_weight.get(cs, 0.0) + weights[i]
            if cb != cs:
                comm_weight[cb] = comm_weight.get(cb, 0.0) + weights[i]
        scale = {c: (cap / w if w > cap else 1.0) for c, w in comm_weight.items()}
        for i, e in enumerate(events):
            if weights[i] <= 0:
                continue
            f = min(
                scale.get(node_comm.get(e.seller, -1), 1.0),
                scale.get(node_comm.get(e.buyer, -1), 1.0),
            )
            if f < 1.0:
                weights[i] *= f

    surviving = float(weights.sum())
    excluded_fraction = excluded_weight / total if total > 0 else 0.0
    removed = 1.0 - (surviving / total if total > 0 else 0.0)
    cap_scaled_fraction = max(0.0, removed - excluded_fraction)
    return CleaningResult(
        events=events,
        weights=weights,
        flagged_self_dealing=fd_self,
        flagged_wash=fd_wash,
        flagged_sybil=fd_sybil,
        communities=communities,
        sybil_communities=sybil_comms,
        removed_fraction=removed,
        excluded_fraction=excluded_fraction,
        cap_scaled_fraction=cap_scaled_fraction,
        degenerate_graph=degenerate,
    )
