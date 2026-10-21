"""Drive the DispenserWorker state machine directly, with a fake DPS client.

Run: python tests/test_dispenser.py  (no hardware touched)
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

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

# 6) external/front-panel enable while idle: setpoints enforced + time-limited
w2 = DispenserWorker(port=None)
w2._client = FakeClient()
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

# 16) constructor falls back to defaults for None/0 values
w6 = DispenserWorker(port=None, fire_seconds=None, dispense_current_a=0)
assert w6.fire_seconds == 30.0
assert w6.dispense_current_a == 7.5

# --- VCO tuning range: 0-5 V spans 2800-2970 MHz, linear, clamped ---
from mot_control import thermistor_ohms_to_c, vtune_to_vco_mhz  # noqa: E402

assert vtune_to_vco_mhz(-1.0) == 2800.0        # clamped low
assert vtune_to_vco_mhz(0.0) == 2800.0
assert vtune_to_vco_mhz(5.0) == 2970.0         # DAC full scale
assert vtune_to_vco_mhz(20.0) == 2970.0        # clamped high
assert abs(vtune_to_vco_mhz(2.5) - 2885.0) < 1e-9   # midpoint
assert abs(vtune_to_vco_mhz(1.0) - 2834.0) < 1e-9   # 2800 + 0.2 * 170

# --- Thermistor Steinhart-Hart conversion ---
assert abs(thermistor_ohms_to_c(10000) - 25.0) < 1.5   # 10 k NTC ~ 25 C
assert thermistor_ohms_to_c(5000) > thermistor_ohms_to_c(10000)  # lower R = hotter
assert thermistor_ohms_to_c(0) < 100.0  # guard against log(0)

print("ALL DISPENSER + VCO CURVE + THERMISTOR TESTS PASSED")
