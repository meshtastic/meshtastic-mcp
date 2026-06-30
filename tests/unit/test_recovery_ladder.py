"""Recovery-ladder escalation logic.

Stubs the per-step techniques + the health probe so the orchestration is
exercised deterministically with no hardware: escalate until healthy, stop at
the first step that works, skip steps missing a prerequisite.
"""

from __future__ import annotations

import meshtastic_mcp.recovery as R


def _patch(monkeypatch, *, health: list[bool], called: list[str]) -> None:
    monkeypatch.setattr(R, "step_reboot", lambda port: called.append("reboot") or {})
    monkeypatch.setattr(
        R, "step_power_cycle", lambda loc, port, **k: called.append("power_cycle") or {}
    )
    monkeypatch.setattr(
        R,
        "step_touch_1200bps",
        lambda port: called.append("touch") or {"new_port": port},
    )
    monkeypatch.setattr(
        R, "step_reflash", lambda env, port: called.append("reflash") or {"exit_code": 0}
    )
    monkeypatch.setattr(R, "step_factory_reset", lambda port: called.append("factory") or {})
    seq = iter(health)
    monkeypatch.setattr(R, "is_healthy", lambda *a, **k: (next(seq, False), None))


def test_already_healthy_runs_nothing(monkeypatch):
    called: list[str] = []
    _patch(monkeypatch, health=[True], called=called)
    rep = R.run_ladder(port="/dev/x", hub_location="1-1", hub_port=2)
    assert rep["recovered"] and rep["final_step"] == "none"
    assert called == []


def test_stops_at_first_step_that_heals(monkeypatch):
    called: list[str] = []
    _patch(monkeypatch, health=[False, True], called=called)  # healthy after reboot
    rep = R.run_ladder(port="/dev/x", hub_location="1-1", hub_port=2)
    assert rep["recovered"] and rep["final_step"] == "reboot"
    assert called == ["reboot"]  # power_cycle never reached


def test_escalates_to_power_cycle(monkeypatch):
    called: list[str] = []
    _patch(monkeypatch, health=[False, False, True], called=called)
    rep = R.run_ladder(port="/dev/x", hub_location="1-1", hub_port=2)
    assert rep["final_step"] == "power_cycle"
    assert called == ["reboot", "power_cycle"]


def test_power_cycle_skipped_without_hub(monkeypatch):
    called: list[str] = []
    _patch(monkeypatch, health=[False, False], called=called)
    rep = R.run_ladder(port="/dev/x", steps=("reboot", "power_cycle"))  # no hub slot
    assert not rep["recovered"]
    pc = next(s for s in rep["steps"] if s["step"] == "power_cycle")
    assert pc["skipped"] == "no uhubctl hub port mapped"
    assert called == ["reboot"]  # skipped, not invoked


def test_reflash_requires_env(monkeypatch):
    called: list[str] = []
    _patch(monkeypatch, health=[False, False], called=called)
    rep = R.run_ladder(port="/dev/x", steps=("reboot", "reflash"))  # no env
    rf = next(s for s in rep["steps"] if s["step"] == "reflash")
    assert rf["skipped"] == "no pio env resolved" and "reflash" not in called

    called2: list[str] = []
    _patch(monkeypatch, health=[False, False, True], called=called2)
    rep2 = R.run_ladder(port="/dev/x", env="heltec-v3", steps=("reboot", "reflash"))
    assert rep2["final_step"] == "reflash" and called2 == ["reboot", "reflash"]


def test_full_ladder_when_nothing_heals(monkeypatch):
    called: list[str] = []
    _patch(monkeypatch, health=[False] * 8, called=called)
    rep = R.run_ladder(port="/dev/x", env="e", hub_location="1-1", hub_port=2, steps=R.LADDER)
    assert not rep["recovered"] and rep["final_step"] is None
    assert called == ["reboot", "power_cycle", "touch", "reflash", "factory"]


# --- web RecoveryService: the "reappeared" promotion must be health-gated ----
#
# After the ladder runs, a device that merely re-enumerated on the USB bus
# (online=1) is NOT recovered — a wedged board (or a board left behind by a
# failed DFU reflash) can sit there with a dead CDC. The service must require an
# actual device_info handshake before promoting `recovered: true`.

import asyncio

from meshtastic_mcp.web.services import recovery as WR


class _FakeHub:
    def __init__(self) -> None:
        self.events: list = []

    async def publish(self, topic, payload):
        self.events.append((topic, payload))

    def publish_threadsafe(self, topic, payload):
        self.events.append((topic, payload))


_UNHEALED = {
    "recovered": False,
    "final_step": None,
    "steps": [
        {"step": "reboot", "skipped": None, "healthy_after": False, "result": {}},
        {"step": "power_cycle", "skipped": None, "healthy_after": False, "result": {}},
    ],
}


def _service(monkeypatch, *, ladder_report, healthy, detail=None, online=1):
    device = {
        "kind": "serial",
        "current_port": "/dev/cu.NEW",
        "env": "rak4631",
        "hub_location": "20-3",
        "hub_port": 1,
        "online": online,
    }

    async def _get(db, serial):
        return dict(device)

    probe_calls: list = []

    def _is_healthy(port, **kwargs):
        probe_calls.append(port)
        return (healthy, detail)

    monkeypatch.setattr(WR.rd, "get", _get)
    monkeypatch.setattr(WR.rec, "run_ladder", lambda **kw: ladder_report)
    monkeypatch.setattr(WR.rec, "is_healthy", _is_healthy)

    svc = WR.RecoveryService(db=object(), hub=_FakeHub(), serialmon=None)
    return svc, probe_calls


def test_reappeared_needs_a_real_health_check(monkeypatch):
    # Re-enumerated (online=1) but every step was healthy_after:false and the
    # confirmation handshake fails — must NOT be promoted to recovered.
    svc, probe_calls = _service(
        monkeypatch,
        ladder_report=dict(_UNHEALED),
        healthy=False,
        detail="connected but no firmware_version",
    )
    rep = asyncio.run(svc.recover("0898D59592BBA72B", allow_reflash=True))
    assert rep["recovered"] is False
    assert rep["final_step"] is None
    assert rep["reappeared_unhealthy"]  # reason surfaced
    assert probe_calls == ["/dev/cu.NEW"]  # probed the re-enumerated port


def test_reappeared_promotes_on_successful_handshake(monkeypatch):
    svc, probe_calls = _service(
        monkeypatch,
        ladder_report=dict(_UNHEALED),
        healthy=True,
        detail={"firmware_version": "2.5.0"},
    )
    rep = asyncio.run(svc.recover("0898D59592BBA72B", allow_reflash=True))
    assert rep["recovered"] is True
    assert rep["final_step"] == "reappeared"
    assert probe_calls == ["/dev/cu.NEW"]
    assert "reappeared_unhealthy" not in rep


def test_offline_device_is_never_probed(monkeypatch):
    # Not even on the bus -> no probe, stays unrecovered (no false reappear).
    svc, probe_calls = _service(monkeypatch, ladder_report=dict(_UNHEALED), healthy=True, online=0)
    rep = asyncio.run(svc.recover("x", allow_reflash=True))
    assert rep["recovered"] is False
    assert probe_calls == []
    assert "reappeared_unhealthy" not in rep


def test_ladder_success_skips_the_reappear_probe(monkeypatch):
    # A ladder step actually healed the device — the post-ladder probe is
    # irrelevant and must not run.
    healed = {
        "recovered": True,
        "final_step": "reboot",
        "steps": [{"step": "reboot", "skipped": None, "healthy_after": True}],
    }
    svc, probe_calls = _service(monkeypatch, ladder_report=healed, healthy=False)
    rep = asyncio.run(svc.recover("x", allow_reflash=True))
    assert rep["recovered"] is True
    assert rep["final_step"] == "reboot"
    assert probe_calls == []
