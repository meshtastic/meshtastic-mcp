# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""A sweep that fails mid-run still says which settings it could not put back."""

from __future__ import annotations

import pytest

from meshtastic_mcp import pa_sweep, power_meter


class _Meter:
    def __init__(self, *_a, **_k) -> None:
        pass

    def __enter__(self) -> _Meter:
        return self

    def __exit__(self, *_exc) -> None:
        return None

    def version(self) -> str:
        return "fake"

    def set_freq_mhz(self, mhz: float) -> float:
        return mhz

    def sample(self, n: int, interval_s: float = 0.0) -> list[float]:
        return [-40.0] * n


class _Sampler:
    def __init__(self, *_a, **_k) -> None:
        pass

    def start(self) -> None:
        pass

    def stop(self) -> list[float]:
        return []


def _node(monkeypatch, *, restore_fails: bool) -> None:
    monkeypatch.setattr(
        pa_sweep,
        "read_lora_context",
        lambda port=None: {"region": "US", "tx_power": 20, "override_duty_cycle": False},
    )
    monkeypatch.setattr(power_meter, "PowerMeter", _Meter)
    monkeypatch.setattr(pa_sweep, "_BackgroundSampler", _Sampler)
    monkeypatch.setattr(pa_sweep.time, "sleep", lambda _s: None)

    def burst(*_a, **_k):
        raise RuntimeError("burst failed")

    monkeypatch.setattr(pa_sweep, "_key_burst", burst)

    def set_config(path, value, port=None):
        if restore_fails and path == "lora.override_duty_cycle" and value is False:
            raise RuntimeError("serial link lost")

    monkeypatch.setattr(pa_sweep.admin, "set_config", set_config)


def test_a_failed_sweep_names_the_override_it_left_on(monkeypatch) -> None:
    _node(monkeypatch, restore_fails=True)
    with pytest.raises(pa_sweep.PaSweepError) as raised:
        pa_sweep.sweep([20], confirm=True, tx_linger_s=0.0, settle_s=0.0)
    assert "burst failed" in str(raised.value)
    assert "lora.override_duty_cycle: serial link lost" in str(raised.value)
    assert isinstance(raised.value.__cause__, RuntimeError)


def test_a_failed_sweep_that_restored_cleanly_raises_its_own_error(monkeypatch) -> None:
    _node(monkeypatch, restore_fails=False)
    with pytest.raises(RuntimeError, match="burst failed"):
        pa_sweep.sweep([20], confirm=True, tx_linger_s=0.0, settle_s=0.0)
