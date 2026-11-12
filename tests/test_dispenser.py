"""Drive the DispenserWorker state machine directly, with a fake DPS client.

Run: python tests/test_dispenser.py  (no hardware touched)
"""

import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from pyqtgraph.Qt import QtCore  # noqa: E402

app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

from mot_workers import DispenserWorker, RidenDPSClient  # noqa: E402

V, I, OUT = (
    RidenDPSClient.REG_V_SET,
    RidenDPSClient.REG_I_SET,
    RidenDPSClient.REG_OUTPUT,
)

GOOD = {"v_set": 5.0, "i_set": 7.5}  # what the supply reads back once programmed


class FakeClient:
    def __init__(self):
        self.writes = []

    def write_register(self, reg, value):
        self.writes.append((reg, value))


def output_writes(client):
    return [v for r, v in client.writes if r == OUT]


assert DispenserWorker.FIRE_SECONDS == 30.0
assert DispenserWorker.DISPENSE_CURRENT_A == 7.5

events = {"fire_states": [], "summaries": []}

# The 15 min re-fire lockout was removed on 2026-10-16 at the operator's
# request, so there is no cooldown phase and no cooldown_started signal.
assert not hasattr(DispenserWorker, "COOLDOWN_SECONDS")
assert not hasattr(DispenserWorker, "cooldown_started")

w = DispenserWorker(port=None)
w._client = FakeClient()
w.fire_state.connect(lambda s: events["fire_states"].append(dict(s)))
w.fire_summary.connect(lambda s: events["summaries"].append(dict(s)))

# 1) fire() programs the fixed setpoints (5.00 V, 7.5 A) then enables output
w.fire()
w._process_pending()
assert w._phase == "firing", w._phase
assert w._client.writes == [(V, 500), (I, 750), (OUT, 1)], w._client.writes

# 2) mid-fire, a front-panel setpoint change is forced back
w._client.writes.clear()
w._service({"output_enabled": True, "v_set": 5.0, "i_set": 12.0})
assert w._client.writes == [(I, 750)], w._client.writes
w._client.writes.clear()
w._service({"output_enabled": True, **GOOD})
assert w._client.writes == []  # correct setpoints -> no writes

# 3) after FIRE_SECONDS the service loop forces the output off, and a
#    non-aborted fire summary is emitted. The phase returns straight to
#    idle -- the fire is still time-limited, only the lockout is gone.
w._fire_end = time.monotonic() - 0.1
w._service({"output_enabled": True, **GOOD})
assert w._phase == "idle", w._phase
assert output_writes(w._client) == [0]
assert len(events["summaries"]) == 1
summary = events["summaries"][0]
assert summary["aborted"] is False
assert summary["planned_s"] == 30.0
assert summary["current_a"] == 7.5
assert summary["elapsed_s"] >= 0.0

# 4) re-firing immediately is allowed: nothing rate-limits the dispenser
#    any more, which is the whole point of removing the cooldown.
w._client.writes.clear()
w.fire()
w._process_pending()
assert w._phase == "firing", w._phase
assert w._client.writes == [(V, 500), (I, 750), (OUT, 1)], w._client.writes

# 5) manual abort mid-fire -> off + aborted summary, straight back to idle
w.abort_fire()
w._process_pending()
assert w._phase == "idle", w._phase
assert output_writes(w._client)[-1] == 0
assert len(events["summaries"]) == 2
assert events["summaries"][1]["aborted"] is True

# 6) external/front-panel enable while idle (connected, so not the first
#    read): setpoints enforced + time-limited
w2 = DispenserWorker(port=None)
w2._client = FakeClient()
w2._service({"output_enabled": False, **GOOD})  # connect: found off
w2._service({"output_enabled": True, "v_set": 30.0, "i_set": 20.0})
assert w2._phase == "firing"
assert w2._client.writes == [(V, 500), (I, 750)], w2._client.writes
w2._fire_end = time.monotonic() - 0.1
w2._service({"output_enabled": True, **GOOD})
assert w2._phase == "idle"
assert output_writes(w2._client) == [0]

# 7) a fresh worker starts idle and fires at once -- there is no persisted
#    lockout to inherit, and the constructor no longer takes one.
w3 = DispenserWorker(port=None)
w3._client = FakeClient()
assert w3._phase == "idle"
w3.fire()
w3._process_pending()
assert w3._client.writes == [(V, 500), (I, 750), (OUT, 1)]
try:
    DispenserWorker(port=None, cooldown_until=time.time() + 600)
except TypeError:
    pass
else:
    raise AssertionError("cooldown_until is still accepted")

# 8) set_output is still routed through fire/abort, so the time limit holds
w4 = DispenserWorker(port=None)
w4._client = FakeClient()
w4.set_output(True)
w4._process_pending()
assert w4._phase == "firing"
assert w4._client.writes == [(V, 500), (I, 750), (OUT, 1)]
w4.set_output(False)
w4._process_pending()
assert output_writes(w4._client)[-1] == 0

# 9) the public setpoint API is inert on the dispenser
w4._client = FakeClient()
w4.set_voltage(30.0)
w4.set_current(20.0)
w4.set_setpoints(30.0, 20.0)
w4._process_pending()
assert w4._client.writes == []

# fire_state payloads carried sensible remaining times
firing = [s for s in events["fire_states"] if s["phase"] == "firing"]
assert firing and 0 < firing[0]["remaining_s"] <= 30.0

# 10) override parameters via constructor: 10 A / 45 s fire
w5 = DispenserWorker(port=None, fire_seconds=45.0, dispense_current_a=10.0)
w5._client = FakeClient()
w5.fire()
w5._process_pending()
assert w5._client.writes == [(V, 500), (I, 1000), (OUT, 1)], w5._client.writes
remaining = w5._fire_end - time.monotonic()
assert 44.0 < remaining <= 45.0, remaining

# 11) runtime override is clamped (current to 20 A max, duration to >= 1 s)
w5.set_fire_parameters(25.0, 0.2)
w5._process_pending()
assert w5.dispense_current_a == 20.0
assert w5.fire_seconds == 1.0

# 12) mid-fire enforcement tracks the overridden current
w5._client.writes.clear()
w5._service({"output_enabled": True, "v_set": 5.0, "i_set": 10.0})
assert w5._client.writes == [(I, 2000)], w5._client.writes  # 20.00 A

# 13) an output found ON at connect is switched off at once. It used to
#     start a fresh full fire window: a link dropped mid-fire ran the
#     dispenser for the outage plus up to 120 s more.
w8 = DispenserWorker(port=None)
w8._client = FakeClient()
w8._service({"output_enabled": True, **GOOD})
assert w8._phase == "idle", w8._phase
assert output_writes(w8._client) == [0], w8._client.writes


class FlakyClient(FakeClient):
    """Fails the next `fail` writes, as a CRC error on the bus does."""

    def __init__(self, fail):
        super().__init__()
        self.fail = fail

    def write_register(self, reg, value):
        if self.fail:
            self.fail -= 1
            raise IOError("CRC mismatch")
        super().write_register(reg, value)


def poll(worker):
    try:
        worker._process_pending()
    except IOError:
        pass  # the run loop reports it; the test looks at what happens next


# 14) a failed Abort is retried on the next poll. One bad write used to
#     drop it, and the dispenser ran on until its timer.
w9 = DispenserWorker(port=None)
w9._client = FakeClient()
w9.fire()
w9._process_pending()
w9._client = FlakyClient(fail=1)
w9.abort_fire()
poll(w9)
assert w9._phase == "firing"  # the off write failed: still the truth
poll(w9)
assert w9._phase == "idle", w9._phase
assert output_writes(w9._client) == [0], w9._client.writes

# 15) ...but a failed Fire is not: a link that recovers later must not
#     start a fire nobody is watching for.
w10 = DispenserWorker(port=None)
w10._client = FlakyClient(fail=1)
w10.fire()
poll(w10)
poll(w10)
assert w10._phase == "idle", w10._phase
assert output_writes(w10._client) == [], w10._client.writes

# 17) stopping the worker mid-fire ends the fire properly: output off, an
#     aborted summary, and an idle state for the card (it froze on
#     "firing" when the stop path wrote the register directly).
stop_events = {"states": [], "summaries": []}
w11 = DispenserWorker(port=None)
w11._client = FakeClient()
w11.fire_state.connect(lambda st: stop_events["states"].append(dict(st)))
w11.fire_summary.connect(lambda st: stop_events["summaries"].append(dict(st)))
w11.fire()
w11._process_pending()
assert stop_events["states"][-1]["planned_s"] == 30.0  # the bar's length
w11._drive_safe_on_stop()
assert output_writes(w11._client)[-1] == 0
assert stop_events["states"][-1]["phase"] == "idle"
assert stop_events["summaries"] and stop_events["summaries"][-1]["aborted"]

# 16) constructor falls back to defaults for None only. An operator's 0 A
# once became the 7.5 A default on every connect (an `or` fallback), and
# a hand-edited 600 s fire ran 600 s: the fire time had no upper bound.
w6 = DispenserWorker(port=None, fire_seconds=None, dispense_current_a=None)
assert w6.fire_seconds == 30.0
assert w6.dispense_current_a == 7.5
w7 = DispenserWorker(port=None, fire_seconds=600.0, dispense_current_a=0)
assert w7.dispense_current_a == 0.01, w7.dispense_current_a  # floor, not 7.5
assert w7.fire_seconds == DispenserWorker.MAX_FIRE_SECONDS
# ...and the voltage is capped at 5 V wherever it comes from (operator,
# 2026-11-11): constructor, or a runtime override from the card.
w12 = DispenserWorker(port=None, dispense_voltage_v=12.0)
assert w12.dispense_voltage_v == 5.0, w12.dispense_voltage_v
w12.set_fire_parameters(7.5, 30.0, voltage_v=9.0)
w12._process_pending()
assert w12.dispense_voltage_v == 5.0, w12.dispense_voltage_v

# --- VCO tuning: ROS-3000-619+ datasheet +25 C curve, clamped to 0-5 V ---
from mot_control import (  # noqa: E402
    thermistor_ohms_to_c, vco_mhz_to_vtune, vtune_to_vco_mhz,
)

assert vtune_to_vco_mhz(-1.0) == 2720.4        # clamped low
assert vtune_to_vco_mhz(0.0) == 2720.4
assert vtune_to_vco_mhz(5.0) == 3032.6         # DAC full scale
assert vtune_to_vco_mhz(20.0) == 3032.6        # clamped high
assert abs(vtune_to_vco_mhz(3.25) - 2920.5) < 1e-9   # a table row
# Between rows: 3.6765 V is the bench operating point the old linear map
# called 2925 MHz.
assert abs(vtune_to_vco_mhz(3.6765) - 2949.4) < 0.1
for v in (0.0, 1.3, 2.9, 3.6765, 4.8, 5.0):
    assert abs(vco_mhz_to_vtune(vtune_to_vco_mhz(v)) - v) < 1e-9

# --- Thermistor Steinhart-Hart conversion ---
assert abs(thermistor_ohms_to_c(10000) - 25.0) < 1.5   # 10 k NTC ~ 25 C
assert thermistor_ohms_to_c(5000) > thermistor_ohms_to_c(10000)  # lower R = hotter
assert thermistor_ohms_to_c(0) < 100.0  # guard against log(0)

print("ALL DISPENSER + VCO CURVE + THERMISTOR TESTS PASSED")
