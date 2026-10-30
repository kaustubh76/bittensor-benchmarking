"""Pytest bootstrap — keep the suite hermetic and credential-free.

Tests must not depend on a developer's local ``.env`` (Circle API keys, Arc RPC,
x402 facilitator URL, …). We disable dotenv loading and strip any ``ACR_*`` env
vars before collection, so every path-selection (sim vs arc tape, dev vs Circle
facilitator, raw-key vs Circle signer) resolves to the offline default unless a
test sets it explicitly via ``monkeypatch.setenv`` + ``reset_settings()``.
"""

from __future__ import annotations

import os
import pathlib
import tempfile

# Single-threaded BLAS, set before numpy is imported anywhere.
#
# Two LAPACK paths sit on the hot path, not one: `np.linalg.solve` in the RTS
# smoother (observation_model.py), on a structurally near-singular k x k system —
# Q is rank-1 and F is a shift matrix, so the conditioning is dominated by an
# absolute 1e-12 jitter — and, larger, the SVD behind statsmodels' WLS in
# hedonic.py, which factorises a ~2500 x 5 design on every print.
# Multithreaded LAPACK blocks the factorisation
# differently per thread count, which changes the summation order and therefore
# the low bits. A suite that pins estimator output to a literal has to fix that,
# or the golden file fails on a machine with a different core count for reasons
# that have nothing to do with the engine.
for _var in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ.setdefault(_var, "1")


def pytest_configure(config) -> None:  # noqa: ARG001 - pytest hook signature
    from acr_core import reset_settings
    from acr_core.config import ACRSettings

    ACRSettings.model_config["env_file"] = None
    # ACR_TEST_RPC survives the scrub: it names WHICH anvil the on-chain tests
    # use, not a product setting. Four test files document it as the override
    # for a machine where another project holds 127.0.0.1:8545 — and this loop
    # deleted it before any of them read it, so the override had never worked
    # (found 2026-10-13: "no anvil at http://127.0.0.1:8545" with it set).
    for key in [k for k in os.environ if k.startswith("ACR_") and k not in ("ACR_TEST_RPC", "ACR_TEST_PLUGIN")]:
        del os.environ[key]

    # THE SUITE USED TO WRITE PAYMENTS INTO THE PRODUCTION LEDGER.
    #
    # `operator.LOG_PATH` is read from this variable AT IMPORT, defaulting to
    # `data/operator_decisions.jsonl` — which is the path `render.yaml` points
    # the deploy at, and one of the three files `statement.read_decisions`
    # reads. `operator_keeper._pass` calls `run_obligation` without `log_path`,
    # so the keeper's tests fell through to that default and appended their
    # fixtures: 8 rows of `paid_usdc: 0.5` carrying `tx: 0x` + "ee" * 32 — four
    # USDC of payments that never happened, with a fabricated transaction hash,
    # in the file the operator's own surfaces serve. Exactly what
    # `ledger_audit._phantom` exists to catch, arriving from our own tests.
    # Nothing structural hid them; they sat under `business: "payer"` and the
    # slug filter happened not to ask.
    #
    # Set rather than stripped, and set HERE because `pytest_configure` runs
    # before collection and therefore before the module-level read. A tmp path
    # per run, so one suite cannot read another's rows either.
    os.environ["ACR_OPERATOR_LOG_PATH"] = str(
        pathlib.Path(tempfile.gettempdir())
        / f"acr-test-operator-decisions-{os.getpid()}.jsonl"
    )
    reset_settings()
