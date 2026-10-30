"""Every GraphQL query in the repo, checked against the subgraph schema.

These strings are the one part of the Graph integration nothing else validates.
`graph build` checks the mappings, not the queries that read them; both the TCA
and proxy test suites monkeypatch the transport out; and a field name that does
not exist does not raise — the endpoint answers 200 with an `errors` array, the
client turns that into an empty result, and the surface reports "the subgraph
did not answer", which is indistinguishable from an outage.

So a typo here is invisible until someone reads a dashboard that has been quietly
empty. This parses `graph/schema.graphql` and asserts that every selected field,
every `where` key and every `orderBy` in every query resolves. It found two real
bugs the day it was written: a `series { … }` sub-selection on an entity that
had no such relation, and an `orderBy` naming a field that did not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = ROOT / "graph" / "schema.graphql"

#: Every file that embeds a GraphQL query string.
QUERY_SOURCES = (
    ROOT / "packages" / "acr_tape" / "acr_tape" / "graph_source.py",
    ROOT / "services" / "index_api" / "index_api" / "tca.py",
    ROOT / "services" / "index_api" / "index_api" / "graph_proxy.py",
)


def _entities() -> dict[str, dict[str, str]]:
    text = SCHEMA.read_text()
    text = re.sub(r'""".*?"""', "", text, flags=re.S)   # block docstrings
    text = re.sub(r'"[^"\n]*"', "", text)                # inline docstrings
    text = re.sub(r"#.*", "", text)                      # comments
    out: dict[str, dict[str, str]] = {}
    for m in re.finditer(r"type\s+(\w+)\s+@\w+\([^)]*\)\s*\{(.*?)\n\}", text, re.S):
        out[m.group(1)] = {
            fm.group(1): fm.group(2)
            for fm in re.finditer(r"^\s*(\w+)\s*:\s*([\[\]\w!]+)", m.group(2), re.M)
        }
    return out


def _roots(types: dict) -> dict[str, str | None]:
    """graph-node's generated query roots: `entity` and its plural."""
    roots: dict[str, str | None] = {"_meta": None}
    for t in types:
        lower = t[0].lower() + t[1:]
        roots[lower] = t
        roots[lower + ("es" if lower.endswith("s") else "s")] = t
    return roots


def _balanced(text: str, open_at: int, pair: str = "{}") -> int:
    depth, i = 0, open_at
    while i < len(text):
        if text[i] in pair[0] + ("(" if pair == "{}" else ""):
            depth += 1
        elif text[i] in pair[1] + (")" if pair == "{}" else ""):
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(text) - 1


def _check_selection(types, entity, body, where, problems):
    nested: list[tuple[str, str]] = []

    def eat(m):
        nested.append((m.group(1), m.group(2)))
        return " "

    flat = re.sub(r"(\w+)\s*\{([^{}]*)\}", eat, body)
    for token in re.findall(r"\b([a-zA-Z_]\w*)\b", flat):
        if token not in types.get(entity, {}):
            problems.append(f"{where}: {entity} has no field {token!r}")
    for sub, sub_body in nested:
        sub_type = types.get(entity, {}).get(sub, "").strip("[]!")
        if sub_type in types:
            _check_selection(types, sub_type, sub_body, where, problems)
        elif sub not in types.get(entity, {}):
            problems.append(f"{where}: {entity} has no field {sub!r}")


def _problems() -> list[str]:
    types = _entities()
    roots = _roots(types)
    problems: list[str] = []
    for path in QUERY_SOURCES:
        text = path.read_text()
        label = path.name
        # selection sets
        for rm in re.finditer(r"\b(\w+)\s*(?:\([^{]*?\))?\s*\{", text):
            if rm.group(1) not in roots:
                continue
            entity = roots[rm.group(1)]
            if entity is None:
                continue
            i = text.index("{", rm.end() - 1)
            _check_selection(types, entity, text[i + 1 : _balanced(text, i)],
                             f"{label}:{rm.group(1)}", problems)
        # where / orderBy
        for m in re.finditer(r"\b([a-zA-Z_]\w*)\s*\(", text):
            entity = roots.get(m.group(1))
            if not entity:
                continue
            args = text[m.end() : _balanced(text, m.end() - 1, "()")]
            for om in re.finditer(r"orderBy:\s*(\w+)", args):
                if om.group(1) not in types[entity]:
                    problems.append(
                        f"{label}:{m.group(1)}: cannot orderBy {om.group(1)!r} — "
                        f"{entity} has no such field"
                    )
            wm = re.search(r"where:\s*\{(.*?)\}", args, re.S)
            if wm:
                for km in re.finditer(r"(\w+?)(_gte|_lte|_gt|_lt|_in|_not)?\s*:", wm.group(1)):
                    if km.group(1) not in types[entity]:
                        problems.append(
                            f"{label}:{m.group(1)}: cannot filter on {km.group(1)!r} — "
                            f"{entity} has no such field"
                        )
    return problems


@pytest.mark.skipif(not SCHEMA.exists(), reason="subgraph schema not present")
def test_every_queried_field_exists_in_the_schema():
    problems = _problems()
    assert not problems, "queries reference fields the schema does not have:\n  " + "\n  ".join(
        problems
    )


@pytest.mark.skipif(not SCHEMA.exists(), reason="subgraph schema not present")
def test_the_checker_actually_reaches_the_queries():
    """A validator that silently matched nothing would pass forever.

    This is the guard against the check quietly becoming a no-op — the same
    failure the repo's claim audit exists to prevent one level up.
    """
    types = _entities()
    assert len(types) >= 14, f"only parsed {len(types)} entities from the schema"
    seen = 0
    roots = _roots(types)
    for path in QUERY_SOURCES:
        text = path.read_text()
        seen += sum(1 for m in re.finditer(r"\b(\w+)\s*\(", text) if m.group(1) in roots)
    assert seen >= 12, f"only found {seen} root queries across {len(QUERY_SOURCES)} files"


def test_every_required_variable_is_either_supplied_or_demanded():
    """A query's declared variables and the proxy's contract must agree.

    The gap this closes: `prints` declares `$index: String!`, `run` only ever
    guarantees `first`, and a caller who omitted `index` got the subgraph's
    "No value provided for required variable" turned into `the subgraph did not
    answer` — the OUTAGE message. The reader is then debugging our indexer
    instead of their own request. Every operation must either need nothing
    beyond `first`, or say precisely what it needs.
    """
    from index_api.graph_proxy import OPERATIONS, required_vars

    known = {"index", "seller", "payer"}
    for name, query in OPERATIONS.items():
        needed = required_vars(query)
        assert set(needed) <= known, (
            f"{name} requires {needed}; a variable outside {sorted(known)} has no "
            "documented way for a caller to supply it"
        )
        # `first` is supplied by run() and must never be demanded of a caller.
        assert "first" not in needed


def test_a_missing_variable_is_reported_as_the_callers_problem():
    from index_api import graph_proxy

    for op in ("prints", "economicPrints", "sellerDays", "payerDays"):
        out = graph_proxy.run(op, {})
        assert out["available"] is False
        # Names the variable, and does NOT blame the subgraph.
        assert "needs variable" in out["reason"], out
        assert out["required"], out
        assert "did not answer" not in out["reason"]


def test_an_operation_that_needs_nothing_is_not_blocked(monkeypatch):
    """The guard must not turn a fully-specified call into a refusal."""
    from index_api import graph_proxy

    monkeypatch.setattr(graph_proxy, "graph_query", lambda *a, **k: {"sellers": []})
    monkeypatch.setattr(
        graph_proxy, "get_settings", lambda: type("S", (), {"subgraph_url": "http://x", "graph_api_key": ""})()
    )
    assert graph_proxy.run("sellers", {})["available"] is True
    assert graph_proxy.run("prints", {"index": "ACR-INF"})["available"] is True
