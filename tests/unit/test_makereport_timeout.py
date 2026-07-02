# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Regression test for `conftest._run_with_timeout`.

The makereport hook snapshots device state (device_info + lora config) when a
test fails. Those queries can hang forever on a wedged board. The bounded
wrapper MUST return promptly on timeout and *abandon* the stuck worker — an
earlier ThreadPoolExecutor version joined the worker on context-exit, so a hung
`SerialInterface` connect blocked the hook past the timeout; pytest-timeout's
SIGALRM then fired mid-hook and crashed the whole session (INTERNALERROR),
discarding every remaining tier. This pins the abandon-on-timeout behaviour.
"""

from __future__ import annotations

import threading
import time

from tests import conftest


def test_returns_result_for_fast_fn() -> None:
    assert conftest._run_with_timeout(lambda: 42, 5.0) == 42


def test_reraises_worker_exception() -> None:
    def boom():
        raise ValueError("worker blew up")

    try:
        conftest._run_with_timeout(boom, 5.0)
    except ValueError as exc:
        assert "worker blew up" in str(exc)
    else:  # pragma: no cover - the call must propagate the worker's error
        raise AssertionError("worker exception was swallowed")


def test_abandons_hung_worker_promptly() -> None:
    """A worker that never returns must NOT make the wrapper block past the
    timeout — the regression that crashed the session. With the old executor it
    blocked until the worker finished; now it returns in ~timeout."""
    started = threading.Event()

    def hang():
        started.set()
        time.sleep(30)  # uncancellable stand-in for a wedged connect()

    t0 = time.monotonic()
    try:
        conftest._run_with_timeout(hang, 0.3)
    except TimeoutError:
        pass
    else:  # pragma: no cover
        raise AssertionError("a hung worker should raise TimeoutError")
    elapsed = time.monotonic() - t0

    assert started.wait(1.0), "worker never started"
    # Must return shortly after the 0.3s timeout — NOT after the 30s sleep.
    assert elapsed < 3.0, f"wrapper blocked {elapsed:.1f}s on the hung worker"
