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
from time import sleep
from linien_common.common import MHz, AutolockMode, Vpp

from mot_logging import NULL_LOG


class LinienWorker(QtCore.QThread):
    new_data = QtCore.pyqtSignal(object)
    initial = QtCore.pyqtSignal(object)
    lock_state = QtCore.pyqtSignal(bool)  # True if locked, False otherwise
    connection_status = QtCore.pyqtSignal(str)  # "connected:<host>" or "disconnected"
    fault = QtCore.pyqtSignal(str)  # anything that would otherwise be silent

    def __init__(self, host="rp-F0C6AD.local", username="root", password="root",
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

    def run(self):
        try:
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

            self.event_log.log("linien_connected", host=self.host)
            self.connection_status.emit(f"connected:{self.host}")

            # Emit initial lock state (best-effort)
            self._emit_lock_state_best_effort()

        except Exception as e:
            self._log(f"connect failed ({self.host}): {e}")
            self.connection_status.emit("disconnected")
            return

        # Main acquisition loop
        self.running = True
        while self.running:
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

                # Scale to float
                self.err = err_raw / 8192.0 if err_raw.size else np.array([], dtype=float)
                self.mon = mon_raw / 8192.0 if mon_raw.size else np.array([], dtype=float)

                n_err = self.err.size
                n_mon = self.mon.size
                N = max(n_err, n_mon)

                if N > 1:
                    cen = float(self.client.parameters.sweep_center.value)
                    amp = float(self.client.parameters.sweep_amplitude.value)
                    self.voltages = np.linspace(cen - amp, cen + amp, N)
                elif N == 1:
                    self.voltages = np.array([float(self.client.parameters.sweep_center.value)])
                else:
                    self.voltages = np.array([], dtype=float)

                self.new_data.emit(
                    {
                        "voltages": self.voltages,
                        "error_signal": self.err,
                        "monitor_signal": self.mon,
                    }
                )
            except Exception as e:
                self._log("acquisition failed: " + str(e))

            self._emit_lock_state_best_effort()
            self.msleep(200)

        self.event_log.log("linien_loop_ended", host=self.host)

    def stop(self):
        self.running = False
        # Sweeping rather than held at a lock point: an unattended lock
        # drifts off the line with nothing watching it. Skipped when the
        # operator chose to leave it engaged.
        try:
            if self.unlock_on_stop and getattr(self, "client", None):
                self.client.connection.root.start_sweep()
            elif not self.unlock_on_stop:
                self.event_log.log("lock_left_engaged", reason="operator_choice")
        except Exception as e:
            self._log("could not start sweep on stop: " + str(e))
        self.wait()

    def _log(self, message):
        """Record what would otherwise have gone to stdout."""
        self.event_log.fault("linien", message=str(message))

    def stop_sweep(self):
        """Stop the sweep and hold the output at the current center voltage."""
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
        try:
            self.client.parameters.modulation_frequency.value = int(
                round(freq_khz / 1000.0 * MHz)
            )
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"mod freq update failed: {e}")

    def set_modulation_phase(self, phase_deg: float):
        """Set the demodulation phase in degrees."""
        try:
            self.client.parameters.demodulation_phase_a.value = int(phase_deg)
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"mod phase update failed: {e}")

    def set_sweep_speed(self, speed: int):
        """Set the sweep speed (0–15, see Linien sweep speed table)."""
        try:
            self.client.parameters.sweep_speed.value = int(speed)
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log(f"sweep speed update failed: {e}")

    def get_pid(self):
        """Read the lock PID gains as (p, i, d), or None if unavailable."""
        if not hasattr(self, "client"):
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
        if not hasattr(self, "client"):
            self._log("PID update failed: not connected")
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
        if not hasattr(self, "client"):
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
        if not hasattr(self, "client"):
            self._log("filter update failed: not connected")
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
        try:
            self.current_center = center
            self.current_amplitude = amplitude
            self.client.parameters.sweep_center.value = center
            self.client.parameters.sweep_amplitude.value = amplitude
            self.client.connection.root.write_registers()
        except Exception as e:
            self._log("sweep update failed: " + str(e))

    def _wait_for_lock(self, should_be_locked: bool, timeout_s: int = 30):
        for _ in range(timeout_s):
            try:
                t = pickle.loads(self.client.parameters.to_plot.value)
                has_locked_key = "error_signal" in t
                param_lock = bool(self.client.parameters.lock.value)
                status = has_locked_key or param_lock
                if status == should_be_locked:
                    return True
            except Exception:
                pass
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
        params = self.client.parameters
        try:
            self.client.connection.root.start_sweep()
        except Exception:
            pass
        sleep(0.4)  # let a full sweep land before sampling it

        try:
            to_plot = pickle.loads(params.to_plot.value)
        except Exception as e:
            self.fault.emit(f"could not read spectrum: {e}")
            return
        key = next(
            (k for k in ("error_signal_1", "error_signal") if k in to_plot),
            None,
        )
        if key is None:
            self.fault.emit("no spectrum available to lock on")
            return

        raw = np.asarray(to_plot[key], dtype=np.int16)
        n = raw.size
        if n < 4:
            self.fault.emit("spectrum too small to lock on")
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
            self.fault.emit("selected range too narrow to lock on")
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
            self.fault.emit(f"start_autolock failed: {e}")
            return

        if self._wait_for_lock(True, timeout_s=30):
            self._emit_lock_state_best_effort(force_value=True)
        else:
            self.fault.emit("lock did not engage within 30 s")
            self._emit_lock_state_best_effort()

    def set_determine_offset(self, enabled: bool):
        """linien's "Determine signal offset" checkbox.

        The server reads this parameter inside exposed_start_autolock, so
        it only has to be set before a lock starts.
        """
        try:
            self.client.parameters.autolock_determine_offset.value = bool(
                enabled
            )
        except Exception as e:
            self.fault.emit(f"determine-offset update failed: {e}")

    def unlock_laser(self):
        
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
        if not hasattr(self, "client"):
            return
        try:
            if force_value is None:
                t = pickle.loads(self.client.parameters.to_plot.value)
                has_locked_key = "error_signal" in t
                param_lock = bool(self.client.parameters.lock.value)
                locked = has_locked_key or param_lock
            else:
                locked = bool(force_value)

            if locked != self._last_ui_lock_state:
                self._last_ui_lock_state = locked
                self.lock_state.emit(locked)
        except Exception:
            pass
