"""Every name a package promises in `__all__` must actually resolve.

This exists because it did not. Removing the World integration deleted
`acr_oracle_client/humanid.py`, and six names it had provided stayed behind in
`acr_oracle_client.__all__`: `RATING_WINDOW_S`, `cluster_id`, `salt_commitment`,
`window_of`, `current_window` and `as_bytes32`. The package imported fine, every
`from acr_oracle_client import X` for a surviving name worked, the whole suite
was green, and `make ci` exited 0 — because **nothing in this repo does
`from <package> import *`**, which is the only operation that reads `__all__`.

So `__all__` was a contract with no counterparty. The failure it produced,
`AttributeError: module 'acr_oracle_client' has no attribute 'RATING_WINDOW_S'`,
would have landed on the first outside caller to write the one line we never
write ourselves.

The check is deliberately over ALL packages rather than the one that broke. A
test named after a single incident catches that incident again; this catches the
next deletion in any of the six. It is also why the assertion reports every
missing name at once instead of failing on the first — a half-finished removal
usually orphans a cluster of names, and fixing them one CI run at a time is how
a five-minute edit becomes an afternoon.
"""

from __future__ import annotations

import importlib

import pytest

#: Every first-party package with a public surface. Hard-coded rather than
#: discovered, so a new package has to be added here on purpose — a glob would
#: silently cover nothing if the layout changed.
PACKAGES = (
    "acr_core",
    "acr_estimator",
    "acr_instrument",
    "acr_oracle_client",
    "acr_sim",
    "acr_subnet",
    "acr_tape",
)


@pytest.mark.parametrize("name", PACKAGES)
def test_every_name_in_all_resolves(name: str) -> None:
    mod = importlib.import_module(name)
    declared = getattr(mod, "__all__", None)
    assert declared is not None, f"{name} declares no __all__"

    missing = [n for n in declared if not hasattr(mod, n)]
    assert not missing, (
        f"{name}.__all__ promises {len(missing)} name(s) that do not resolve: "
        f"{', '.join(missing)} — `from {name} import *` raises AttributeError"
    )


@pytest.mark.parametrize("name", PACKAGES)
def test_star_import_actually_works(name: str) -> None:
    """The operation `__all__` exists for, performed once per package.

    `test_every_name_in_all_resolves` is the useful diagnostic — it names what
    is missing. This is the end-to-end proof, and it is here because the
    diagnostic version could itself drift from what the interpreter does (for
    instance if `__all__` ever held a non-string, which `hasattr` would reject
    for a different reason than the import machinery does).
    """
    ns: dict[str, object] = {}
    exec(f"from {name} import *", ns)  # noqa: S102 - the behaviour under test


@pytest.mark.parametrize("name", PACKAGES)
def test_all_has_no_duplicates(name: str) -> None:
    """A name listed twice is a merge artefact, and it is the shape a bad rebase
    leaves when two branches both add to the same export list."""
    declared = list(getattr(importlib.import_module(name), "__all__", ()))
    dupes = sorted({n for n in declared if declared.count(n) > 1})
    assert not dupes, f"{name}.__all__ lists {', '.join(dupes)} more than once"
