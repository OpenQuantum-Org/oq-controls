"""
Linien acquisition worker thread for the control/master laser.

Connects to a Red Pitaya running linien-server, streams the error and
monitor signals for the spectroscopy plot, and provides lock/unlock control.

Originally from MOT_GUI-main/Linien_Worker.py.
"""

from linien_client.connection import LinienClient
from pyqtgraph.Qt import QtCore
from linien_client.device import Device
import pickle
import numpy as np
from time import monotonic, sleep
from linien_common.common import MHz, AutolockMode, Vpp

from mot_logging import NULL_LOG
from mot_workers import LinkHealth, drain_parameter_queue

# See linien_widget: rp-F0C6AD was a different Red Pitaya.
DEFAULT_RP_HOST = "rp-f0f2b5.local"


class LinienWorker(QtCore.QThread):
    new_data = QtCore.pyqtSignal(object)
    initial = QtCore.pyqtSignal(object)
    lock_state = QtCore.pyqtSignal(bool)  # True if locked, False otherwise
    connection_status = QtCore.pyqtSignal(str)  # "connected:<host>" or "disconnected"
    fault = QtCore.pyqtSignal(str)  # anything that would otherwise be silent
    # True when the server's heartbeat has stopped, False when it resumes.
    server_stalled = QtCore.pyqtSignal(bool)
    # The dither amplitude the server is actually running, in mV, read once
    # at connect. Nothing else told the UI whether the dither was on.
    modulation_state = QtCore.pyqtSignal(object)
    # Why a lock the operator asked for did not happen, for display by the
    # lock buttons. Also sent as a fault, so the log keeps it.
    lock_feedback = QtCore.pyqtSignal(str)
    # (paused, speed index) as the server holds them, read once at connect.
    sweep_state = QtCore.pyqtSignal(object)

    # linien-server increments ``ping`` once a second from a thread of its
    # own (linien_server/server.py:181). If it stops advancing the server's
    # threads are dead, which is the one thing a frozen plot cannot tell
    # you by itself: a static spectrum is legitimate, a static heartbeat
    # is not.
    STALL_AFTER_S = 5.0

    # A few attempts, then stop and say so. An RP that is still booting, or
    # an mDNS name that has not propagated yet, fails once and then works;
    # a host that is off fails every time and the operator needs telling.
    CONNECT_ATTEMPTS = 3
    RETRY_DELAY_S = 2.0

    def __init__(self, host=DEFAULT_RP_HOST, username="root", password="root",
                 event_log=None, unlock_on_stop=True):
        super().__init__()
        # Faults go to the log, never to stdout (CLAUDE.md invariant 4).
        self.event_log = event_log or NULL_LOG
        # Cleared by the shutdown dialog when the operator chooses to
        # leave the laser locked past the app.
        self.unlock_on_stop = bool(unlock_on_stop)
        self.host = host
        self.username = username
        self.password = password
        self.running = False
        self.current_center = 0.0
        self.current_amplitude = 1.0
        self.to_plot = {}
        self.err = np.array([])
        self.mon = np.array([])
        self.voltages = np.array([])
        self._last_ui_lock_state = None  # debounce UI lock-state updates
        self._last_ping = None
        self._ping_changed_at = None
        self._ping_ever_moved = False
        self._stalled = False
        self._last_lock_read_error = None
        self._link = LinkHealth()

    def _check_server_alive(self):
        """Watch the server's heartbeat; report the edges, not every poll."""
        try:
            ping = int(self.client.parameters.ping.value)
        except Exception:
            return  # a read that fails is a connection problem, not a stall
        now = monotonic()
        if ping != self._last_ping:
            if self._last_ping is not None:
                self._ping_ever_moved = True
            self._last_ping = ping
            self._ping_changed_at = now
            if self._stalled:
                self._stalled = False
                self.event_log.log("linien_server_recovered", host=self.host)
                self.server_stalled.emit(False)
            return
        if self._ping_changed_at is None:
            self._ping_changed_at = now
            return
        quiet_s = now - self._ping_changed_at
        if not self._ping_ever_moved:
            # Never seen it tick, so a still value proves nothing about
            # the server -- it is far more likely we are not reading it
            # properly. Accusing a healthy server of being wedged is the
            # worse error, so say nothing until the heartbeat is trusted.
            return
        if not self._stalled and quiet_s >= self.STALL_AFTER_S:
            self._stalled = True
            self.event_log.fault(
                "linien",
                message=(
                    f"server heartbeat stopped {quiet_s:.0f} s ago — "
                    "it is wedged, restart it"
                ),
                host=self.host,
            )
            self.server_stalled.emit(True)

    def run(self):
        if not self._connect_with_retries():
            return
        # Main acquisition loop
        self._acquire()

    def _connect_with_retries(self):
        """Connect, trying a few times. True once the server answered.

        One attempt was not enough to act on: an RP that is booting, or a
        laptop whose mDNS has not caught up, fails once and then works.
        Bounded on purpose -- after the last attempt the widget says to use
        Auto-connect, which keeps reconnecting the operator's decision
        rather than something the app does on a timer forever.
        """
        for attempt in range(1, self.CONNECT_ATTEMPTS + 1):
            try:
                self._connect_once()
                return True
            except Exception as e:
                # Class name, not just str(e): linien raises a message-less
                # GeneralConnectionError for host-not-found (it logs the
                # real reason to its own logger, which
                # capture_library_logging routes into ours).
                detail = (
                    f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                )
                self._log(
                    f"connect failed ({self.host}) "
                    f"attempt {attempt}/{self.CONNECT_ATTEMPTS}: {detail}"
                )
                if attempt < self.CONNECT_ATTEMPTS:
                    self.connection_status.emit(
                        f"connecting:{self.host} — attempt {attempt + 1} of "
                        f"{self.CONNECT_ATTEMPTS}"
                    )
                    sleep(self.RETRY_DELAY_S)
                    continue
                # "error:" rather than "disconnected": never having reached
                # the device is a different thing from losing it, and only
                # the first one means check the host and the cabling.
                self.connection_status.emit(
                    f"error:{detail} (after {self.CONNECT_ATTEMPTS} attempts)"
                )
                return False
        return False

    def _connect_once(self):
        dev = Device(host=self.host, username=self.username, password=self.password)
        self.client = LinienClient(dev)
        self.client.connect(autostart_server=True, use_parameter_cache=True)

        # Initialize sweep params
        self.current_amplitude = float(self.client.parameters.sweep_amplitude.value)
        self.current_center = float(self.client.parameters.sweep_center.value)

        self.initial.emit(
            {"center": self.current_center, "amplitude": self.current_amplitude}
        )

        # Force the SIMPLE autolock. This must be the *preference*:
        # the server derives autolock_mode from it when a lock starts,
        # so setting autolock_mode here would be overwritten.
        # autolock_determine_offset is left alone -- it is a user
        # setting the UI owns.
        try:
            p = self.client.parameters
            p.fetch_additional_signals.value = False
            p.autolock_mode_preference.value = AutolockMode.SIMPLE
            self.client.connection.root.write_registers()
        except Exception as e:
            self.fault.emit(f"autolock setup failed: {e}")

        # What the dither is actually doing, before anything is written.
        # Read here rather than from a GUI slot: it is one more hop on a
        # connection that is already open, and the answer is needed
        # before the startup report decides what to claim.
        self._emit_modulation_state()

        # Same reason for the sweep: the speed box restores its last
        # setting, which after a restart can say "hold" while the server
        # sweeps -- and Lock at centre then refuses with no visible cause.
        try:
            p = self.client.parameters
            self.sweep_state.emit(
                (bool(p.sweep_pause.value), int(p.sweep_speed.value))
            )
        except Exception as e:
            self._log(f"sweep state read failed: {e}")

        self.event_log.log("linien_connected", host=self.host)
        self.connection_status.emit(f"connected:{self.host}")

        # Emit initial lock state (best-effort)
        self._emit_lock_state_best_effort()

    def _acquire(self):
        self.running = True
        while self.running:
            # Refreshes the local parameter cache and, just as
            # importantly, empties this client's queue on the server.
            drain_parameter_queue(self.client)
            try:
                fetched = pickle.loads(self.client.parameters.to_plot.value)
                self.to_plot = fetched if isinstance(fetched, dict) else {}

                # Choose error/monitor arrays depending on mode
                if "error_signal_1" in self.to_plot:
                    err_src_key = "error_signal_1"
                elif "error_signal" in self.to_plot:
                    err_src_key = "error_signal"
                else:
                    err_src_key = None

                mon_src_key = "monitor_signal" if "monitor_signal" in self.to_plot else None

                # "error_signal" (rather than "error_signal_1") is the
                # server's in-band statement that it is locked: the scope
                # channels are reassigned on lock, so the key names change
                # with them (linien_server/acquisition.py:161-177). Taken
                # from the frame itself rather than from parameters.lock,
                # which arrives on a different signal and can disagree with
                # the arrays by a poll.
                locked_frame = err_src_key == "error_signal"

                err_raw = (
                    np.asarray(self.to_plot.get(err_src_key, []), dtype=float)
                    if err_src_key
                    else np.array([], dtype=float)
                )
                mon_raw = (
                    np.asarray(self.to_plot.get(mon_src_key, []), dtype=float)
                    if mon_src_key
                    else np.array([], dtype=float)
                )

                # The sweep scope taps fast_a_out_i, upstream of the
                # combined_offset adder (registers.py:203 vs
                # linien_module.py:280), so the trace would sit still while
                # the PID sees it shifted. Add it here to show what the lock
                # acts on. Same 14-bit counts (scopegen.py:85). The locked
                # trace is logic_combined_error_signal, already offset.
                # Cached parameter, so autolock's own offset shows up too.
                offset_counts = int(self.client.parameters.combined_offset.value)
                if err_raw.size and not locked_frame:
                    err_raw = err_raw + float(offset_counts)

                # Scale to float
                self.err = err_raw / 8192.0 if err_raw.size else np.array([], dtype=float)
                self.mon = mon_raw / 8192.0 if mon_raw.size else np.array([], dtype=float)
                # Where the lock is sitting, in the sweep's own x units.
                # control_signal is the whole fast DAC output after the
                # limiter (linien_module.py:147, fed from fast_outs at
                # :410): PID + mod + the sweep held where the lock engaged
                # (:99) + out_offset. So it is already an absolute output
                # voltage; nothing needs adding. Only present while locked;
                # /8192 like the other traces, as linien-gui does
                # (plot_widget.py:482).
                ctrl_raw = self.to_plot.get("control_signal")
                if locked_frame and ctrl_raw is not None:
                    ctrl = np.asarray(ctrl_raw, dtype=float)
                    ctrl_mean = float(np.mean(ctrl)) / 8192.0 if ctrl.size else None
                else:
                    ctrl_mean = None

                n_err = self.err.size
                n_mon = self.mon.size
                N = max(n_err, n_mon)

                # Cached locally (use_parameter_cache=True), so reading it
                # outside the branches below costs no round trip.
                cen = float(self.client.parameters.sweep_center.value)
                if N > 1:
                    amp = float(self.client.parameters.sweep_amplitude.value)
                    self.voltages = np.linspace(cen - amp, cen + amp, N)
                elif N == 1:
                    self.voltages = np.array([cen])
                else:
                    self.voltages = np.array([], dtype=float)

                self.new_data.emit(
                    {
                        "voltages": self.voltages,
                        "error_signal": self.err,
                        "monitor_signal": self.mon,
                        "locked": locked_frame,
                        "center": cen,
                        "control_mean": ctrl_mean,
                        "combined_offset_v": offset_counts / 8192.0,
                    }
                )
            except Exception as e:
                if self._link.failed():
                    # A closed RPyC stream never recovers on its own, so
                    # the row must stop saying OK. Once, not per poll.
                    self.connection_status.emit(f"error:{type(e).__name__}: {e}")
                self._log("acquisition failed: " + str(e))
            else:
                if self._link.ok():
                    self.connection_status.emit(f"connected:{self.host}")

            self._emit_lock_state_best_effort()
            self._check_server_alive()
            self.msleep(200)

        self.event_log.log("linien_loop_ended", host=self.host)

    def stop(self):
        self.running = False
        # Sweeping rather than held at a lock point: an unattended lock
        # drifts off the line with nothing watching it. Skipped when the
        # operator chose to leave it engaged.
        try:
            if self.unlock_on_stop and self.connected:
                self.client.connection.root.start_sweep()
            elif not self.unlock_on_stop:
                self.event_log.log("lock_left_engaged", reason="operator_choice")
        except Exception as e:
            self._log("could not start sweep on stop: " + str(e))
        self.wait()

    def _log(self, message):
        """Record what would otherwise have gone to stdout."""
        self.event_log.fault("linien", message=str(message))

    def _emit_modulation_state(self):
        """Report the dither amplitude the server is running, in mV.

        ``modulation_amplitude`` is in DAC units -- ``Vpp`` of them per volt
        peak-to-peak -- so this is the inverse of set_modulation_amplitude.
        Best-effort: a connect that otherwise worked must stand.
        """
        try:
            counts = float(self.client.parameters.modulation_amplitude.value)
        except Exception as e:
            self._log(f"mod amplitude read failed: {e}")
            return
        self.modulation_state.emit(counts / Vpp * 1000.0)

    @property
    def connected(self):
        """True once connect() reached the server.

        ``self.client`` is assigned before ``connect()`` runs, so its mere
        existence proves nothing: after a failed connect it is a live
        object with no ``parameters``, which is why writes used to report
        ``'LinienClient' object has no attribute 'parameters'`` instead of
        saying they had nowhere to go.

        This is "the handshake succeeded", not "the link is up now" --
        linien clears its flag only in ``disconnect()``, so a cable pulled
        mid-session leaves it True. That is the right test for the guards
        below; the ``except`` arms are what cover a link that dies later.
        """
        return bool(getattr(getattr(self, "client", None), "connected", False))

    def _require_connection(self, what):
        """Guard a write. Logs and returns False when there is no server."""
        if self.connected:
            return True
        self._log(f"{what} failed: not connected")
        return False

    def stop_sweep(self):
        """Stop the sweep and hold the output at the current center voltage."""
        if not self._require_connection("stop sweep"):
            return
        try:
            self.client.parameters.sweep_amplitude.value = 0
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"stop sweep failed: {e}")

    def set_modulation_amplitude(self, amplitude_mV: float):
        """Set the modulation amplitude in mV (0-1000).

        ``modulation_amplitude`` is in raw DAC units, not volts: linien's
        own ``Vpp`` constant (= 4095.75) is units per volt peak-to-peak.
        Writing volts here made the dither ~4096x too small.
        """
        if not self._require_connection("mod amplitude update"):
            return
        try:
            self.client.parameters.modulation_amplitude.value = int(
                round(amplitude_mV / 1000.0 * Vpp)
            )
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"mod amplitude update failed: {e}")

    def set_modulation_frequency(self, freq_khz: float):
        """Set the lock-in modulation frequency in kHz.

        ``modulation_frequency`` is in raw phase-accumulator units, not
        Hz: linien's ``MHz`` constant (= 0x10000000/8) is units per MHz.
        Writing Hz here made the frequency ~33.55x too low.
        """
        if not self._require_connection("mod freq update"):
            return
        try:
            self.client.parameters.modulation_frequency.value = int(
                round(freq_khz / 1000.0 * MHz)
            )
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"mod freq update failed: {e}")

    def set_modulation_phase(self, phase_deg: float):
        """Set the demodulation phase in degrees."""
        if not self._require_connection("mod phase update"):
            return
        try:
            self.client.parameters.demodulation_phase_a.value = int(phase_deg)
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"mod phase update failed: {e}")

    def set_sweep_pause(self, paused: bool):
        """Hold the sweep at ``sweep_center``, or let it run again.

        linien's own Start/Pause button, which is the only thing that
        writes this parameter (linien_gui/ui/sweep_control.py:86). Unlike
        stop_sweep() it leaves sweep_amplitude alone, so resuming returns
        to the same span rather than needing it set again.
        """
        if not self._require_connection("sweep pause update"):
            return
        try:
            self.client.parameters.sweep_pause.value = bool(paused)
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"sweep pause update failed: {e}")

    def set_sweep_speed(self, speed: int):
        """Set the sweep speed (0–15, see Linien sweep speed table)."""
        if not self._require_connection("sweep speed update"):
            return
        try:
            self.client.parameters.sweep_speed.value = int(speed)
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"sweep speed update failed: {e}")

    def get_pid(self):
        """Read the lock PID gains as (p, i, d), or None if unavailable."""
        if not self.connected:
            return None
        try:
            params = self.client.parameters
            return (
                int(params.p.value),
                int(params.i.value),
                int(params.d.value),
            )
        except Exception as e:
            self._log(f"PID read failed: {e}")
            return None

    def set_pid(self, p_value: int, i_value: int, d_value: int):
        """Write the lock PID gains (each 0–8191)."""
        if not self._require_connection("PID update"):
            return
        try:
            params = self.client.parameters
            params.p.value = int(p_value)
            params.i.value = int(i_value)
            params.d.value = int(d_value)
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"PID update failed: {e}")

    def get_filters(self):
        """Read the channel-A error-signal filter settings, or None."""
        if not self.connected:
            return None
        try:
            params = self.client.parameters
            return {
                "automatic": bool(params.filter_automatic_a.value),
                "filter_1_enabled": bool(params.filter_1_enabled_a.value),
                "filter_1_type": int(params.filter_1_type_a.value),
                "filter_1_frequency": int(params.filter_1_frequency_a.value),
                "filter_2_enabled": bool(params.filter_2_enabled_a.value),
                "filter_2_type": int(params.filter_2_type_a.value),
                "filter_2_frequency": int(params.filter_2_frequency_a.value),
            }
        except Exception as e:
            self._log(f"filter read failed: {e}")
            return None

    def set_filters(self, settings: dict):
        """Write the channel-A error-signal filters (see get_filters)."""
        if not self._require_connection("filter update"):
            return
        try:
            params = self.client.parameters
            params.filter_automatic_a.value = bool(settings["automatic"])
            params.filter_1_enabled_a.value = bool(settings["filter_1_enabled"])
            params.filter_1_type_a.value = int(settings["filter_1_type"])
            params.filter_1_frequency_a.value = int(
                settings["filter_1_frequency"]
            )
            params.filter_2_enabled_a.value = bool(settings["filter_2_enabled"])
            params.filter_2_type_a.value = int(settings["filter_2_type"])
            params.filter_2_frequency_a.value = int(
                settings["filter_2_frequency"]
            )
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"filter update failed: {e}")

    def request_sweep_change(self, center: float, amplitude: float):
        if not self._require_connection("sweep update"):
            return
        try:
            self.current_center = center
            self.current_amplitude = amplitude
            self.client.parameters.sweep_center.value = center
            self.client.parameters.sweep_amplitude.value = amplitude
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log("sweep update failed: " + str(e))

    def _wait_for_lock(self, should_be_locked: bool, timeout_s: int = 30):
        failures = 0
        for _ in range(timeout_s):
            try:
                t = pickle.loads(self.client.parameters.to_plot.value)
                has_locked_key = "error_signal" in t
                param_lock = bool(self.client.parameters.lock.value)
                status = has_locked_key or param_lock
                if status == should_be_locked:
                    return True
                failures = 0
            except Exception as e:
                # A read failing every second for 30 s is a broken
                # connection, not a lock that is taking its time -- and
                # the two are indistinguishable from the outside. Report
                # once rather than waiting out the full timeout in
                # silence.
                failures += 1
                if failures == 3:
                    self._log(f"lock-state polling is failing: {e}")
            sleep(1)
        return False

    def lock_laser(self, x0, x1, n_points):
        """Start the autolock over a selected index range.

        ``x0``/``x1`` are indices into the plotted trace and ``n_points``
        its length. The spectrum itself is re-read here in **raw ADC
        counts**: the plotted copy is divided by 8192 for display, and the
        autolock's peak finding and line-width estimate work in counts, so
        handing it volts degrades the lock badly. linien likewise sends
        the unscaled signal and divides only when drawing.
        """
        if not self.connected:
            self._lock_problem("Not connected to the Red Pitaya.")
            return
        params = self.client.parameters
        try:
            self.client.connection.root.start_sweep()
        except Exception as e:
            # Not fatal -- the spectrum below may still be usable -- but
            # it means the autolock is about to sample a trace that was
            # not freshly swept, which is worth knowing if it then fails.
            self._log(f"could not restart the sweep before locking: {e}")
        sleep(0.4)  # let a full sweep land before sampling it

        try:
            to_plot = pickle.loads(params.to_plot.value)
        except Exception as e:
            self._lock_problem(f"Could not read the spectrum: {e}")
            return
        key = next(
            (k for k in ("error_signal_1", "error_signal") if k in to_plot),
            None,
        )
        if key is None:
            self._lock_problem("No spectrum available to lock on.")
            return

        raw = np.asarray(to_plot[key], dtype=np.int16)
        n = raw.size
        if n < 4:
            self._lock_problem("The spectrum is too small to lock on.")
            return

        # The selection was made against a trace of n_points samples; map
        # it onto this spectrum in case the two differ.
        if n_points and n_points != n:
            scale = (n - 1) / float(n_points - 1)
            x0 = int(round(x0 * scale))
            x1 = int(round(x1 * scale))
        i0, i1 = sorted((int(x0), int(x1)))
        i0 = max(0, min(n - 1, i0))
        i1 = max(0, min(n - 1, i1))
        if i1 - i0 < 2:
            self._lock_problem("The selected range is too narrow; drag across the whole slope.")
            return

        try:
            # SIMPLE is forced through the *preference*: the server builds
            # an AutolockAlgorithmSelector from autolock_mode_preference and
            # assigns autolock_mode from it, so writing autolock_mode here
            # would just be overwritten. A non-AUTO_DETECT preference
            # short-circuits the selector.
            params.autolock_mode_preference.value = AutolockMode.SIMPLE
            params.fetch_additional_signals.value = False
            params.autolock_target_position.value = 0
            self.client.connection.root.write_registers()
        except Exception as e:
            self.fault.emit(f"autolock parameter set failed: {e}")

        try:
            self.client.connection.root.start_autolock(
                i0, i1, pickle.dumps(raw)
            )
        except Exception as e:
            self._lock_problem(f"The autolock could not start: {e}")
            return

        if self._wait_for_lock(True, timeout_s=30):
            self._emit_lock_state_best_effort(force_value=True)
        else:
            self._lock_problem("The lock did not engage within 30 s.")
            self._emit_lock_state_best_effort()

    # Half-width of the slope probe around the paused centre. Small against
    # the few-mV-wide MOT window seen on the bench, large enough that the
    # error moves clear of the frame-to-frame scatter.
    CENTRE_PROBE_V = 0.005
    # A held sweep still delivers traces at the held speed's rate: 8.6 s
    # each at index 15, which timed every probe out. Index 8 (14.8 Hz) is
    # ~70 ms per trace; the output stays at the centre either way, since
    # pause zeroes the ramp whatever its speed.
    PROBE_SWEEP_SPEED = 8
    # Good centre locks sat 2-4 mV from the centre; the one runaway seen
    # landed 190 mV away. 10 mV keeps clear of both.
    RUNAWAY_V = 0.010

    def _lock_problem(self, message):
        """A lock action that did not happen: log it and say it by the button.

        The log pane alone was not enough -- a refused click looked like
        a click that did nothing.
        """
        self.fault.emit(message)
        self.lock_feedback.emit(message)

    def _fresh_error_mean(self, prev, timeout_s=5.0):
        """Mean raw error (counts) of a frame taken after the last write.

        The first changed frame may straddle the register write, so the one
        after it is used. Returns (mean, frame) or (None, prev) on timeout.
        """
        changes = 0
        deadline = monotonic() + timeout_s
        while monotonic() < deadline:
            t = pickle.loads(self.client.parameters.to_plot.value)
            err = t.get("error_signal_1")
            if err is not None:
                err = np.asarray(err, dtype=float)
                if prev is None or err.size != prev.size or np.any(err != prev):
                    changes += 1
                    prev = err
                    if changes == 2:
                        return float(np.mean(err)), err
            sleep(0.1)
        return None, prev

    def lock_at_centre(self):
        """Lock where the paused sweep sits, rather than on a zero crossing.

        Reads the error at the paused centre, sets combined_offset so the
        PID sees zero there, takes the slope sign from a +/- probe of the
        centre itself (the plotted x axis is ~0.2 V out, so it cannot be
        trusted for this), then starts linien's manual lock
        (locking_panel.py:158). The lock engages at once because a paused
        sweep outputs 0, which is the SIMPLE autolock's target.
        """
        if not self.connected:
            self._lock_problem("Not connected to the Red Pitaya.")
            return
        params = self.client.parameters
        root = self.client.connection.root
        if not bool(params.sweep_pause.value):
            self._lock_problem("Set Sweep Speed to 0 Hz (hold) first.")
            return
        centre = float(params.sweep_center.value)
        speed = int(params.sweep_speed.value)
        d = self.CENTRE_PROBE_V
        reads = {}
        frame = None
        try:
            params.sweep_speed.value = min(speed, self.PROBE_SWEEP_SPEED)
            for name, v in (("c1", centre), ("hi", centre + d),
                            ("lo", centre - d), ("c2", centre)):
                params.sweep_center.value = v
                root.write_registers()
                reads[name], frame = self._fresh_error_mean(frame)
                if reads[name] is None:
                    self._lock_problem(
                        "No fresh error signal from the Red Pitaya within 5 s."
                    )
                    return
        except Exception as e:
            self._lock_problem(f"Could not measure the error signal: {e}")
            return
        finally:
            try:
                params.sweep_center.value = centre
                params.sweep_speed.value = speed
                root.write_registers()
            except Exception as e:
                self._lock_problem(
                    f"Could not restore the sweep centre and speed: {e}"
                )

        step = reads["hi"] - reads["lo"]
        scatter = abs(reads["c1"] - reads["c2"])
        e0 = reads["c2"]
        self.event_log.log(
            "centre_lock_probe",
            centre_v=round(centre, 4),
            error_v=round(e0 / 8192.0, 5),
            slope_v_per_v=round(step / (2 * d) / 8192.0, 3),
            scatter_v=round(scatter / 8192.0, 5),
        )
        # 14-bit signed adder (registers.py:125).
        if abs(e0) > 8191:
            self._lock_problem(
                "The error signal at the centre is beyond the offset range."
            )
            return
        if abs(step) < 3 * scatter + 1:
            self._lock_problem(
                "The error signal is too flat here to hold a lock "
                f"({step / 8192:+.4f} V over ±{d * 1000:.0f} mV). "
                "Move Sweep Centre 1–2 mV and try again."
            )
            return

        try:
            params.combined_offset.value = -int(round(e0))
            params.target_slope_rising.value = bool(step > 0)
            params.fetch_additional_signals.value = False
            params.autolock_mode.value = AutolockMode.SIMPLE
            params.autolock_target_position.value = 0
            root.write_registers()
            root.start_lock()
        except Exception as e:
            self._lock_problem(f"Lock at centre failed: {e}")
            self._emit_lock_state_best_effort()
            return

        if not self._wait_for_lock(True, timeout_s=10):
            self._lock_problem("The lock did not engage within 10 s.")
            self._emit_lock_state_best_effort()
            return

        drift = self._control_drift(centre)
        if drift is not None and drift > self.RUNAWAY_V:
            # Seen on the bench: a probe on a small dip riding a flat error
            # offset engaged, then slid ~190 mV along the flat stretch while
            # the header said LOCKED. The sweep stays held, so unlocking
            # parks the laser back at the centre.
            try:
                root.start_sweep()
            except Exception as e:
                self._log(f"unlock after runaway failed: {e}")
            self.event_log.fault(
                "centre_lock_runaway", centre_v=round(centre, 4),
                drift_v=round(drift, 4),
            )
            self._lock_problem(
                f"The lock ran {drift * 1000:.0f} mV away from the centre, "
                "so it was released. Adjust Mod Phase until the error signal "
                "has a clear slope here, then try again."
            )
            self._emit_lock_state_best_effort(force_value=False)
            return
        self._emit_lock_state_best_effort(force_value=True)

    def _control_drift(self, centre, settle_s=1.5):
        """Largest |control - centre| (V) over the first moments of a lock.

        A good centre lock holds the output within a few mV of the centre
        (2-4 mV across the bench locks of 2026-11-02); a runaway settles
        tens to hundreds of mV away within about a second. None if no
        locked frame arrived.
        """
        worst = None
        deadline = monotonic() + settle_s
        while monotonic() < deadline:
            try:
                t = pickle.loads(self.client.parameters.to_plot.value)
            except Exception:
                return worst
            ctrl = t.get("control_signal")
            if "error_signal" in t and ctrl is not None and len(ctrl):
                gap = abs(float(np.mean(ctrl)) / 8192.0 - centre)
                worst = gap if worst is None else max(worst, gap)
            sleep(0.1)
        return worst

    def set_determine_offset(self, enabled: bool):
        """linien's "Determine signal offset" checkbox.

        The server reads this parameter inside exposed_start_autolock, so
        it only has to be set before a lock starts.
        """
        if not self._require_connection("determine-offset update"):
            return
        try:
            self.client.parameters.autolock_determine_offset.value = bool(
                enabled
            )
        except Exception as e:
            self.fault.emit(f"determine-offset update failed: {e}")

    def unlock_laser(self):
        if not self._require_connection("unlock"):
            return
        try:
            self.client.connection.root.start_sweep()
            if self._wait_for_lock(False, timeout_s=10):
                pass
                self._emit_lock_state_best_effort(force_value=False)
            else:
                self._log("unlock not confirmed, but sweep was requested")
                self._emit_lock_state_best_effort()
        except Exception as e:
            self._log(f"unlock failed: {e}")
            self._emit_lock_state_best_effort()

    def _emit_lock_state_best_effort(self, force_value=None):
        if not self.connected:
            return
        try:
            if force_value is None:
                # Reuse the dict the acquisition pass just unpickled.
                # to_plot is a 2048-point array over RPyC and the only
                # uncached read in the loop; fetching it again here made
                # every poll carry it twice. linien names the signal
                # "error_signal" only while locked and "error_signal_1"
                # while sweeping (linien_server/acquisition.py:163-177).
                t = self.to_plot
                if not t:
                    t = pickle.loads(self.client.parameters.to_plot.value)
                locked = (
                    "error_signal" in t
                    or bool(self.client.parameters.lock.value)
                )
            else:
                locked = bool(force_value)

            if locked != self._last_ui_lock_state:
                self._last_ui_lock_state = locked
                self.lock_state.emit(locked)
        except Exception as e:
            # This decides whether the UI says LOCKED. Swallowing it
            # silently turns "the lock status is not updating" into an
            # unreproducible report, which is exactly what happened.
            # Logged once per distinct failure so a persistent one does
            # not fill the log at 5 Hz.
            if str(e) != self._last_lock_read_error:
                self._last_lock_read_error = str(e)
                self._log(f"lock-state read failed: {e}")
