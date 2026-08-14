"""Worker command queues: the GUI thread must never touch a device.

Run: python tests/test_workers.py

Exercises the queue mechanics directly (no threads started, no hardware):
that public setters only enqueue, that setpoints conflate while actions
keep their order, and that stale commands are dropped on connect.
"""

import os
import sys
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyqtgraph.Qt import QtWidgets  # noqa: E402

app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

from linien_worker import LinienWorker  # noqa: E402
from mot_workers import LaserSetpointWorker  # noqa: E402


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


def test_linien_setters_only_enqueue():
    worker = LinienWorker(host="fake.local")
    worker.client = FakeClient()

    worker.set_modulation_amplitude(500)
    worker.set_sweep_speed(7)
    # Nothing has reached the device: the run loop has not drained yet.
    assert worker.client.connection.root.calls == []
    assert len(worker._pending) == 2

    worker._process_pending()
    assert worker.client.parameters.modulation_amplitude.value == 2048  # 0.5*Vpp
    assert worker.client.parameters.sweep_speed.value == 7
    assert worker._pending == []


def test_linien_setpoints_conflate_actions_do_not():
    worker = LinienWorker(host="fake.local")
    worker.client = FakeClient()

    # A slider drag must become one write, not a replayed burst.
    for mv in (100, 200, 300, 400):
        worker.set_modulation_amplitude(mv)
    assert len(worker._pending) == 1
    worker._process_pending()
    assert worker.client.parameters.modulation_amplitude.value == int(
        round(0.4 * 4095.75)
    )

    # Actions keep their ordering: two lock requests are two requests.
    worker.lock_laser(10, 20, 2048)
    worker.lock_laser(30, 40, 2048)
    assert [k for k, _ in worker._pending] == ["lock", "lock"]


def test_linien_unknown_command_faults_without_killing_the_loop():
    worker = LinienWorker(host="fake.local")
    worker.client = FakeClient()
    faults = []
    worker.fault.connect(faults.append)

    worker._queue("nonsense")
    worker.set_sweep_speed(3)
    worker._process_pending()

    assert faults and "nonsense" in faults[0]
    # The good command still ran.
    assert worker.client.parameters.sweep_speed.value == 3


def test_linien_failing_command_does_not_stop_the_next():
    worker = LinienWorker(host="fake.local")
    worker.client = FakeClient()
    faults = []
    worker.fault.connect(faults.append)

    def boom():
        raise RuntimeError("rpyc is down")

    worker.client.connection.root.write_registers = boom
    worker.set_sweep_speed(5)
    worker.set_modulation_phase(90)
    worker._process_pending()

    assert len(faults) == 2, faults
    assert "rpyc is down" in faults[0]


def test_linien_reads_come_back_as_signals():
    worker = LinienWorker(host="fake.local")
    worker.client = FakeClient()
    worker.client.parameters.p.value = 300
    worker.client.parameters.i.value = 2000
    worker.client.parameters.d.value = 0

    got = []
    worker.pid_read.connect(got.append)
    worker.request_pid()
    # A read is a request, not a return value.
    assert got == []
    worker._process_pending()
    assert got == [(300, 2000, 0)]

    filters = []
    worker.filters_read.connect(filters.append)
    worker.request_filters()
    worker._process_pending()
    assert filters[0]["filter_1_frequency"] == 0
    assert set(filters[0]) == {
        "automatic", "filter_1_enabled", "filter_1_type",
        "filter_1_frequency", "filter_2_enabled", "filter_2_type",
        "filter_2_frequency",
    }


def test_linien_lock_arms_a_deadline_instead_of_blocking():
    """A 30 s lock wait used to run on the GUI thread and freeze it."""
    import pickle

    import numpy as np

    worker = LinienWorker(host="fake.local")
    worker.client = FakeClient()
    spectrum = np.zeros(2048, dtype=np.int16)
    worker.client.parameters.to_plot.value = pickle.dumps(
        {"error_signal_1": spectrum}
    )

    worker.lock_laser(100, 200, 2048)
    worker._process_pending()

    assert worker._await_lock is not None
    want, deadline = worker._await_lock
    assert want is True
    calls = [name for name, _ in worker.client.connection.root.calls]
    assert "start_autolock" in calls

    # The spectrum handed to the autolock is raw counts, not the /8192
    # display copy -- volts degrade the peak finding badly.
    args = next(a for n, a in worker.client.connection.root.calls
                if n == "start_autolock")
    assert pickle.loads(args[2]).dtype == np.int16


def test_linien_stale_commands_dropped():
    worker = LinienWorker(host="fake.local")
    worker.set_modulation_amplitude(900)
    worker.lock_laser(1, 2, 3)
    # What _connect() does before announcing itself: a lock aimed at a
    # spectrum from the previous session must not run against this one.
    worker._pop_pending()
    assert worker._pending == []


def _run_worker(worker, fake, seconds, states=None, faults=None, on_tick=None):
    """Drive the real run loop against a fake client for a moment."""
    import time

    def fake_connect():
        worker.client = fake
        return True

    worker._connect = fake_connect
    worker.start()
    deadline = time.time() + seconds
    while time.time() < deadline:
        app.processEvents()
        time.sleep(0.02)
        if on_tick is not None:
            on_tick()
    worker.running = False
    worker.wait(3000)


def _sweeping(n=8):
    import pickle
    return pickle.dumps({"error_signal_1": [0] * n, "monitor_signal": [0] * n})


def _locked(n=8):
    import pickle
    return pickle.dumps({"error_signal": [0] * n, "control_signal": [0] * n})


def test_run_loop_tracks_lock_state():
    """The loop, not just the helpers -- this path had no coverage.

    linien names the signal "error_signal" only while locked and
    "error_signal_1" while sweeping (linien_server/acquisition.py), which
    is what the lock indicator reads.
    """
    worker = LinienWorker(host="fake.local")
    fake = FakeClient()
    fake.parameters.to_plot.value = _sweeping()
    fake.parameters.lock.value = 0

    states, frames = [], []
    worker.lock_state.connect(states.append)
    worker.new_data.connect(frames.append)

    flipped = []

    def flip():
        if not flipped and states == [False]:
            fake.parameters.to_plot.value = _locked()
            fake.parameters.lock.value = 1
            flipped.append(True)

    _run_worker(worker, fake, 1.5, on_tick=flip)
    assert frames, "the loop never emitted a frame"
    assert states == [False, True], states


def test_run_loop_reports_a_failed_autolock_instead_of_timing_out():
    """linien sets autolock_failed when it gives up looking for the line.

    Without reading it, a failed lock is a silent 30 s wait and then a
    generic message -- indistinguishable from the button doing nothing.
    """
    worker = LinienWorker(host="fake.local")
    fake = FakeClient()
    fake.parameters.to_plot.value = _sweeping(2048)
    fake.parameters.lock.value = 0
    fake.parameters.autolock_failed.value = False

    faults = []
    worker.fault.connect(faults.append)
    worker.lock_laser(100, 200, 2048)

    def fail_it():
        if worker._await_lock is not None:
            fake.parameters.autolock_failed.value = True

    _run_worker(worker, fake, 2.0, on_tick=fail_it)
    assert any("could not find" in f for f in faults), faults
    # Well inside the 30 s timeout, and the flag is cleared for next time.
    assert worker._await_lock is None
    assert not fake.parameters.autolock_failed.value


def test_laser_setters_only_enqueue_and_conflate():
    worker = LaserSetpointWorker(port=None)
    writes = []
    worker._write = writes.append
    worker._ser = types.SimpleNamespace(is_open=True)

    for ma in (70, 75, 80):
        worker.set_current(ma)
    worker.laser_on(True)
    worker.laser_on(False)
    # Serial I/O has not happened on this (the GUI) thread.
    assert writes == []
    # Setpoints conflate; enable/disable keeps order, so a rapid off/on
    # cannot collapse into just one of them.
    assert [k for k, _ in worker._pending] == ["current", "enable", "enable"]

    for kind, value in worker._pop_pending():
        worker._apply(kind, value)
    assert writes == ["ilaser 80.0", "lason 1", "lason 0"]


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("WORKERS OK")


if __name__ == "__main__":
    main()
