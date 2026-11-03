"""SerialProbeWorker assignment logic, with monkeypatched probes.

Run: python tests/test_probe.py  (no hardware touched)
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from pyqtgraph.Qt import QtCore  # noqa: E402

app = QtCore.QCoreApplication.instance() or QtCore.QCoreApplication([])

import mot_workers  # noqa: E402
from mot_workers import SerialProbeWorker  # noqa: E402

PORTMAP = {}   # port -> ("dps", model) | ("koheron", None)
PORTS = []
CALLS = []


def fake_dps(port, address=1, **kw):
    CALLS.append(("dps", port))
    identity = PORTMAP.get(port)
    return identity[1] if identity and identity[0] == "dps" else None


def fake_koh(port, **kw):
    CALLS.append(("koh", port))
    identity = PORTMAP.get(port)
    return bool(identity and identity[0] == "koheron")


mot_workers.probe_dps_model = fake_dps
mot_workers.probe_koheron_port = fake_koh
mot_workers._list_candidate_ports = lambda: list(PORTS)


def run_probe(saved=None):
    CALLS.clear()
    results = []
    worker = SerialProbeWorker(saved=saved or {})
    worker.result.connect(results.append)
    worker.run()  # synchronous: no thread needed for logic testing
    return results[0]


LASER, COIL, DISP, JUNK = "/dev/uA", "/dev/uB", "/dev/uC", "/dev/uZ"
PORTMAP.update({
    LASER: ("koheron", None),
    COIL: ("dps", 5005),
    DISP: ("dps", 5020),
})

# 1) fresh scan (nothing saved) finds and assigns all three
PORTS[:] = [JUNK, LASER, COIL, DISP]
res = run_probe()
assert res["ok"], res
assert res["assignments"] == {"laser": LASER, "mot_coil": COIL, "dispenser": DISP}
assert res["status"] == {k: "verified" for k in ("laser", "mot_coil", "dispenser")}

# 2) correct saved ports verify directly: exactly one probe per device, no scan
res = run_probe(saved={"laser": LASER, "mot_coil": COIL, "dispenser": DISP})
assert res["ok"]
assert CALLS == [("koh", LASER), ("dps", COIL), ("dps", DISP)], CALLS

# 3) coil/dispenser cables swapped since last run -> re-verified and corrected
res = run_probe(saved={"laser": LASER, "mot_coil": DISP, "dispenser": COIL})
assert res["ok"]
assert res["assignments"] == {"laser": LASER, "mot_coil": COIL, "dispenser": DISP}

# 4) dispenser unplugged -> partial result, ok=False
PORTS[:] = [LASER, COIL, JUNK]
res = run_probe(saved={"laser": LASER, "mot_coil": COIL, "dispenser": DISP})
assert not res["ok"]
assert res["assignments"]["laser"] == LASER
assert res["assignments"]["mot_coil"] == COIL
assert res["assignments"]["dispenser"] == ""
assert res["status"]["dispenser"] == "missing"

# 5) no ports at all
PORTS[:] = []
res = run_probe()
assert not res["ok"]
assert all(v == "" for v in res["assignments"].values())

# 6) two DPS units swapped AND laser missing -> DPS still resolved by model
PORTS[:] = [COIL, DISP]
res = run_probe(saved={"laser": "", "mot_coil": DISP, "dispenser": COIL})
assert not res["ok"]
assert res["assignments"]["mot_coil"] == COIL
assert res["assignments"]["dispenser"] == DISP
assert res["status"]["laser"] == "missing"

print("ALL PROBE LOGIC TESTS PASSED")
