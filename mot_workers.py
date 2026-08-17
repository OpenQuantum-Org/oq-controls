"""
Worker threads for rubidium MOT control hardware.

One QThread per device: the GUI thread calls small set_* methods, and the
worker owns the blocking hardware I/O.

Hardware handled here:
- Riden DPS5005 (MOT coils) and DPS5020 (dispenser) over Modbus RTU
- DAC Click (MCP4921) on the Red Pitaya Click Shield, driven through the
  patched linien-server ``dac_click_voltage`` parameter (repump VCO V_tune)
- One Linien GPIO output bit (repump RF gate, DIO1_N by default)
"""

import time

from pyqtgraph.Qt import QtCore

from mot_logging import NULL_LOG, capture_library_logging


# ---------------------------------------------------------------------------
# USB serial discovery helpers
# ---------------------------------------------------------------------------


def _is_likely_usb_serial(port):
    haystack = " ".join(
        str(x or "")
        for x in (
            port.device,
            port.description,
            port.manufacturer,
            port.product,
            port.interface,
        )
    ).lower()
    if "bluetooth" in haystack or "debug-console" in haystack:
        return False
    return any(
        token in haystack
        for token in (
            "usb",
            "serial",
            "wch",
            "ch340",
            "cp210",
            "ftdi",
            "ttyusb",
            "ttyacm",
            "usbmodem",
            "usbserial",
        )
    )


def _list_candidate_ports():
    try:
        from serial.tools import list_ports
    except Exception:
        return []
    return [p.device for p in list_ports.comports() if _is_likely_usb_serial(p)]


class SerialPresenceWorker(QtCore.QThread):
    """Watch for serial ports appearing and disappearing.

    The Koheron worker has no poll loop -- it reads two setpoints at
    connect and then only writes, from GUI slots, which is the accepted
    exception to invariant 1. That leaves nobody to notice the laser being
    unplugged: its row read OK for the rest of the session.

    Enumerating ports is not device I/O. It does not open anything, does
    not touch the handles the other workers hold, and costs nothing on the
    wire, so it can watch every serial device at once -- including the one
    that is never polled.

    Signals:
        ports_present(object): set of device names currently enumerated.
    """

    ports_present = QtCore.pyqtSignal(object)

    POLL_MS = 3000

    def __init__(self, poll_ms=None):
        super().__init__()
        self.poll_ms = int(poll_ms or self.POLL_MS)
        self.running = False

    def run(self):
        self.running = True
        last = None
        while self.running:
            try:
                from serial.tools import list_ports

                present = {p.device for p in list_ports.comports()}
            except Exception:
                present = None  # enumeration itself failed: claim nothing
            if present is not None and present != last:
                last = present
                self.ports_present.emit(present)
            self.msleep(self.poll_ms)

    def stop(self):
        self.running = False
        self.wait()


def drain_parameter_queue(client):
    """Drain this client's parameter-change queue on the server.

    Every client that connects gets a listener registered for every
    cacheable parameter (linien_server/parameters.py:617), and each change
    is appended to a per-client list. ``get_changed_parameters_queue`` --
    which this call issues -- is the only thing that empties it. Nothing
    in this app called it, so those lists grew for the lifetime of every
    connection, on a 512 MB Red Pitaya, across four connections.

    It is also what refreshes the local cache: with ``use_parameter_cache``
    on, ``parameters.x.value`` returns a value frozen at connect time
    until this runs. That made the server's ``ping`` heartbeat look dead
    on a perfectly healthy server.

    The call is asynchronous inside linien (it issues, then collects on
    the next call), so this is cheap enough to run every poll.
    """
    try:
        client.parameters.check_for_changed_parameters()
    except Exception:
        pass  # a failed drain is a connection problem, reported elsewhere


# ---------------------------------------------------------------------------
# Riden DPS serial / Modbus RTU support (DPS5005, DPS5020)
# ---------------------------------------------------------------------------


class RidenDPSClient:
    """Small Modbus RTU client for Riden DPS-series supplies.

    Register map (shared across the DPS series):
        0: voltage setpoint, 0.01 V
        1: current setpoint, model-dependent LSB
        2: output voltage, 0.01 V
        3: output current, model-dependent LSB
        4: output power, model-dependent LSB
        5: input voltage, 0.01 V
        7: protection status
        8: CV/CC status
        9: output enable

    Current/power resolution differs per model (``current_scale`` /
    ``power_scale`` are LSBs per unit): DPS5005 is 0.001 A and 0.01 W,
    DPS5020 is 0.01 A and 0.1 W.
    """

    REG_V_SET = 0
    REG_I_SET = 1
    REG_V_OUT = 2
    REG_I_OUT = 3
    REG_POWER = 4
    REG_V_IN = 5
    REG_PROTECTION = 7
    REG_CV_CC = 8
    REG_OUTPUT = 9
    REG_MODEL = 11  # reads e.g. 5005 / 5020; used for port identification

    def __init__(
        self,
        port,
        address=1,
        baudrate=9600,
        timeout=0.5,
        current_scale=1000,
        power_scale=100,
    ):
        self.port = port
        self.address = int(address)
        self.baudrate = int(baudrate)
        self.timeout = timeout
        self.current_scale = int(current_scale)
        self.power_scale = int(power_scale)
        self._ser = None

    def open(self):
        import serial

        self._ser = serial.Serial(
            self.port,
            baudrate=self.baudrate,
            bytesize=8,
            parity=serial.PARITY_NONE,
            stopbits=1,
            timeout=self.timeout,
            write_timeout=self.timeout,
        )

    def close(self):
        if self._ser and self._ser.is_open:
            self._ser.close()

    @staticmethod
    def _crc16(data: bytes) -> int:
        crc = 0xFFFF
        for byte in data:
            crc ^= byte
            for _ in range(8):
                if crc & 1:
                    crc = (crc >> 1) ^ 0xA001
                else:
                    crc >>= 1
        return crc & 0xFFFF

    def _request(self, function: int, payload: bytes, expected_len: int) -> bytes:
        if self._ser is None or not self._ser.is_open:
            raise RuntimeError("serial port is not open")

        frame = bytes([self.address, function]) + payload
        crc = self._crc16(frame)
        packet = frame + bytes([crc & 0xFF, (crc >> 8) & 0xFF])

        self._ser.reset_input_buffer()
        self._ser.write(packet)
        response = self._ser.read(expected_len)
        if len(response) != expected_len:
            raise TimeoutError(
                f"short DPS response: got {len(response)}, expected {expected_len}"
            )

        body, crc_bytes = response[:-2], response[-2:]
        got_crc = crc_bytes[0] | (crc_bytes[1] << 8)
        if self._crc16(body) != got_crc:
            raise RuntimeError("DPS CRC mismatch")
        if body[0] != self.address:
            raise RuntimeError(f"DPS address mismatch: {body[0]}")
        if body[1] == (function | 0x80):
            raise RuntimeError(f"DPS Modbus exception {body[2]}")
        if body[1] != function:
            raise RuntimeError(f"DPS function mismatch: {body[1]}")
        return body

    def read_holding_registers(self, start: int, count: int):
        import struct

        payload = struct.pack(">HH", int(start), int(count))
        body = self._request(0x03, payload, expected_len=5 + 2 * count)
        byte_count = body[2]
        if byte_count != 2 * count:
            raise RuntimeError(f"unexpected DPS byte count: {byte_count}")
        return list(struct.unpack(">" + "H" * count, body[3:3 + byte_count]))

    def write_register(self, register: int, value: int):
        import struct

        payload = struct.pack(">HH", int(register), int(value))
        self._request(0x06, payload, expected_len=8)


# Per-model register scaling and hard limits used for client-side clamping.
DPS_MODELS = {
    "DPS5005": {
        "current_scale": 1000,  # 0.001 A per LSB
        "power_scale": 100,     # 0.01 W per LSB
        "max_voltage": 50.0,
        "max_current": 5.0,
    },
    "DPS5020": {
        "current_scale": 100,   # 0.01 A per LSB
        "power_scale": 10,      # 0.1 W per LSB
        "max_voltage": 50.0,
        "max_current": 20.0,
    },
}


class LinkHealth:
    """Turns a stream of per-poll failures into two edges: down, and back.

    A poll loop that fails keeps failing -- an unplugged supply raises
    every 500 ms -- but the operator needs to be told once, and told again
    when it recovers. ``connection_status`` was only ever emitted at
    connect, so a device that went away mid-session still read as OK.

    One failure is not a disconnection: a single bad frame on a 9600-baud
    line is ordinary, so the down edge waits for a few in a row.
    """

    FAILURES_BEFORE_DOWN = 3

    def __init__(self, threshold=None):
        self.threshold = int(threshold or self.FAILURES_BEFORE_DOWN)
        self.failures = 0
        self.down = False

    def failed(self):
        """True exactly once, on the poll that makes this a disconnection."""
        self.failures += 1
        if self.down or self.failures < self.threshold:
            return False
        self.down = True
        return True

    def ok(self):
        """True exactly once, on the poll that brings the link back."""
        self.failures = 0
        if not self.down:
            return False
        self.down = False
        return True


class RidenDPSWorker(QtCore.QThread):
    """Control a Riden DPS supply (MOT coils or dispenser)."""

    readback = QtCore.pyqtSignal(object)
    connection_status = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)

    def __init__(
        self,
        port=None,
        address=1,
        model="DPS5005",
        baudrate=9600,
        disable_on_stop=True,
        poll_ms=500,
        event_log=None,
        log_name="dps",
    ):
        super().__init__()
        spec = DPS_MODELS[model]
        # Transactions are logged here rather than in the GUI: this is
        # where the write actually happens and where its duration and
        # outcome are known.
        self.event_log = event_log or NULL_LOG
        self.log_name = log_name
        self.port = port
        self.address = int(address)
        self.model = model
        self.baudrate = int(baudrate)
        self.current_scale = spec["current_scale"]
        self.power_scale = spec["power_scale"]
        self.max_voltage = spec["max_voltage"]
        self.max_current = spec["max_current"]
        self.disable_on_stop = bool(disable_on_stop)
        self.poll_ms = int(poll_ms)
        self.running = False

        self._client = None
        self._mutex = QtCore.QMutex()
        self._pending = []
        self._link = LinkHealth()

    def run(self):
        if not self.port:
            self.connection_status.emit("no_port")
            return

        self.running = True
        try:
            self._client = RidenDPSClient(
                self.port,
                address=self.address,
                baudrate=self.baudrate,
                current_scale=self.current_scale,
                power_scale=self.power_scale,
            )
            self._client.open()
            self.connection_status.emit(f"connected:{self.port}@{self.address}")
        except Exception as e:
            self.connection_status.emit(f"error:{e}")
            self.running = False
            return

        # Drop anything queued while we were disconnected so a stale command
        # (e.g. an old fire request) cannot execute on reconnect.
        self._pop_pending()

        while self.running:
            try:
                self._process_pending()
                state = self._read_state()
                self._service(state)
                self.readback.emit(state)
            except Exception as e:
                if self._link.failed():
                    # Say it once, in the place that means "this device is
                    # not there": the row and the DEVICES count, not another
                    # line in the log.
                    self.connection_status.emit(f"error:{e}")
                self.fault.emit(str(e))
            else:
                if self._link.ok():
                    self.connection_status.emit(
                        f"connected:{self.port}@{self.address}"
                    )
            self.msleep(self.poll_ms)

        if self.disable_on_stop and self._client is not None:
            # The safety-critical write of the whole class: this is what
            # makes CLAUDE.md invariant 7 true, so it is logged even
            # though nothing is left alive to react to a failure.
            try:
                with self.event_log.command(
                    f"{self.log_name}.output", value=False, reason="worker_stop"
                ):
                    self._client.write_register(RidenDPSClient.REG_OUTPUT, 0)
            except Exception:
                pass
        if self._client is not None:
            self._client.close()

    def stop(self):
        self.running = False
        self.wait()

    def set_voltage(self, volts: float):
        volts = max(0.0, min(float(volts), self.max_voltage))
        self._queue(("voltage", volts))

    def set_current(self, amps: float):
        amps = max(0.0, min(float(amps), self.max_current))
        self._queue(("current", amps))

    def set_output(self, enabled: bool):
        self._queue(("output", bool(enabled)))

    def set_setpoints(self, volts: float, amps: float):
        self.set_voltage(volts)
        self.set_current(amps)

    # Setpoint-style commands are coalesced to the latest value so a slider
    # drag results in one write, not a replayed burst. fire/abort keep
    # their ordering.
    COALESCE_KINDS = ("voltage", "current", "output", "params")

    def _queue(self, item):
        with QtCore.QMutexLocker(self._mutex):
            if item[0] in self.COALESCE_KINDS:
                self._pending = [p for p in self._pending if p[0] != item[0]]
            self._pending.append(item)

    def _pop_pending(self):
        with QtCore.QMutexLocker(self._mutex):
            items = self._pending
            self._pending = []
        return items

    def _process_pending(self):
        for kind, value in self._pop_pending():
            self._apply(kind, value)

    def _apply(self, kind, value):
        with self.event_log.command(
            f"{self.log_name}.{kind}", value=value, port=self.port
        ):
            if kind == "voltage":
                self._client.write_register(
                    RidenDPSClient.REG_V_SET, int(round(value * 100))
                )
            elif kind == "current":
                self._client.write_register(
                    RidenDPSClient.REG_I_SET,
                    int(round(value * self.current_scale)),
                )
            elif kind == "output":
                self._client.write_register(
                    RidenDPSClient.REG_OUTPUT, 1 if value else 0
                )

    def _service(self, state):
        """Per-poll hook for subclasses (runs after the state read)."""

    def _read_state(self):
        regs = self._client.read_holding_registers(0, 10)
        return {
            "v_set": regs[RidenDPSClient.REG_V_SET] / 100.0,
            "i_set": regs[RidenDPSClient.REG_I_SET] / self.current_scale,
            "v_out": regs[RidenDPSClient.REG_V_OUT] / 100.0,
            "i_out": regs[RidenDPSClient.REG_I_OUT] / self.current_scale,
            "power": regs[RidenDPSClient.REG_POWER] / self.power_scale,
            "v_in": regs[RidenDPSClient.REG_V_IN] / 100.0,
            "protection": regs[RidenDPSClient.REG_PROTECTION],
            "cv_cc": regs[RidenDPSClient.REG_CV_CC],
            "output_enabled": bool(regs[RidenDPSClient.REG_OUTPUT]),
        }


class DispenserWorker(RidenDPSWorker):
    """DPS5020 dispenser supply with a fixed operating point, a timed fire,
    and a lockout cooldown.

    fire() programs the fixed setpoints, then enables the output for
    FIRE_SECONDS, after which the worker forces the output off and refuses
    to re-enable it until COOLDOWN_SECONDS have elapsed. Turning the output
    off is always allowed. If the output is enabled behind our back (front
    panel), the fixed setpoints and the same fire time limit are applied
    while the app is connected.

    The dispense operating point is not adjustable through the normal
    supply API (set_voltage/set_current are inert), so a stray call cannot
    move it. The only way in is set_fire_parameters(), which the UI drives
    from the dispenser card's voltage/current sliders and from the
    Override dialog. The dispenser is a near-short (<~0.1 V at 7.5 A), so
    the supply runs current-limited (CC); the voltage setpoint is only
    headroom and is never reached.

    The auto-off runs in this worker thread, so it only holds while the
    app is running and the serial link is alive.
    """

    # {"phase": "idle"|"firing"|"cooldown", "remaining_s": float}
    fire_state = QtCore.pyqtSignal(object)
    # Wall-clock epoch when the cooldown will end; the GUI persists this
    # so the lockout survives an app restart.
    cooldown_started = QtCore.pyqtSignal(float)
    # Emitted once per completed/aborted fire:
    # {"elapsed_s", "planned_s", "current_a", "aborted"}
    fire_summary = QtCore.pyqtSignal(object)

    FIRE_SECONDS = 30.0
    COOLDOWN_SECONDS = 15 * 60.0
    DISPENSE_CURRENT_A = 7.5
    DISPENSE_VOLTAGE_V = 5.0
    MIN_FIRE_SECONDS = 1.0

    def __init__(
        self,
        *args,
        cooldown_until=0.0,
        fire_seconds=None,
        dispense_current_a=None,
        dispense_voltage_v=None,
        **kwargs,
    ):
        kwargs.setdefault("model", "DPS5020")
        super().__init__(*args, **kwargs)
        self.fire_seconds = self._clamp_seconds(fire_seconds or self.FIRE_SECONDS)
        self.dispense_current_a = self._clamp_current(
            dispense_current_a or self.DISPENSE_CURRENT_A
        )
        self.dispense_voltage_v = self._clamp_voltage(
            dispense_voltage_v or self.DISPENSE_VOLTAGE_V
        )
        remaining = max(0.0, float(cooldown_until or 0.0) - time.time())
        self._phase = "cooldown" if remaining > 0 else "idle"
        self._fire_end = 0.0
        self._fire_started = 0.0
        self._cooldown_end = time.monotonic() + remaining

    def _clamp_current(self, amps):
        return max(0.01, min(float(amps), self.max_current))

    def _clamp_voltage(self, volts):
        return max(0.0, min(float(volts), self.max_voltage))

    def _clamp_seconds(self, seconds):
        return max(self.MIN_FIRE_SECONDS, float(seconds))

    # --- GUI-thread interface ---

    def fire(self):
        self._queue(("fire", None))

    def abort_fire(self):
        self._queue(("abort", None))

    def set_output(self, enabled: bool):
        # Route through fire/abort so the cooldown is enforced no matter
        # which code path asks for the output.
        if enabled:
            self.fire()
        else:
            self.abort_fire()

    def set_voltage(self, volts: float):
        """Ignored: the dispenser operating point is fixed."""

    def set_current(self, amps: float):
        """Ignored: the dispenser operating point is fixed."""

    def set_fire_parameters(
        self, current_a: float, seconds: float, voltage_v: float = None
    ):
        """Override the dispense current, fire duration and voltage limit.

        Takes effect from the next fire (mid-fire, the new setpoints are
        also enforced on the next poll; the running fire keeps its
        original end time). ``voltage_v`` is headroom only -- the
        dispenser is a near-short, so the supply runs current-limited and
        this setpoint is never reached.
        """
        self._queue(
            ("params", (float(current_a), float(seconds), voltage_v))
        )

    # --- worker-thread internals ---

    def _apply(self, kind, value):
        if kind == "fire":
            self._start_fire()
        elif kind == "abort":
            self._end_fire(aborted=True)
        elif kind == "params":
            current_a, seconds, voltage_v = value
            self.dispense_current_a = self._clamp_current(current_a)
            self.fire_seconds = self._clamp_seconds(seconds)
            if voltage_v is not None:
                self.dispense_voltage_v = self._clamp_voltage(voltage_v)
        elif kind == "output":
            # Should not happen (set_output is rerouted), but keep the
            # cooldown authoritative for any direct queued output command.
            if value:
                self._start_fire()
            else:
                self._end_fire(aborted=True)
        else:
            super()._apply(kind, value)

    def _start_fire(self):
        now = time.monotonic()
        if self._phase == "cooldown":
            if now < self._cooldown_end:
                self._emit_fire_state()
                return
            self._phase = "idle"
        if self._phase == "firing":
            return
        # Program the operating point before every enable so a front-panel
        # change can never carry into a fire.
        super()._apply("voltage", self.dispense_voltage_v)
        super()._apply("current", self.dispense_current_a)
        super()._apply("output", True)
        self._phase = "firing"
        self._fire_started = now
        self._fire_end = now + self.fire_seconds
        self._emit_fire_state()

    def _end_fire(self, aborted=False):
        super()._apply("output", False)
        if self._phase == "firing":
            self._phase = "cooldown"
            self._cooldown_end = time.monotonic() + self.COOLDOWN_SECONDS
            self.fire_summary.emit(
                {
                    "elapsed_s": max(0.0, time.monotonic() - self._fire_started),
                    "planned_s": self.fire_seconds,
                    "current_a": self.dispense_current_a,
                    "aborted": bool(aborted),
                }
            )
            self.cooldown_started.emit(time.time() + self.COOLDOWN_SECONDS)
        self._emit_fire_state()

    def _service(self, state):
        now = time.monotonic()
        if self._phase == "firing":
            if now >= self._fire_end:
                self._end_fire(aborted=False)
            else:
                # Undo any mid-fire front-panel change of the setpoints.
                self._enforce_setpoints(state)
        elif self._phase == "cooldown":
            if state.get("output_enabled"):
                # Never allow the output during cooldown, even if it was
                # switched on at the front panel.
                super()._apply("output", False)
            if now >= self._cooldown_end:
                self._phase = "idle"
        elif self._phase == "idle" and state.get("output_enabled"):
            # Output enabled outside our fire() path: enforce the
            # operating point and apply the same time limit.
            self._enforce_setpoints(state)
            self._phase = "firing"
            self._fire_started = now
            self._fire_end = now + self.fire_seconds
        self._emit_fire_state()

    def _enforce_setpoints(self, state):
        if abs(state.get("v_set", 0.0) - self.dispense_voltage_v) > 0.005:
            super()._apply("voltage", self.dispense_voltage_v)
        if abs(state.get("i_set", 0.0) - self.dispense_current_a) > 0.005:
            super()._apply("current", self.dispense_current_a)

    def _emit_fire_state(self):
        now = time.monotonic()
        if self._phase == "firing":
            remaining = max(0.0, self._fire_end - now)
        elif self._phase == "cooldown":
            remaining = max(0.0, self._cooldown_end - now)
        else:
            remaining = 0.0
        self.fire_state.emit({"phase": self._phase, "remaining_s": remaining})


# ---------------------------------------------------------------------------
# USB device identification (read-only probes)
# ---------------------------------------------------------------------------


def probe_dps_model(port, address=1, baudrate=9600, timeout=0.4):
    """Return the Riden DPS model number (e.g. 5005, 5020) or None.

    Read-only: reads the model register. A Koheron on this port sees a few
    baud-mismatched binary bytes that match no ASCII command.
    """
    client = RidenDPSClient(port, address=address, baudrate=baudrate, timeout=timeout)
    try:
        client.open()
        return client.read_holding_registers(RidenDPSClient.REG_MODEL, 1)[0]
    except Exception:
        return None
    finally:
        client.close()


def probe_koheron_port(port, baudrate=115200, timeout=0.4):
    """Return True if a Koheron CTL200 answers an `ilaser` query here.

    Read-only: `ilaser` without an argument only reads the current
    setpoint. A DPS on this port ignores the ASCII bytes (no valid Modbus
    frame), so the probe is harmless either way.
    """
    try:
        import serial

        with serial.Serial(
            port, baudrate=baudrate, timeout=timeout, write_timeout=timeout
        ) as ser:
            ser.reset_input_buffer()
            ser.write(b"ilaser\r\n")
            for _ in range(4):
                raw = ser.readline()
                if not raw:
                    return False  # silence: not a Koheron
                text = raw.decode("ascii", errors="ignore").strip().lstrip(">").strip()
                if not text or text.lower().startswith("ilaser"):
                    continue  # command echo / prompt
                try:
                    float(text)
                    return True
                except ValueError:
                    continue
    except Exception:
        pass
    return False


class SerialProbeWorker(QtCore.QThread):
    """Work out which USB serial port hosts each device.

    Saved port assignments are re-verified first (cables may have been
    rearranged since the last run); any device that fails verification is
    then searched for across the remaining candidate ports. Identification
    uses read-only probes only — nothing on any unit is changed.

    Emits result:
        {"ok": bool,
         "assignments": {"laser"|"mot_coil"|"dispenser": port or ""},
         "status": {...: "verified" | "missing"}}
    """

    result = QtCore.pyqtSignal(object)

    KEYS = ("laser", "mot_coil", "dispenser")
    EXPECTED = {
        "laser": ("koheron", None),
        "mot_coil": ("dps", 5005),
        "dispenser": ("dps", 5020),
    }

    def __init__(self, saved=None, coil_address=1, dispenser_address=1):
        super().__init__()
        self.saved = dict(saved or {})
        self.addresses = {
            "mot_coil": int(coil_address),
            "dispenser": int(dispenser_address),
        }

    def run(self):
        ports = _list_candidate_ports()
        assignments = {key: "" for key in self.KEYS}
        used = set()

        # Pass 1: re-verify the remembered port for each device.
        for key in self.KEYS:
            port = (self.saved.get(key) or "").strip()
            if port and port in ports and port not in used and self._verify(key, port):
                assignments[key] = port
                used.add(port)

        # Pass 2: scan the remaining ports for the still-missing devices.
        for port in ports:
            if all(assignments.values()):
                break
            if port in used:
                continue
            identity = self._identify(port)
            if identity is None:
                continue
            kind, model = identity
            for key in self.KEYS:
                want_kind, want_model = self.EXPECTED[key]
                if (
                    not assignments[key]
                    and kind == want_kind
                    and (want_model is None or model == want_model)
                ):
                    assignments[key] = port
                    used.add(port)
                    break

        self.result.emit(
            {
                "ok": all(assignments.values()),
                "assignments": assignments,
                "status": {
                    key: "verified" if assignments[key] else "missing"
                    for key in self.KEYS
                },
            }
        )

    def _verify(self, key, port):
        kind, model = self.EXPECTED[key]
        if kind == "koheron":
            return probe_koheron_port(port)
        return probe_dps_model(port, address=self.addresses[key]) == model

    def _identify(self, port):
        for address in dict.fromkeys(self.addresses.values()):
            model = probe_dps_model(port, address=address)
            if model is not None:
                return ("dps", model)
        if probe_koheron_port(port):
            return ("koheron", None)
        return None


# ---------------------------------------------------------------------------
# DAC Click (MCP4921) repump VCO tune voltage, via the patched linien-server
# ---------------------------------------------------------------------------


class DacClickWorker(QtCore.QThread):
    """Set the repump VCO tune voltage (V_tune) via the DAC Click.

    Requires the patched linien-server on the Red Pitaya that exposes the
    ``dac_click_voltage`` parameter (MCP4921 in Click Shield socket 1,
    SPI on /dev/spidev1.0 with DIO3_N as chip select, 0-5 V full scale set
    by the hardware Vref). Each write costs ~0.3 s on the server because
    of its deliberate CS retry loop, so only the most recent requested
    voltage is pushed.

    The linien parameter cache MUST stay off: cached parameter writes are
    silently swallowed and never reach the DAC.
    """

    voltage_readback = QtCore.pyqtSignal(float)
    connection_status = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)

    PARAMETER = "dac_click_voltage"

    def __init__(
        self,
        host=None,
        min_voltage=0.0,
        max_voltage=5.0,
        username="root",
        password="root",
        event_log=None,
    ):
        super().__init__()
        self.event_log = event_log or NULL_LOG
        self.host = host
        self.min_voltage = float(min_voltage)
        self.max_voltage = float(max_voltage)
        self.username = username
        self.password = password
        self.running = False

        self._client = None
        self._parameter = None
        self._mutex = QtCore.QMutex()
        self._pending_voltage = None
        self._link = LinkHealth()

    def run(self):
        if not self.host:
            self.connection_status.emit("no_host")
            return

        try:
            from linien_client.connection import LinienClient
            from linien_client.device import Device

            # This import may be the one that loaded linien_client, which
            # attaches its own console handler as it goes.
            capture_library_logging()

            dev = Device(
                host=self.host, username=self.username, password=self.password
            )
            self._client = LinienClient(dev)
            self._client.connect(autostart_server=True, use_parameter_cache=False)
            self._parameter = getattr(
                self._client.parameters, self.PARAMETER, None
            )
            if self._parameter is None:
                self.connection_status.emit(
                    "error:linien-server has no dac_click_voltage parameter "
                    "(server not patched for the DAC Click)"
                )
                return
            self.connection_status.emit(f"connected:{self.host}")
            self.voltage_readback.emit(float(self._parameter.value))
        except Exception as e:
            self.connection_status.emit(f"error:{e}")
            return

        self.running = True
        ticks = 0
        while self.running:
            try:
                drain_parameter_queue(self._client)
                pending = self._take_pending_voltage()
                if pending is not None:
                    with self.event_log.command(
                        "repump.vtune", volts=pending
                    ) as op:
                        self._parameter.value = pending
                        # Blocks ~0.3 s while the server runs its SPI
                        # retry loop.
                        self._client.connection.root.write_registers()
                        op.result(readback_v=float(self._parameter.value))
                    self.voltage_readback.emit(float(self._parameter.value))
                elif ticks % 20 == 0:
                    self.voltage_readback.emit(float(self._parameter.value))
            except Exception as e:
                if self._link.failed():
                    self.connection_status.emit(f"error:{e}")
                self.fault.emit(str(e))
            else:
                if self._link.ok():
                    self.connection_status.emit(f"connected:{self.host}")
            ticks += 1
            self.msleep(100)

        # The linien server holds the DAC at its last value after we
        # disconnect, so there is nothing to reset here.

    def stop(self):
        self.running = False
        self.wait()

    def set_voltage(self, volts: float):
        volts = max(self.min_voltage, min(float(volts), self.max_voltage))
        with QtCore.QMutexLocker(self._mutex):
            self._pending_voltage = volts

    def _take_pending_voltage(self):
        with QtCore.QMutexLocker(self._mutex):
            value = self._pending_voltage
            self._pending_voltage = None
        return value


# ---------------------------------------------------------------------------
# Lock-noise PSD measurement / PID optimization, via linien
# ---------------------------------------------------------------------------


class PsdWorker(QtCore.QThread):
    """Drive linien's lock-noise PSD measurement and PID optimization.

    Uses its own RPyC connection (parameter cache off, as required for
    writes to reach the server). Results arrive as pickled payload dicts
    in the ``psd_data_partial`` / ``psd_data_complete`` parameters:

        {"uuid", "time", "p", "i", "d", "signals",
         "psds": {decimation: (freq_Hz, asd_counts_per_sqrtHz)},
         "fitness", "complete"}

    The acquisition steps through decimations (each stage ~16x longer
    than the last); ``psd_acquisition_max_decimation`` bounds how low in
    frequency (and how long) it runs. ``stop_psd()`` aborts a run in
    flight, the way linien's Stop button does.
    """

    connection_status = QtCore.pyqtSignal(str)
    # {"acquiring": bool, "optimizing": bool, "locked": bool, p, i, d}
    status = QtCore.pyqtSignal(object)
    psd_data = QtCore.pyqtSignal(object)  # unpickled payload dict
    fault = QtCore.pyqtSignal(str)

    POLL_MS = 2000

    def __init__(self, host=None, username="root", password="root"):
        super().__init__()
        self.host = host
        self.username = username
        self.password = password
        self.running = False

        self._client = None
        self._mutex = QtCore.QMutex()
        self._pending = []
        self._last_payload_key = None
        self._was_busy = False
        self._fetch_complete = True  # pick up an existing result on connect

    def run(self):
        if not self.host:
            self.connection_status.emit("no_host")
            return

        try:
            from linien_client.connection import LinienClient
            from linien_client.device import Device

            capture_library_logging()  # see DacClickWorker.run

            dev = Device(
                host=self.host, username=self.username, password=self.password
            )
            self._client = LinienClient(dev)
            # Cache off: cached parameter writes never reach the server.
            self._client.connect(autostart_server=True, use_parameter_cache=False)
            self.connection_status.emit(f"connected:{self.host}")
        except Exception as e:
            self.connection_status.emit(f"error:{e}")
            return

        self.running = True
        while self.running:
            try:
                drain_parameter_queue(self._client)
                self._process_pending()
                self._poll()
            except Exception as e:
                self.fault.emit(str(e))
            self.msleep(self.POLL_MS)

        try:
            self._client.disconnect()
        except Exception:
            pass

    def stop(self):
        self.running = False
        self.wait()

    # --- GUI-thread interface ---

    def start_psd(self):
        self._queue(("start_psd", None))

    def start_pid_optimization(self):
        self._queue(("start_pid_opt", None))

    def stop_psd(self):
        """Abort a running acquisition, as linien's Stop button does."""
        self._queue(("stop_psd", None))

    def set_algorithm(self, value: int):
        self._queue(("algorithm", int(value)))

    def set_max_decimation(self, value: int):
        self._queue(("max_decimation", int(value)))

    def _queue(self, item):
        with QtCore.QMutexLocker(self._mutex):
            self._pending = [p for p in self._pending if p[0] != item[0]]
            self._pending.append(item)

    def _pop_pending(self):
        with QtCore.QMutexLocker(self._mutex):
            items = self._pending
            self._pending = []
        return items

    # --- worker-thread internals ---

    def _process_pending(self):
        params = self._client.parameters
        for kind, value in self._pop_pending():
            if kind == "start_psd":
                self._client.connection.root.start_psd_acquisition()
            elif kind == "start_pid_opt":
                self._client.connection.root.start_pid_optimization()
            elif kind == "stop_psd":
                # linien's PSDWindow.stop_psd, verbatim.
                if params.task.value is not None:
                    params.task.value.stop()
                    params.task.value = None
            elif kind == "algorithm":
                params.psd_algorithm.value = value
            elif kind == "max_decimation":
                params.psd_acquisition_max_decimation.value = value

    def _poll(self):
        import pickle

        params = self._client.parameters
        acquiring = bool(params.psd_acquisition_running.value)
        optimizing = bool(params.psd_optimization_running.value)
        busy = acquiring or optimizing
        self.status.emit(
            {
                "acquiring": acquiring,
                "optimizing": optimizing,
                "locked": bool(params.lock.value),
                "p": int(params.p.value),
                "i": int(params.i.value),
                "d": int(params.d.value),
            }
        )

        # Fetch strategy keeps network traffic down: partial data only
        # while a measurement runs, the complete payload once when it
        # finishes (payloads carry raw traces and can be megabytes).
        if self._was_busy and not busy:
            self._fetch_complete = True
        self._was_busy = busy

        raw = None
        if busy:
            raw = params.psd_data_partial.value
        elif self._fetch_complete:
            raw = params.psd_data_complete.value
            self._fetch_complete = False
        if not raw:
            return

        payload = pickle.loads(raw)
        key = (
            payload.get("uuid"),
            payload.get("time"),
            bool(payload.get("complete")),
        )
        if key == self._last_payload_key:
            return
        self._last_payload_key = key
        self.psd_data.emit(payload)


# ---------------------------------------------------------------------------
# Linien GPIO bitfield output (repump RF gate)
# ---------------------------------------------------------------------------


class LinienGpioWorker(QtCore.QThread):
    """Control one Linien GPIO output bit.

    Linien exposes GPIO outputs as two bitfields:
        gpio_p_out: DIO0_P ... DIO7_P
        gpio_n_out: DIO0_N ... DIO7_N

    The repump RF amplifier enable sits on DIO1_N (bank "n", bit 1,
    active-low) — the Click Shield's socket-2 CS pin repurposed as a
    plain digital output. Bit 3 of the "n" bank is the DAC Click chip
    select and must not be assigned here.
    """

    output_state = QtCore.pyqtSignal(bool)
    connection_status = QtCore.pyqtSignal(str)
    fault = QtCore.pyqtSignal(str)

    # DIO3_N is the DAC Click chip select, driven by the patched server.
    DAC_CS_BIT = 3

    def __init__(
        self,
        host=None,
        bank="n",
        bit=1,
        active_high=False,
        username="root",
        password="root",
        disable_on_stop=True,
        event_log=None,
    ):
        super().__init__()
        self.event_log = event_log or NULL_LOG
        self.host = host
        self.bank = str(bank or "n").lower()
        self.bit = int(bit)
        self.active_high = bool(active_high)
        self.username = username
        self.password = password
        self.disable_on_stop = bool(disable_on_stop)
        self.running = False

        self._client = None
        self._parameter = None
        self._mutex = QtCore.QMutex()
        self._pending_state = None
        self._link = LinkHealth()

    def run(self):
        if not self.host:
            self.connection_status.emit("no_host")
            return
        if self.bank not in ("p", "n") or not (0 <= self.bit <= 7):
            self.connection_status.emit("error:GPIO must be bank p/n and bit 0-7")
            return
        if self.bank == "n" and self.bit == self.DAC_CS_BIT:
            self.connection_status.emit(
                "error:DIO3_N is reserved as the DAC Click chip select"
            )
            return

        try:
            from linien_client.connection import LinienClient
            from linien_client.device import Device

            capture_library_logging()  # see DacClickWorker.run

            dev = Device(
                host=self.host, username=self.username, password=self.password
            )
            self._client = LinienClient(dev)
            # Cache off: cached parameter writes never reach the hardware.
            self._client.connect(autostart_server=True, use_parameter_cache=False)
            self._parameter = getattr(
                self._client.parameters, f"gpio_{self.bank}_out"
            )
            self.connection_status.emit(f"connected:{self.host}")
            self._emit_state_from_raw(int(self._parameter.value))
        except Exception as e:
            self.connection_status.emit(f"error:{e}")
            return

        self.running = True
        while self.running:
            try:
                drain_parameter_queue(self._client)
                pending = self._take_pending_state()
                if pending is not None:
                    with self.event_log.command(
                        "repump.gate", enabled=bool(pending)
                    ):
                        self._write_state(pending)
                else:
                    self._emit_state_from_raw(int(self._parameter.value))
            except Exception as e:
                if self._link.failed():
                    self.connection_status.emit(f"error:{e}")
                self.fault.emit(str(e))
            else:
                if self._link.ok():
                    self.connection_status.emit(f"connected:{self.host}")
            self.msleep(250)

        if self.disable_on_stop:
            try:
                self._write_state(False)
            except Exception:
                pass

    def stop(self):
        self.running = False
        self.wait()

    def set_output(self, enabled: bool):
        with QtCore.QMutexLocker(self._mutex):
            self._pending_state = bool(enabled)

    def _take_pending_state(self):
        with QtCore.QMutexLocker(self._mutex):
            value = self._pending_state
            self._pending_state = None
        return value

    def _write_state(self, logical_on: bool):
        mask = 1 << self.bit
        raw = int(self._parameter.value)
        drive_high = bool(logical_on) if self.active_high else not bool(logical_on)
        if drive_high:
            raw |= mask
        else:
            raw &= ~mask
        self._parameter.value = raw
        self._client.connection.root.write_registers()
        self._emit_state_from_raw(raw)

    def _emit_state_from_raw(self, raw: int):
        bit_high = bool(raw & (1 << self.bit))
        logical_on = bit_high if self.active_high else not bit_high
        self.output_state.emit(logical_on)


# ---------------------------------------------------------------------------
# Laser Diode Setpoint Controller (CTL200-0)
# ---------------------------------------------------------------------------
class LaserSetpointWorker(QtCore.QThread):
    """Controls a CTL200-0 laser diode controller for current and temperature.

    Connects via serial, reads initial setpoints, then exposes methods
    for the GUI thread to update current, thermistor, and on/off state.

    Signals:
        initial_values(dict): {"current": int, "thermistor": int} on connect.
        connection_status(str): "connected:<port>", "no_port", or "error:<msg>".
    """
    initial_values = QtCore.pyqtSignal(dict)
    connection_status = QtCore.pyqtSignal(str)

    def __init__(self, port=None, baudrate=115200, event_log=None,
                 disable_on_stop=True):
        super().__init__()
        self.event_log = event_log or NULL_LOG
        # Cleared by the shutdown dialog when the operator chooses to
        # leave the laser running past the app.
        self.disable_on_stop = bool(disable_on_stop)
        self._port = port
        self._baudrate = baudrate
        self._ser = None

    def run(self):
        import serial as _serial

        if not self._port:
            self.connection_status.emit("no_port")
            return
        try:
            self._ser = _serial.Serial(
                self._port, baudrate=self._baudrate, timeout=1.0
            )
            current = float(self._query("ilaser"))
            thermistor = float(self._query("rtact"))
            self.initial_values.emit({
                "current": int(current),
                "thermistor": int(thermistor),
            })
            self.connection_status.emit(f"connected:{self._port}")
        except Exception as e:
            self.connection_status.emit(f"error:{e}")

    def _query(self, cmd: str) -> str:
        self._ser.reset_input_buffer()
        self._ser.write(f"{cmd}\r\n".encode("ascii"))
        self._ser.readline()  # discard echo
        return self._ser.readline().decode("ascii", errors="ignore").strip()

    def _send(self, cmd: str):
        if self._ser and self._ser.is_open:
            self._ser.write(f"{cmd}\r\n".encode("ascii"))

    def set_current(self, current_mA: float):
        """Set laser diode current in mA."""
        with self.event_log.command("laser.current", ma=current_mA):
            self._send(f"ilaser {current_mA}")

    def set_thermistor(self, resistance_ohms: float):
        """Set thermistor setpoint in Ohms."""
        with self.event_log.command("laser.thermistor", ohms=resistance_ohms):
            self._send(f"rtset {resistance_ohms}")

    def laser_on(self, on: bool = True):
        """Enable or disable laser output."""
        with self.event_log.command("laser.enable", on=bool(on)):
            self._send(f"lason {1 if on else 0}")

    def stop(self):
        if self.disable_on_stop:
            try:
                with self.event_log.command(
                    "laser.enable", on=False, reason="worker_stop"
                ):
                    self._send("lason 0")
            except Exception:
                pass
        else:
            self.event_log.log("laser_left_on", reason="operator_choice")
        if self._ser and self._ser.is_open:
            self._ser.close()
        self.wait()



# ---------------------------------------------------------------------------
class LinienServerRestartWorker(QtCore.QThread):
    """Restart linien-server on the Red Pitaya, over SSH.

    A wedged server answers RPyC but its acquisition and heartbeat threads
    are dead, and the only cure is a restart. This does it from the app so
    that does not mean an SSH session.

    Deliberately a one-shot thread rather than a persistent worker: it
    exists because ``start_remote_server`` blocks on SSH for several
    seconds and the GUI thread must not. It owns no client and no port.

    **Only ever calls ``start_remote_server``.** linien-gui's
    ``install_remote_server`` runs ``pip3 uninstall linien-server -y``,
    which would destroy the hand-applied dac_click_voltage patch on this
    device (see CLAUDE.md).

    Signals:
        finished_ok(bool, str): success flag and a message for the log.
    """

    finished_ok = QtCore.pyqtSignal(bool, str)

    def __init__(self, host, username="root", password="root", event_log=None):
        super().__init__()
        self.event_log = event_log or NULL_LOG
        self.host = host
        self.username = username
        self.password = password

    def run(self):
        try:
            from linien_client.deploy import start_remote_server
            from linien_client.device import Device

            capture_library_logging()  # see DacClickWorker.run
        except Exception as e:
            self.finished_ok.emit(False, f"linien_client unavailable: {e}")
            return

        device = Device(
            host=self.host, username=self.username, password=self.password
        )
        try:
            with self.event_log.command(
                "linien.restart_server", host=self.host
            ):
                start_remote_server(device)
        except Exception as e:
            self.finished_ok.emit(False, str(e))
            return
        self.finished_ok.emit(True, f"linien-server restarted on {self.host}")
