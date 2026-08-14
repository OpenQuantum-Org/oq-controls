"""Linien worker unit handling, against a fake client (no hardware).

Run: python tests/test_workers.py

The queue-mechanics tests that used to live here went away with the
threading revert. What remains are the conversions that were silently
wrong by orders of magnitude once before, and are worth a guard.
"""

import os
import pickle
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyqtgraph.Qt import QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

import numpy as np  # noqa: E402

from linien_worker import LinienWorker  # noqa: E402


class FakeParam:
    def __init__(self, value=0):
        self.value = value


class FakeParams:
    """Every attribute is a parameter that remembers its last write."""

    def __init__(self):
        object.__setattr__(self, "_params", {})

    def __getattr__(self, name):
        return self._params.setdefault(name, FakeParam())


class FakeRoot:
    def __init__(self):
        self.calls = []

    def __getattr__(self, name):
        def record(*args):
            self.calls.append((name, args))
        return record


class FakeClient:
    def __init__(self):
        self.parameters = FakeParams()
        self.connection = types.SimpleNamespace(root=FakeRoot())


def worker():
    w = LinienWorker(host="fake.local")
    w.client = FakeClient()
    return w


def test_modulation_amplitude_is_dac_units_not_volts():
    """Vpp = 4095.75 units per volt peak-to-peak.

    Writing volts straight into this parameter made the dither ~4096x
    too small, which is invisible on the plot and ruins the lock.
    """
    w = worker()
    w.set_modulation_amplitude(500)  # 0.5 Vpp
    assert w.client.parameters.modulation_amplitude.value == 2048
    w.set_modulation_amplitude(1000)
    assert w.client.parameters.modulation_amplitude.value == 4096


def test_modulation_frequency_is_phase_accumulator_units_not_hz():
    """MHz = 0x10000000/8 units per MHz; writing Hz was ~33.55x low."""
    w = worker()
    w.set_modulation_frequency(1000)  # 1 MHz
    assert w.client.parameters.modulation_frequency.value == 0x10000000 // 8


def test_autolock_receives_raw_counts():
    """The autolock's peak finding works in ADC counts, not volts.

    The plotted copy is divided by 8192 for display; handing that to
    start_autolock degrades the lock badly, so the worker re-reads the
    spectrum itself.
    """
    w = worker()
    spectrum = (np.arange(2048) % 97).astype(np.int16)
    w.client.parameters.to_plot.value = pickle.dumps(
        {"error_signal_1": spectrum}
    )
    w._wait_for_lock = lambda *a, **k: True  # do not sit out the real wait
    w.lock_laser(100, 200, 2048)

    sent = next(
        a for n, a in w.client.connection.root.calls if n == "start_autolock"
    )
    raw = pickle.loads(sent[2])
    assert raw.dtype == np.int16
    assert raw.max() > 1  # not the /8192 display copy


def test_lock_uses_the_simple_autolock_preference():
    """SIMPLE has to be set through the *preference*.

    The server builds its algorithm selector from
    autolock_mode_preference and assigns autolock_mode from it, so
    writing autolock_mode directly is overwritten.
    """
    from linien_common.common import AutolockMode

    w = worker()
    w.client.parameters.to_plot.value = pickle.dumps(
        {"error_signal_1": np.zeros(2048, dtype=np.int16)}
    )
    w._wait_for_lock = lambda *a, **k: True
    w.lock_laser(100, 200, 2048)
    assert (
        w.client.parameters.autolock_mode_preference.value
        == AutolockMode.SIMPLE
    )


def test_stalled_server_is_detected_from_its_heartbeat():
    """linien-server bumps `ping` once a second; a frozen one does not.

    A wedged server still answers RPyC, so the plot just stops with no
    explanation -- indistinguishable from a static spectrum. The
    heartbeat is what tells them apart.
    """
    import time

    w = worker()
    w.STALL_AFTER_S = 0.15  # do not sit out the real 5 s
    w.client.parameters.ping.value = 7

    seen = []
    w.server_stalled.connect(seen.append)

    w._check_server_alive()          # first sighting, starts the clock
    assert seen == []
    time.sleep(0.2)
    w._check_server_alive()          # still 7 -> stalled
    assert seen == [True], seen
    w._check_server_alive()          # reported on the edge, not per poll
    w._check_server_alive()
    assert seen == [True], seen

    w.client.parameters.ping.value = 8
    w._check_server_alive()
    assert seen == [True, False], seen


def test_a_beating_heart_never_reports_a_stall():
    import time

    w = worker()
    w.STALL_AFTER_S = 0.15
    seen = []
    w.server_stalled.connect(seen.append)
    for tick in range(6):
        w.client.parameters.ping.value = tick
        w._check_server_alive()
        time.sleep(0.05)
    assert seen == [], seen


def test_a_failed_ping_read_is_not_a_stall():
    """A read that raises is a connection problem, reported elsewhere."""
    w = worker()
    w.STALL_AFTER_S = 0.0
    seen = []
    w.server_stalled.connect(seen.append)

    class Exploding:
        @property
        def value(self):
            raise RuntimeError("connection reset")

    w.client.parameters._params["ping"] = Exploding()
    w._check_server_alive()
    w._check_server_alive()
    assert seen == [], seen


def test_stop_can_leave_the_lock_engaged():
    """The shutdown checklist's "leave it locked" has to reach here."""
    w = worker()
    w.unlock_on_stop = False
    w.stop()
    assert "start_sweep" not in [n for n, _ in w.client.connection.root.calls]

    w = worker()
    w.stop()
    assert "start_sweep" in [n for n, _ in w.client.connection.root.calls]


def test_faults_go_to_the_log_not_stdout():
    """CLAUDE.md invariant 4: no fault path ends at a print."""
    import io as _io
    from contextlib import redirect_stdout

    import mot_logging

    w = worker()
    records = []
    w.event_log = mot_logging.EventLog(".")
    w.event_log._write = lambda kind, event, fields: records.append(
        (kind, event, fields)
    )

    def boom():
        raise RuntimeError("rpyc is down")

    w.client.connection.root.write_registers = boom
    captured = _io.StringIO()
    with redirect_stdout(captured):
        w.set_modulation_phase(90)
    assert captured.getvalue() == "", captured.getvalue()
    assert records and records[0][0] == "fault", records


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("WORKERS OK")


if __name__ == "__main__":
    main()
