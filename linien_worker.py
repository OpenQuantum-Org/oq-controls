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


class LinienWorker(QtCore.QThread):
    new_data = QtCore.pyqtSignal(object)
    initial = QtCore.pyqtSignal(object)
    lock_state = QtCore.pyqtSignal(bool)  # True if locked, False otherwise
    connection_status = QtCore.pyqtSignal(str)  # "connected:<host>" or "disconnected"

    def __init__(self, host="rp-F0C6AD.local", username="root", password="root"):
        super().__init__()
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

            # Configure simple autolock preference
            try:
                p = self.client.parameters
                p.fetch_additional_signals.value = False
                p.autolock_mode.value = AutolockMode.SIMPLE
                if hasattr(p, "autolock_determine_offset"):
                    p.autolock_determine_offset.value = False
                self.client.connection.root.write_registers()
            except Exception as e:
                print("Could not set autolock parameters:", e)

            print(f"Linien initialized and running (host: {self.host})")
            self.connection_status.emit(f"connected:{self.host}")

            # Emit initial lock state (best-effort)
            self._emit_lock_state_best_effort()

        except Exception as e:
            print(f"Failed to initialize linien (host: {self.host}): {e}")
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
                print("! Linien loop error:", e)

            self._emit_lock_state_best_effort()
            self.msleep(200)

        print("Linien loop ended.")

    def stop(self):
        self.running = False
        try:
            if hasattr(self, "client") and self.client:
                self.client.connection.root.start_sweep()
        except Exception as e:
            print("Error starting sweep during stop():", e)
        self.wait()

    def stop_sweep(self):
        """Stop the sweep and hold the output at the current center voltage."""
        try:
            self.client.parameters.sweep_amplitude.value = 0
            self.client.connection.root.write_registers()
        except Exception as e:
            print(f"! Linien stop sweep failed: {e}")

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
            print(f"! Linien mod amplitude update failed: {e}")

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
            print(f"! Linien mod freq update failed: {e}")

    def set_modulation_phase(self, phase_deg: float):
        """Set the demodulation phase in degrees."""
        try:
            self.client.parameters.demodulation_phase_a.value = int(phase_deg)
            self.client.connection.root.write_registers()
        except Exception as e:
            print(f"! Linien mod phase update failed: {e}")

    def set_sweep_speed(self, speed: int):
        """Set the sweep speed (0–15, see Linien sweep speed table)."""
        try:
            self.client.parameters.sweep_speed.value = int(speed)
            self.client.connection.root.write_registers()
        except Exception as e:
            print(f"! Linien sweep speed update failed: {e}")

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
            print(f"! Linien PID read failed: {e}")
            return None

    def set_pid(self, p_value: int, i_value: int, d_value: int):
        """Write the lock PID gains (each 0–8191)."""
        if not hasattr(self, "client"):
            print("! Linien PID update failed: not connected")
            return
        try:
            params = self.client.parameters
            params.p.value = int(p_value)
            params.i.value = int(i_value)
            params.d.value = int(d_value)
            self.client.connection.root.write_registers()
        except Exception as e:
            print(f"! Linien PID update failed: {e}")

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
            print(f"! Linien filter read failed: {e}")
            return None

    def set_filters(self, settings: dict):
        """Write the channel-A error-signal filters (see get_filters)."""
        if not hasattr(self, "client"):
            print("! Linien filter update failed: not connected")
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
            print(f"! Linien filter update failed: {e}")

    def request_sweep_change(self, center: float, amplitude: float):
        try:
            self.current_center = center
            self.current_amplitude = amplitude
            self.client.parameters.sweep_center.value = center
            self.client.parameters.sweep_amplitude.value = amplitude
            self.client.connection.root.write_registers()
        except Exception as e:
            print("! Linien sweep update failed:", e)

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

    def lock_laser(self, selected_positions):
        try:
            self.client.connection.root.start_sweep()
        except Exception:
            pass
        sleep(0.4)

        to_plot = pickle.loads(self.client.parameters.to_plot.value)
        if "error_signal_1" not in to_plot:
            print("No spectrum available to lock on.")
            return

        raw_err = np.asarray(to_plot["error_signal_1"], dtype=np.int16)
        N = raw_err.size
        if N < 4:
            print("Spectrum too small.")
            return

        cen = float(self.client.parameters.sweep_center.value)
        amp = float(self.client.parameters.sweep_amplitude.value)
        voltages = np.linspace(cen - amp, cen + amp, N)

        x0_pos, x1_pos = selected_positions
        i0 = int(np.argmin(np.abs(voltages - x0_pos)))
        i1 = int(np.argmin(np.abs(voltages - x1_pos)))
        if i0 > i1:
            i0, i1 = i1, i0

        if i0 == i1 or (i1 - i0) < 2:
            i0 = max(0, i0 - 1)
            i1 = min(N - 1, i1 + 1)
        if not (0 <= i0 < i1 < N):
            print(f"Invalid index window: ({i0}, {i1}) for N={N}")
            return

        payload = pickle.dumps(np.array(raw_err, dtype=np.int16))

        try:
            p = self.client.parameters
            p.autolock_mode.value = AutolockMode.SIMPLE
            if hasattr(p, "autolock_determine_offset"):
                p.autolock_determine_offset.value = False
            self.client.connection.root.write_registers()
        except Exception as e:
            print("Autolock parameter set warning:", e)

        print(f"Calling start_autolock(i0={i0}, i1={i1}, N={N})")
        try:
            self.client.connection.root.start_autolock(i0, i1, payload)
        except Exception as e:
            print(f"start_autolock raised: {e}")
            return

        if self._wait_for_lock(True, timeout_s=30):
            print("Lock engaged.")
            self._emit_lock_state_best_effort(force_value=True)
        else:
            print("! Lock did not engage in time.")
            self._emit_lock_state_best_effort()

    def unlock_laser(self):
        print("Attempting to unlock laser...")
        try:
            self.client.connection.root.start_sweep()
            if self._wait_for_lock(False, timeout_s=10):
                print("Laser unlocked (sweep started).")
                self._emit_lock_state_best_effort(force_value=False)
            else:
                print("! Unlock not confirmed, but sweep requested.")
                self._emit_lock_state_best_effort()
        except Exception as e:
            print(f"Failed to unlock laser: {e}")
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
