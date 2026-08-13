# OpenQuantum Controls

This is the control UI for the rubidium MOT hardware. The repo is
self-contained: the Linien spectroscopy widget/worker, the shared styles,
and the Koheron setpoint worker are vendored here (originally from the
eit-controls project), plus the MOT supplies and repump tune/gate controls.

## Hardware covered

- One Koheron CTL200-0 laser controller for the MOT laser current, thermistor
  setpoint, and laser on/off state
- One Linien/Red Pitaya (STEMlab 125-14) spectroscopy lock; the error signal
  locks this one laser
- Repump VCO tune voltage (V_tune) through the MikroE DAC Click (MCP4921) on
  the Red Pitaya Click Shield, socket 1 — driven via the patched
  linien-server's `dac_click_voltage` parameter (see below). The UI shows the
  resulting VCO output frequency (measured tuning range: 0–5 V spans
  2800–2970 MHz, linear)
- Repump RF gate through a Linien Red Pitaya GPIO bit — fixed at `DIO1_N`
  (bank `n`, bit 1), **active-low** (the RF amp enable on Click Shield
  socket 2, the empirically verified mapping)
- MOT coil DPS5005 voltage/current/output control
- Dispenser DPS5020 with a fixed, timed fire: **7.5 A constant-current**
  (5 V headroom setpoint, never reached) for **30 s**, then the output is
  forced off automatically and a **15 min cooldown** blocks re-firing (the
  lockout persists across app restarts). The operating point is fixed in
  code — no UI controls — to protect the dispenser and the pump
- Optional USB camera pane that can be toggled on below the live spectroscopy
  plot, with a no-camera fallback if no camera is present

## USB auto-detection at startup

On every launch the app probes the USB serial devices before starting any
hardware workers, using read-only identification commands only:

- Koheron CTL200: an `ilaser` query (reads the current setpoint; a numeric
  reply identifies the device)
- DPS supplies: a Modbus read of the model register, which distinguishes the
  DPS5005 (coils) from the DPS5020 (dispenser)

The remembered port for each device is re-verified first — so rearranged USB
cables are detected and corrected automatically — and any device that fails
verification is searched for across the remaining ports. Verified assignments
are saved in `QSettings` and auto-load next time. The device configuration
pop-up only appears if a device cannot be found; when everything verifies,
the app starts without interruption.

The "Device Connections" card is collapsible and collapsed by default; its
header shows a summary (e.g. `3/3 verified`), and it expands automatically
when something is missing. Per-device connection status lives on each port
row inside this card. `Devices -> Auto Discover USB Devices` (or the card's
Auto Discover button) re-runs the probe at any time.

## Red Pitaya prerequisite: patched linien-server

V_tune goes through the Linien server on the Red Pitaya, which must be patched
to expose the `dac_click_voltage` parameter (the patch adds an MCP4921 SPI
driver to `write_registers()`, using `DIO3_N` as chip select). The patch and
its full walkthrough live in the `qmot-linien-controls` repo. Notes that
matter on the client side (already handled by this app):

- The DAC full scale is **0–5 V**, set in hardware by the Click board's Vref;
  the software range must match it (`--vtune-max`).
- The linien parameter cache must be **off** for these writes
  (`use_parameter_cache=False`) or they are silently swallowed.
- Each DAC update costs ~0.3 s on the server (deliberate chip-select retry
  loop), so the app only pushes the most recent slider value.
- `DIO3_N` (bank `n`, bit 3) is reserved as the DAC chip select; the repump
  gate is fixed on `DIO1_N` and never touches it.

## Dispenser fire behavior

The dispenser button fires the DPS5020 at the fixed operating point — the
worker programs 7.5 A / 5.0 V before every enable, so the dispenser (a
near-short) runs in constant-current mode — for 30 s, then the app turns the
output off; clicking again during a fire aborts it. Either way a 15 min
cooldown starts, shown as a countdown, and the fire button is locked out
until it expires. The cooldown end time is saved in `QSettings`, so
restarting the app does not reset it. The setpoints are re-enforced during a
fire and applied to any output enabled from the DPS front panel while the
app is connected, which also gets the same fire time limit. The auto-off
runs in the app — if the app or the serial link dies mid-fire, the supply
does not shut off on its own.

The `Override...` button next to the fire button opens a dialog for changing
the fire current (up to the DPS5020's 20 A) and duration (1–120 s); a
confirmation window restates the requested values and the safe default
before anything is applied. Overridden values are shown in the Setpoint row
with an orange `(OVERRIDE)` marker, persist across restarts, and can be
restored via the dialog's Defaults button (the safe operating point remains
`DispenserWorker.DISPENSE_CURRENT_A` / `FIRE_SECONDS` in `mot_workers.py`).
The override is locked out while a fire is running.

## UI behavior

- **Health strip** (title row): color-coded chips for the laser lock, USB
  verification, coil supply (with live current), and dispenser phase —
  readable from across the lab.
- **Typed entry**: every slider's value box is editable — type a number
  (suffix optional), press Enter or click away, and the value is clamped
  and applied.
- **Wheel guard**: the mouse wheel never changes an unfocused control, so
  scrolling the column can't silently move a hardware setpoint. Click a
  control first if you want wheel adjustment.
- **Event log pane**: a collapsible card at the bottom of the column
  mirrors the JSONL log; its header always shows the latest event.
- **Dispenser progress bar**: time remaining in the current fire (orange)
  or cooldown (blue).
- **Tooltips**: every control has hover help; informational readouts show
  a (?) cursor.
- **Persistent UI state**: window geometry, splitter position, camera
  pane visibility, and card expansion are restored on the next launch
  (alongside all control parameters).
- Slider drags are coalesced into single hardware writes (latest value
  wins) instead of replaying every intermediate step.

## Event log

Everything notable is appended to `logs/mot-YYYYMMDD.jsonl` next to the
code (a frozen/packaged build writes to the OS per-user app-data
directory instead, since the install location may be read-only). One
JSON object per line, gitignored, safe to tail or load with
`pandas.read_json(..., lines=True)`): app start/stop, USB probe results,
device connections and faults, lock/unlock transitions, laser on/off and
init, setpoint changes (debounced after a slider drag settles), V_tune/VCO
frequency changes, supply readbacks (every 10 s, plus immediately on any
output or protection change), DPS protection trips, and every dispenser
fire (start, end with actual elapsed time and abort flag, cooldown,
override). The dispenser card also shows a cumulative `Fires:` counter
(count and total seconds, persisted) for tracking dispenser life.

## DPS protection status

The supplies' protection register is decoded on every poll; if a supply
trips OVP/OCP/OPP, the readback row turns red (e.g.
`OCP TRIPPED — 0.00 V / 0.00 A`) and a `protection_trip` event is logged.

## Thermistor temperature

The Koheron thermistor setpoint (ohms) is also shown as an estimated
temperature (`Temp (est):`), using the Steinhart–Hart coefficients for the
laser head's 10 k NTC carried over from the old qMOT-os rig.

## Fonts

Body text is **IBM Plex Sans**; titles (app header, card/section titles)
are **Space Grotesk**. Both are bundled in `fonts/` under the SIL Open
Font License, so the app looks the same on a fresh clone. Any extra font
files dropped into `fonts/` are also registered but stay local (licensed
files such as `PP*`/`Aptos*` are gitignored). A missing family falls
back to Helvetica Neue with a console warning. Note: fonts are
registered from bytes (`addApplicationFontFromData`) because the
path-based Qt API silently fails on Qt 6.11/macOS. The smoke test
asserts the *resolved* families on real widgets, so a regression here
fails CI rather than just looking wrong.

## Packaged builds (Mac / Linux / Windows)

GitHub Actions (`.github/workflows/build.yml`) runs the test suite on
every push, and builds standalone executables for all three platforms on
a version tag (`v*`) or a manual workflow run. Artifacts:
`OpenQuantumControls-macos.zip` (an .app bundle),
`OpenQuantumControls-linux.tar.gz`, and
`OpenQuantumControls-windows.zip`; a tag also attaches them to a GitHub
release.

Local build (any platform):

```bash
pip install pyinstaller
pyinstaller OpenQuantumControls.spec --noconfirm   # -> dist/
```

Notes:

- The binaries are unsigned. First launch on macOS needs
  right-click → Open (or `xattr -dr com.apple.quarantine <app>`);
  Windows SmartScreen needs "More info → Run anyway".
- Packaged builds write logs and data to the OS per-user app-data
  directory (macOS:
  `~/Library/Application Support/OQ-Controls/OpenQuantumControls/logs`)
  instead of a `logs/` folder next to the executable.
- On Linux, serial access usually requires
  `sudo usermod -a -G dialout $USER` (log out/in afterwards).

## Tests

`tests/` contains three hardware-free suites, run directly:

```bash
.venv/bin/python tests/test_dispenser.py   # fire/cooldown/override state machine
.venv/bin/python tests/test_probe.py       # USB port identification logic
.venv/bin/python tests/smoke_ui.py         # offscreen UI build + behavior checks
```

The smoke test uses isolated settings/log files and refreshes
`mot-ui-smoke.png`.

## Parameter persistence

All control parameters are saved to `QSettings` as they change and restored
on the next launch: Linien modulation frequency/phase/strength and sweep
speed, laser current and thermistor setpoints, MOT coil voltage/current,
the V_tune slider, and the dispenser override. Restoring a parameter only
sets the UI — nothing is pushed to hardware until you move a control (and
the laser sliders re-adopt the Koheron's actual setpoints on connect, as
the V_tune slider does from the DAC).

## DPS register scaling

The two supplies use different Modbus current/power resolutions: DPS5005 is
0.001 A and 0.01 W per LSB, DPS5020 is 0.01 A and 0.1 W per LSB. On first
connect, sanity-check the app's readback against each supply's front panel.

## Run

The repo stands alone — clone, install, run:

```bash
git clone https://github.com/OpenQuantum-Org/oq-controls.git
cd oq-controls
uv venv --python 3.12 .venv
uv pip install -r requirements.txt
uv run python mot_control.py \
  --rp-host rp-f0f2b5.local \
  --laser-port /dev/ttyUSB0 \
  --mot-coil-port /dev/ttyUSB1 \
  --dispenser-port /dev/ttyUSB2
```

(The port arguments are optional — the startup USB probe finds and
verifies the devices automatically.)

The app persists connection settings in `QSettings` and re-verifies them at
startup, so command-line arguments are mostly needed for first launch or
scripted startup.

Use `Devices -> Configure Devices...` to inspect discovered USB serial devices,
apply best guesses, or enter ports manually (the same dialog pops up
automatically at startup when auto-detection fails).

## Linien GPIO mapping

Linien exposes GPIO outputs as bitfields (`gpio_n_out`: `DIO0_N` through
`DIO7_N`). The repump gate is fixed in code at `DIO1_N` (bank `n`, bit 1,
Click Shield socket 2), active-low; `DIO3_N` (bit 3) is the DAC Click chip
select and is never touched by the gate. Both mappings were verified
empirically in `qmot-linien-controls` — change `_start_repump_workers()` in
`mot_control.py` if the wiring ever changes.
