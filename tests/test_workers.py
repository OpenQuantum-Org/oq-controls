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
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

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
        # linien sets this only once connect() has reached the server, and
        # the worker's write guards read it. A fake that stands for a
        # connected client has to carry it, or every setter below refuses.
        self.connected = True


def worker():
    w = LinienWorker(host="fake.local")
    w.client = FakeClient()
    # Lock actions run on their own thread in the app; run them inline so
    # a test can look at what they sent as soon as the call returns.
    w._run_action = lambda label, work: work()
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


def test_modulation_amplitude_is_read_back_in_mv():
    """The inverse of the write above, used at connect.

    Nothing read this parameter before, so the dither button was a saved
    preference displayed as device state -- it claimed a running dither on
    a launch that never reached the Red Pitaya.
    """
    w = worker()
    seen = []
    w.modulation_state.connect(seen.append)
    w.client.parameters.modulation_amplitude.value = 2048  # 0.5 Vpp
    w._emit_modulation_state()
    assert seen and abs(seen[0] - 500.0) < 0.1, seen

    # A failed read is logged, not emitted as a zero: "the dither is off"
    # and "we could not ask" are different claims.
    class Exploding:
        @property
        def value(self):
            raise RuntimeError("rpyc is down")

    w.client.parameters._params["modulation_amplitude"] = Exploding()
    w._emit_modulation_state()
    assert len(seen) == 1, seen


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
    w.client.parameters.ping.value = 6

    seen = []
    w.server_stalled.connect(seen.append)

    w._check_server_alive()          # first sighting: nothing proven yet
    w.client.parameters.ping.value = 7
    w._check_server_alive()          # it ticked -- now the heartbeat counts
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



def test_a_heartbeat_that_never_ticks_is_not_reported_as_a_stall():
    """A still `ping` is only evidence once it has been seen to move.

    linien caches parameters client-side, so `ping.value` sits at its
    connect-time value until the change queue is drained. That looked
    exactly like a wedged server on a perfectly healthy one, and
    accusing a healthy server is the worse error.
    """
    import time

    w = worker()
    w.STALL_AFTER_S = 0.05
    w.client.parameters.ping.value = 42  # frozen cache, never advances
    seen = []
    w.server_stalled.connect(seen.append)
    for _ in range(5):
        w._check_server_alive()
        time.sleep(0.03)
    assert seen == [], seen


def test_the_parameter_queue_is_drained():
    """Nothing drained it, so it grew on the server for every client.

    Every client gets a listener registered for every cacheable
    parameter (linien_server/parameters.py:617) and each change is
    appended to a per-client list that only
    exposed_get_changed_parameters_queue empties.
    """
    from mot_workers import drain_parameter_queue

    calls = []

    class Params:
        def check_for_changed_parameters(self):
            calls.append(1)

    class Client:
        parameters = Params()

    drain_parameter_queue(Client())
    assert calls == [1]

    class Broken:
        @property
        def parameters(self):
            raise RuntimeError("connection reset")

    drain_parameter_queue(Broken())  # must not raise


def test_to_plot_is_fetched_once_per_poll():
    """to_plot is the only uncached read in the loop -- do not pay twice.

    It is a pickled 2048-point array over RPyC. Acquisition unpickles it
    and the lock check used to fetch the whole thing again, doubling the
    wire traffic of every poll. The lock check now reads only the
    server's cached lock flag, so it never touches to_plot.
    """
    reads = []

    class CountingParam:
        def __init__(self, value):
            self._value = value

        @property
        def value(self):
            reads.append(1)
            return self._value

    w = worker()
    w.client.parameters._params["to_plot"] = CountingParam(
        pickle.dumps({"error_signal_1": [0] * 8, "monitor_signal": [0] * 8})
    )
    w.client.parameters.lock.value = 0

    # Stand in for one acquisition pass: unpickle once, stash it.
    w.to_plot = pickle.loads(w.client.parameters.to_plot.value)
    before = len(reads)
    w._emit_lock_state_best_effort()
    w._emit_lock_state_best_effort()
    assert len(reads) == before, "the lock check re-fetched to_plot"

    # With nothing stashed it still does not fetch: the server's cached
    # lock flag decides, not the trace.
    w.to_plot = {}
    w._emit_lock_state_best_effort()
    assert len(reads) == before


def test_default_host_is_this_experiments_red_pitaya():
    """rp-F0C6AD was a different device; an empty host field used to
    connect to it silently."""
    from linien_worker import DEFAULT_RP_HOST
    from linien_widget import DEFAULT_RP_HOST as WIDGET_HOST

    assert DEFAULT_RP_HOST == "rp-f0f2b5.local"
    assert WIDGET_HOST == DEFAULT_RP_HOST  # two definitions, one device


def test_stop_can_leave_the_lock_engaged():
    """The shutdown checklist's "leave it locked" has to reach here."""
    w = worker()
    w.unlock_on_stop = False
    w.stop()
    assert "start_sweep" not in [n for n, _ in w.client.connection.root.calls]

    w = worker()
    w.stop()
    assert "start_sweep" in [n for n, _ in w.client.connection.root.calls]


def test_a_failing_lock_state_read_reaches_the_log():
    """The handler that decides whether the UI says LOCKED must not be
    silent.

    It used to swallow every exception, which is how "the lock status is
    not updating" became an unreproducible report: the evidence went
    into a bare `except: pass`. Repeats are collapsed so a persistent
    failure does not fill the log at 5 Hz.
    """
    import mot_logging

    w = worker()
    records = []
    w.event_log = mot_logging.EventLog(".")
    w.event_log._write = lambda kind, event, fields: records.append(
        (kind, fields.get("message", ""))
    )

    class Exploding:
        @property
        def value(self):
            raise RuntimeError("rpyc is down")

    # The server's lock flag is what decides (not the trace, which goes
    # stale after an unlock), so that is the read that must not go quiet.
    w.client.parameters._params["lock"] = Exploding()
    w._emit_lock_state_best_effort()
    assert records, "the lock-state read failed silently"
    assert records[0][0] == "fault"
    assert "rpyc is down" in records[0][1]

    # The same failure again is not logged again.
    w._emit_lock_state_best_effort()
    w._emit_lock_state_best_effort()
    assert len(records) == 1, records

    # A different failure is.
    class OtherFailure:
        @property
        def value(self):
            raise RuntimeError("connection reset")

    w.client.parameters._params["lock"] = OtherFailure()
    w._emit_lock_state_best_effort()
    assert len(records) == 2, records


def test_faults_go_to_the_log_not_stdout():
    """No fault path ends at a print: a windowed build has no stdout."""
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


def test_connect_tries_a_few_times_then_says_so():
    """One attempt was not enough to act on.

    An RP that is booting, or an mDNS name that has not propagated, fails
    once and then works. A host that is off fails every time, and then the
    operator has to be told it stopped trying.
    """
    import linien_worker

    w = LinienWorker(host="fake.local")
    w.RETRY_DELAY_S = 0  # the test is about the count, not the wait
    statuses = []
    w.connection_status.connect(statuses.append)

    attempts = []

    def always_fails():
        attempts.append(1)
        raise RuntimeError("no route to host")

    w._connect_once = always_fails
    assert w._connect_with_retries() is False
    assert len(attempts) == w.CONNECT_ATTEMPTS, attempts
    # Two "still trying" statuses, then exactly one error.
    assert [s.split(":")[0] for s in statuses] == [
        "connecting", "connecting", "error"
    ], statuses
    assert "3 attempts" in statuses[-1], statuses[-1]
    assert "RuntimeError" in statuses[-1], statuses[-1]

    # A connect that comes good on a later attempt is not an error at all.
    w = LinienWorker(host="fake.local")
    w.RETRY_DELAY_S = 0
    statuses = []
    w.connection_status.connect(statuses.append)
    calls = []

    def fails_once():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("not yet")

    w._connect_once = fails_once
    assert w._connect_with_retries() is True
    assert not any(s.startswith("error") for s in statuses), statuses


def test_link_health_reports_one_edge_each_way():
    """A poll loop that fails keeps failing; the operator is told once.

    connection_status used to be emitted only at connect, so a device that
    went away mid-session still read as OK -- and its failure was written
    to the log at the poll rate instead.
    """
    from mot_workers import LinkHealth

    link = LinkHealth()
    # One bad frame on a 9600-baud line is not a disconnection.
    assert not link.failed()
    assert not link.failed()
    assert link.failed(), "third consecutive failure is the down edge"
    assert not link.failed(), "down edge must not repeat"
    assert link.ok(), "recovery is the up edge"
    assert not link.ok(), "up edge must not repeat"

    # A success resets the run, so intermittent failures never latch down.
    link = LinkHealth()
    for _ in range(10):
        assert not link.failed()
        assert not link.ok()


def test_writes_are_refused_when_the_connect_never_succeeded():
    """`self.client` exists before connect() runs, so it is not proof.

    A failed connect leaves a live LinienClient with no `parameters`, and
    every later write reported `'LinienClient' object has no attribute
    'parameters'` -- an AttributeError where the answer was "there is no
    server to write to".
    """
    import mot_logging

    w = worker()
    records = []
    w.event_log = mot_logging.EventLog(".")
    w.event_log._write = lambda kind, event, fields: records.append(
        (kind, fields.get("message", ""))
    )
    w.client.connected = False

    w.set_modulation_amplitude(500)
    assert w.client.parameters.modulation_amplitude.value == 0, "wrote anyway"
    assert not w.client.connection.root.calls, w.client.connection.root.calls
    assert records and records[0][0] == "fault", records
    assert "not connected" in records[0][1], records


def test_a_stop_during_connect_ends_the_worker():
    """A connect takes up to ~20 s. run() used to set running = True once
    it finished, undoing a stop() that arrived meanwhile, and stop()'s
    wait() then hung the window for good (Auto-connect or close during a
    slow connect). The same flag pattern is in every mot_workers thread.
    """
    import threading

    w = LinienWorker(host="fake.local")

    def connect_then_get_stopped():
        w.client = FakeClient()
        w.running = False  # what stop() does before it waits

    w._connect_once = connect_then_get_stopped
    t = threading.Thread(target=w.run, daemon=True)
    t.start()
    t.join(timeout=3)
    assert not t.is_alive(), "run() kept looping after a mid-connect stop"


def main():
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
    print("WORKERS OK")


if __name__ == "__main__":
    main()
