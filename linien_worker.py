"""
Linien acquisition worker thread for the control/master laser.

Connects to a Red Pitaya running linien-server, streams the error and
monitor signals for the spectroscopy plot, and provides lock/unlock
control.

**The worker owns the RPyC client.** Nothing outside this thread may
touch ``self.client``: RPyC connections are not thread-safe, and the
acquisition loop is reading from it continuously. Every public method
below is therefore a non-blocking *request* -- it appends to a
mutex-guarded queue that the run loop drains -- and every read comes back
as a signal rather than a return value. This is the same pattern
``RidenDPSWorker._queue`` uses.

Long operations (lock, unlock) do not block the loop either. They arm a
deadline that the loop checks each pass, so acquisition keeps running and
the plot stays live while a lock is being acquired.

Originally from MOT_GUI-main/Linien_Worker.py.
"""

import pickle
from time import monotonic

import numpy as np
from linien_client.connection import LinienClient
from linien_client.device import Device
from linien_common.common import MHz, AutolockMode, Vpp
from pyqtgraph.Qt import QtCore

from mot_logging import NULL_LOG

# Counts per volt on the Red Pitaya ADC, for the display copy of the
# error signal. The autolock is handed raw counts instead -- see
# _cmd_lock.
COUNTS_PER_VOLT = 8192.0


class LinienWorker(QtCore.QThread):
    new_data = QtCore.pyqtSignal(object)
    initial = QtCore.pyqtSignal(object)
    lock_state = QtCore.pyqtSignal(bool)  # True if locked, False otherwise
    connection_status = QtCore.pyqtSignal(str)  # "connected:<host>"/"disconnected"
    fault = QtCore.pyqtSignal(str)  # anything that would otherwise be silent
    # Reads come back as signals: the GUI thread must never block on RPyC.
    pid_read = QtCore.pyqtSignal(object)  # (p, i, d) or None
    filters_read = QtCore.pyqtSignal(object)  # dict or None

    # Setpoints conflate to the latest value, so dragging a slider results
    # in one write rather than a replayed burst. Actions (lock, unlock,
    # reads) keep their ordering.
    CONFLATE = frozenset({
        "mod_amplitude", "mod_frequency", "mod_phase", "sweep_speed",
        "sweep", "pid", "filters", "determine_offset", "stop_sweep",
    })

    POLL_MS = 200
    LOCK_TIMEOUT_S = 30
    UNLOCK_TIMEOUT_S = 10

    def __init__(
        self,
        host="rp-F0C6AD.local",
        username="root",
        password="root",
        event_log=None,
        unlock_on_stop=True,
    ):
        super().__init__()
        # Cleared by the shutdown dialog when the operator chooses to
        # leave the laser locked past the app.
        self.unlock_on_stop = bool(unlock_on_stop)
        self.host = host
        self.username = username
        self.password = password
        self.event_log = event_log or NULL_LOG
        self.running = False
        self.current_center = 0.0
        self.current_amplitude = 1.0
        self.to_plot = {}
        self.err = np.array([])
        self.mon = np.array([])
        self.voltages = np.array([])
        self.client = None
        self._last_ui_lock_state = None  # debounce UI lock-state updates
        self._mutex = QtCore.QMutex()
        self._pending = []
        # Armed by lock/unlock; checked each pass so the loop keeps
        # acquiring while the transition settles.
        self._await_lock = None  # (want_locked, deadline_monotonic)

    # ------------------------------------------------------------------
    # Public API -- all called from the GUI thread, none of it blocking
    # ------------------------------------------------------------------

    def set_modulation_amplitude(self, amplitude_mV: float):
        """Set the modulation amplitude in mV (0-1000)."""
        self._queue("mod_amplitude", float(amplitude_mV))

    def set_modulation_frequency(self, freq_khz: float):
        """Set the lock-in modulation frequency in kHz."""
        self._queue("mod_frequency", float(freq_khz))

    def set_modulation_phase(self, phase_deg: float):
        """Set the demodulation phase in degrees."""
        self._queue("mod_phase", float(phase_deg))

    def set_sweep_speed(self, speed: int):
        """Set the sweep speed (0-15, see Linien sweep speed table)."""
        self._queue("sweep_speed", int(speed))

    def stop_sweep(self):
        """Stop the sweep and hold the output at the current center."""
        self._queue("stop_sweep")

    def set_pid(self, p_value: int, i_value: int, d_value: int):
        """Write the lock PID gains (each 0-8191)."""
        self._queue("pid", int(p_value), int(i_value), int(d_value))

    def set_filters(self, settings: dict):
        """Write the channel-A error-signal filters."""
        self._queue("filters", dict(settings))

    def set_determine_offset(self, enabled: bool):
        """linien's "Determine signal offset" checkbox.

        The server reads this parameter inside exposed_start_autolock, so
        it only has to be set before a lock starts.
        """
        self._queue("determine_offset", bool(enabled))

    def request_sweep_change(self, center: float, amplitude: float):
        self._queue("sweep", float(center), float(amplitude))

    def request_pid(self):
        """Ask for the PID gains; answered on ``pid_read``."""
        self._queue("read_pid")

    def request_filters(self):
        """Ask for the filter settings; answered on ``filters_read``."""
        self._queue("read_filters")

    def lock_laser(self, x0, x1, n_points):
        """Start the autolock over a selected index range.

        ``x0``/``x1`` are indices into the plotted trace and ``n_points``
        its length.
        """
        self._queue("lock", int(x0), int(x1), int(n_points))

    def unlock_laser(self):
        self._queue("unlock")

    def stop(self):
        self.running = False
        self.wait()

    # ------------------------------------------------------------------
    # Command queue
    # ------------------------------------------------------------------

    def _queue(self, kind, *args):
        with QtCore.QMutexLocker(self._mutex):
            if kind in self.CONFLATE:
                self._pending = [p for p in self._pending if p[0] != kind]
            self._pending.append((kind, args))

    def _pop_pending(self):
        with QtCore.QMutexLocker(self._mutex):
            items = self._pending
            self._pending = []
        return items

    def _process_pending(self):
        for kind, args in self._pop_pending():
            handler = getattr(self, f"_cmd_{kind}", None)
            if handler is None:
                self._fault(f"unknown command: {kind}")
                continue
            try:
                handler(*args)
            except Exception as e:
                # A failed command must not take the acquisition loop
                # down with it -- the next one may well succeed.
                self._fault(f"{kind} failed: {e}")

    def _fault(self, message):
        self.event_log.fault("linien", message=message)
        self.fault.emit(message)

    # ------------------------------------------------------------------
    # Thread body
    # ------------------------------------------------------------------

    def run(self):
        if not self._connect():
            return

        self.running = True
        while self.running:
            self._process_pending()
            if not self.running:
                break
            try:
                self._acquire_once()
            except Exception as e:
                self._fault(f"acquisition failed: {e}")
            self._check_lock_transition()
            self._emit_lock_state_best_effort()
            self.msleep(self.POLL_MS)

        # Commands queued just before stop() -- zeroing the dither, most
        # importantly -- would otherwise never reach the device, because
        # the loop tests self.running before draining.
        self._process_pending()
        self._teardown()
        self.event_log.log("linien_loop_ended", host=self.host)

    def _teardown(self):
        """Leave the Red Pitaya in a safe resting state (invariant 7)."""
        if not self.unlock_on_stop:
            self.event_log.log("lock_left_engaged", reason="operator_choice")
            return
        # Sweeping rather than held at a lock point: an unattended lock
        # drifts off the line with nothing watching it.
        try:
            with self.event_log.command("linien.unlock", reason="worker_stop"):
                self.client.connection.root.start_sweep()
        except Exception as e:
            self.event_log.fault(
                "linien", message=f"could not start sweep on stop: {e}"
            )

    def _connect(self):
        try:
            with self.event_log.command("linien.connect", host=self.host):
                dev = Device(
                    host=self.host,
                    username=self.username,
                    password=self.password,
                )
                self.client = LinienClient(dev)
                self.client.connect(
                    autostart_server=True, use_parameter_cache=True
                )
        except Exception as e:
            self.client = None
            self.event_log.fault(
                "linien", message=f"connect failed ({self.host}): {e}"
            )
            self.connection_status.emit("disconnected")
            return False

        params = self.client.parameters
        self.current_amplitude = float(params.sweep_amplitude.value)
        self.current_center = float(params.sweep_center.value)
        self.initial.emit(
            {"center": self.current_center, "amplitude": self.current_amplitude}
        )

        # Force the SIMPLE autolock. This must be the *preference*: the
        # server derives autolock_mode from it when a lock starts, so
        # setting autolock_mode here would be overwritten.
        # autolock_determine_offset is left alone -- the UI owns it.
        try:
            with self.event_log.command("linien.autolock_setup"):
                params.fetch_additional_signals.value = False
                params.autolock_mode_preference.value = AutolockMode.SIMPLE
                self.client.connection.root.write_registers()
        except Exception as e:
            self._fault(f"autolock setup failed: {e}")

        # Anything queued while disconnected is stale -- a lock request
        # aimed at a spectrum from the previous session must not run now.
        self._pop_pending()

        self.connection_status.emit(f"connected:{self.host}")
        self._emit_lock_state_best_effort()
        return True

    def _acquire_once(self):
        fetched = pickle.loads(self.client.parameters.to_plot.value)
        self.to_plot = fetched if isinstance(fetched, dict) else {}

        err_key = next(
            (k for k in ("error_signal_1", "error_signal") if k in self.to_plot),
            None,
        )
        mon_key = "monitor_signal" if "monitor_signal" in self.to_plot else None

        empty = np.array([], dtype=float)
        err_raw = (
            np.asarray(self.to_plot.get(err_key, []), dtype=float)
            if err_key else empty
        )
        mon_raw = (
            np.asarray(self.to_plot.get(mon_key, []), dtype=float)
            if mon_key else empty
        )

        self.err = err_raw / COUNTS_PER_VOLT if err_raw.size else empty
        self.mon = mon_raw / COUNTS_PER_VOLT if mon_raw.size else empty

        n = max(self.err.size, self.mon.size)
        params = self.client.parameters
        if n > 1:
            cen = float(params.sweep_center.value)
            amp = float(params.sweep_amplitude.value)
            self.voltages = np.linspace(cen - amp, cen + amp, n)
        elif n == 1:
            self.voltages = np.array([float(params.sweep_center.value)])
        else:
            self.voltages = empty

        self.new_data.emit({
            "voltages": self.voltages,
            "error_signal": self.err,
            "monitor_signal": self.mon,
        })

    # ------------------------------------------------------------------
    # Command handlers -- all run on the worker thread
    # ------------------------------------------------------------------

    def _write_registers(self):
        self.client.connection.root.write_registers()

    def _cmd_mod_amplitude(self, amplitude_mV):
        """``modulation_amplitude`` is in raw DAC units, not volts.

        linien's own ``Vpp`` constant (= 4095.75) is units per volt
        peak-to-peak; writing volts here made the dither ~4096x too small.
        """
        with self.event_log.command("linien.mod_amplitude", mv=amplitude_mV):
            self.client.parameters.modulation_amplitude.value = int(
                round(amplitude_mV / 1000.0 * Vpp)
            )
            self._write_registers()

    def _cmd_mod_frequency(self, freq_khz):
        """``modulation_frequency`` is in raw phase-accumulator units.

        linien's ``MHz`` constant (= 0x10000000/8) is units per MHz;
        writing Hz here made the frequency ~33.55x too low.
        """
        with self.event_log.command("linien.mod_frequency", khz=freq_khz):
            self.client.parameters.modulation_frequency.value = int(
                round(freq_khz / 1000.0 * MHz)
            )
            self._write_registers()

    def _cmd_mod_phase(self, phase_deg):
        with self.event_log.command("linien.mod_phase", deg=phase_deg):
            self.client.parameters.demodulation_phase_a.value = int(phase_deg)
            self._write_registers()

    def _cmd_sweep_speed(self, speed):
        with self.event_log.command("linien.sweep_speed", speed=speed):
            self.client.parameters.sweep_speed.value = int(speed)
            self._write_registers()

    def _cmd_stop_sweep(self):
        with self.event_log.command("linien.stop_sweep"):
            self.client.parameters.sweep_amplitude.value = 0
            self._write_registers()

    def _cmd_sweep(self, center, amplitude):
        with self.event_log.command(
            "linien.sweep", center=center, amplitude=amplitude
        ):
            self.current_center = center
            self.current_amplitude = amplitude
            self.client.parameters.sweep_center.value = center
            self.client.parameters.sweep_amplitude.value = amplitude
            self._write_registers()

    def _cmd_pid(self, p_value, i_value, d_value):
        with self.event_log.command(
            "linien.pid", p=p_value, i=i_value, d=d_value
        ):
            params = self.client.parameters
            params.p.value = p_value
            params.i.value = i_value
            params.d.value = d_value
            self._write_registers()
        self._cmd_read_pid()  # report what actually took

    def _cmd_read_pid(self):
        params = self.client.parameters
        values = (
            int(params.p.value),
            int(params.i.value),
            int(params.d.value),
        )
        self.event_log.readback(
            "linien.pid", p=values[0], i=values[1], d=values[2]
        )
        self.pid_read.emit(values)

    def _cmd_filters(self, settings):
        with self.event_log.command("linien.filters", **settings):
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
            self._write_registers()

    def _cmd_read_filters(self):
        # Seven sequential RPyC hops, not one -- CLAUDE.md counts round
        # trips. Called on connect and on demand, never per poll.
        params = self.client.parameters
        settings = {
            "automatic": bool(params.filter_automatic_a.value),
            "filter_1_enabled": bool(params.filter_1_enabled_a.value),
            "filter_1_type": int(params.filter_1_type_a.value),
            "filter_1_frequency": int(params.filter_1_frequency_a.value),
            "filter_2_enabled": bool(params.filter_2_enabled_a.value),
            "filter_2_type": int(params.filter_2_type_a.value),
            "filter_2_frequency": int(params.filter_2_frequency_a.value),
        }
        self.event_log.readback("linien.filters", **settings)
        self.filters_read.emit(settings)

    def _cmd_determine_offset(self, enabled):
        with self.event_log.command("linien.determine_offset", enabled=enabled):
            self.client.parameters.autolock_determine_offset.value = enabled

    def _cmd_lock(self, x0, x1, n_points):
        """Start the autolock over a selected index range.

        The spectrum is re-read here in **raw ADC counts**: the plotted
        copy is divided by 8192 for display, and the autolock's peak
        finding and line-width estimate work in counts, so handing it
        volts degrades the lock badly. linien likewise sends the unscaled
        signal and divides only when drawing.
        """
        params = self.client.parameters
        try:
            self.client.connection.root.start_sweep()
        except Exception:
            pass
        # Let a full sweep land before sampling it. This is on the worker
        # thread, so it costs one acquisition frame, not a frozen GUI.
        self.msleep(400)

        to_plot = pickle.loads(params.to_plot.value)
        key = next(
            (k for k in ("error_signal_1", "error_signal") if k in to_plot),
            None,
        )
        if key is None:
            self._fault("no spectrum available to lock on")
            return

        raw = np.asarray(to_plot[key], dtype=np.int16)
        n = raw.size
        if n < 4:
            self._fault("spectrum too small to lock on")
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
            self._fault("selected range too narrow to lock on")
            return

        with self.event_log.command(
            "linien.lock", i0=i0, i1=i1, n=n,
            determine_offset=bool(params.autolock_determine_offset.value),
        ):
            # SIMPLE is forced through the *preference*: the server builds
            # an AutolockAlgorithmSelector from autolock_mode_preference
            # and assigns autolock_mode from it, so writing autolock_mode
            # here would just be overwritten. A non-AUTO_DETECT preference
            # short-circuits the selector.
            params.autolock_mode_preference.value = AutolockMode.SIMPLE
            params.fetch_additional_signals.value = False
            params.autolock_target_position.value = 0
            # Clear the previous verdict, or a stale True from an earlier
            # attempt fails this one before it has started.
            params.autolock_failed.value = False
            self._write_registers()
            self.client.connection.root.start_autolock(
                i0, i1, pickle.dumps(raw)
            )

        self._await_lock = (True, monotonic() + self.LOCK_TIMEOUT_S)

    def _cmd_unlock(self):
        with self.event_log.command("linien.unlock"):
            self.client.connection.root.start_sweep()
        self._await_lock = (False, monotonic() + self.UNLOCK_TIMEOUT_S)

    # ------------------------------------------------------------------
    # Lock state
    # ------------------------------------------------------------------

    def _read_locked(self):
        t = pickle.loads(self.client.parameters.to_plot.value)
        return "error_signal" in t or bool(self.client.parameters.lock.value)

    def _check_lock_transition(self):
        """Resolve a pending lock/unlock without blocking the loop."""
        if self._await_lock is None:
            return
        want, deadline = self._await_lock
        try:
            reached = self._read_locked() == want
        except Exception:
            reached = False
        if reached:
            self._await_lock = None
            self.event_log.log("lock_state", locked=want)
            self._emit_lock_state_best_effort(force_value=want)
            return

        # linien reports its own verdict rather than making us infer one
        # from a timeout: autolock_failed means the algorithm gave up
        # looking for the line. Without this a failed lock is a silent
        # 30 s wait and then a generic message.
        if want:
            try:
                params = self.client.parameters
                if bool(params.autolock_failed.value):
                    self._await_lock = None
                    params.autolock_failed.value = False
                    self._fault(
                        "autolock could not find the selected line — "
                        "try a wider selection around a stronger feature"
                    )
                    self._emit_lock_state_best_effort()
                    return
            except Exception:
                pass  # older server without the parameter: fall back to timeout

        if monotonic() >= deadline:
            self._await_lock = None
            self._fault(
                f"{'lock' if want else 'unlock'} did not take within "
                f"{self.LOCK_TIMEOUT_S if want else self.UNLOCK_TIMEOUT_S} s"
            )
            self._emit_lock_state_best_effort()

    def _emit_lock_state_best_effort(self, force_value=None):
        if self.client is None:
            return
        try:
            locked = (
                bool(force_value) if force_value is not None
                else self._read_locked()
            )
            if locked != self._last_ui_lock_state:
                self._last_ui_lock_state = locked
                # Logged on every transition, not just the ones we asked
                # for: a lock that drops on its own is the interesting
                # case and nothing else would record it.
                self.event_log.log("lock_state", locked=locked)
                self.lock_state.emit(locked)
        except Exception as e:
            self.event_log.fault("linien", message=f"lock-state read failed: {e}")
