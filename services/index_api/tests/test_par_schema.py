"""The documented shape of `GET /par` must be the shape it returns.

WHY A SEPARATE GATE IS NEEDED AT ALL. `ParCheckOut` is attached through
`responses=` rather than `response_model=`, deliberately: a `response_model`
makes FastAPI serialize the answer THROUGH the class, so any field the class
forgot would vanish from live responses — a documentation change that silently
breaks the terminal and every integrator at once. The cost of not filtering is
that nothing makes the description true. This is that something.

It checks BOTH directions, because each failure is real and they are different:
a key the route returns and the schema omits leaves an integrator unable to find
a field that is there; a key the schema declares and the route never sends sends
them looking for one that is not.

`/par` is the only route in this service with a documented response, and that is
the point rather than an inconsistency: it is the one surface a stranger can
exercise with no key, no card and no payment, and before 2026-10-12 there was no
document anywhere that said what comes back.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from index_api.app import ParBasketOut, ParCheckOut, app

client = TestClient(app)

UNIT = "$/1k tokens"


def _body(**over):
    q = {"unit": UNIT, "billed_usdc": 0.02, "quantity": 10}
    q.update(over)
    r = client.get("/par", params=q)
    assert r.status_code == 200, r.text
    return r.json()


def test_every_key_par_returns_is_declared_in_its_schema():
    body = _body()
    declared = set(ParCheckOut.model_fields)
    returned = set(body)

    assert returned - declared == set(), (
        f"GET /par returns {sorted(returned - declared)}, which `ParCheckOut` does not "
        "declare. An integrator reading the generated reference cannot find a field "
        "that is actually there."
    )
    assert declared - returned == set(), (
        f"`ParCheckOut` declares {sorted(declared - returned)}, which GET /par does not "
        "return. That sends a reader looking for a field that does not exist."
    )


def test_every_basket_key_is_declared_too():
    """The nested object is where the new fields went, so it gets its own check
    rather than riding on the parent's."""
    basket = _body()["basket"]
    declared = set(ParBasketOut.model_fields)
    returned = set(basket)
    assert returned == declared, (
        f"basket drift — only in the response: {sorted(returned - declared)}; "
        f"only in the schema: {sorted(declared - returned)}"
    )


@pytest.mark.parametrize("field", ["age_s", "max_age_s", "warn_after_s", "status"])
def test_the_fields_a_reader_needs_to_judge_the_basket_are_in_the_schema(field: str):
    """Named one by one so a failure says WHICH of them went missing.

    These four are the difference between a basket a reader can judge and a
    date they have to guess about: the age, the two marks it is measured
    against, and the word that says which band it is in.
    """
    assert field in ParBasketOut.model_fields


def test_the_schema_does_not_filter_the_live_response():
    """The whole reason for `responses=` over `response_model=`.

    If someone 'tidies' this into `response_model=ParCheckOut`, the nested
    `par`/`verdict`/`would` dicts keep working (they are `dict`) but any field
    added to the route and not to the class starts disappearing. This asserts
    the route is NOT wired that way, so the tidy-up is caught here rather than
    by an integrator finding a field gone.
    """
    from pydantic import BaseModel

    route = next(r for r in app.routes if getattr(r, "path", None) == "/par")
    rm = getattr(route, "response_model", None)

    # NOT `is None`. FastAPI INFERS a response_model from the return annotation,
    # and `price_check` is annotated `-> dict`, so this is `<class 'dict'>` — a
    # bare dict serializes every key and filters nothing, which is the state we
    # want. Asserting None here failed on correct code, which is worth leaving
    # written down: the property that matters is not "no model" but "not a model
    # that drops fields".
    assert rm is not ParCheckOut, (
        "/par now serializes THROUGH ParCheckOut, so any field the class does not "
        "declare is silently dropped from live responses. Document it with "
        "`responses={200: {'model': ParCheckOut}}` instead; this file is what keeps "
        "the description honest without putting it in the serialization path."
    )
    assert not (isinstance(rm, type) and issubclass(rm, BaseModel)), (
        f"/par serializes through {rm!r}, a Pydantic model, which filters unknown keys"
    )

    # And the documented schema really did reach the spec, which is the whole
    # point of attaching it at all.
    assert "ParCheckOut" in str(app.openapi()), (
        "ParCheckOut is not in app.openapi(), so the generated reference documents "
        "nothing about what /par returns"
    )
