# OpenQuantum Controls

Control UI for the rubidium MOT hardware: a Koheron laser controller, a
Linien/Red Pitaya spectroscopy lock, two Riden DPS supplies, and the repump
tune/gate. The repo is self-contained — the Linien spectroscopy widget and
worker, the shared styles, and the Koheron worker are vendored here.

Architectural rules live in `CLAUDE.md`; what is left to do and what was
decided against live in `ROADMAP.md`.

## Hardware covered

- **Koheron CTL200-0** laser controller: MOT laser current, temperature
  setpoint, and laser on/off
- **Red Pitaya STEMlab 125-14** running a patched linien-server: the
  spectroscopy lock for that laser (see [Setup](#setup))
- **Repump VCO tune voltage** (V_tune) through the MikroE DAC Click (MCP4921)
  on the Red Pitaya Click Shield, socket 1, driven via the patched server's
  `dac_click_voltage` parameter. The UI shows the resulting VCO frequency
  (measured: 0–5 V spans 2800–2970 MHz, linear)
- **Repump RF gate** on a Red Pitaya GPIO bit — fixed at `DIO1_N` (bank `n`,
  bit 1), **active-low** (RF amp enable on Click Shield socket 2)
- **MOT coil DPS5005**: voltage, current, output
- **Dispenser DPS5020**: a timed constant-current fire
  (see [Dispenser](#dispenser))
- Optional USB camera pane below the spectroscopy plot

# Setup

## 1. Red Pitaya

**The image.** Linien 2.x requires **Red Pitaya OS 2.x** on a STEMlab 125-14
(Linien 1.x ran on OS 1.x and is unmaintained). The image this experiment runs
is kept here:

> **[Red Pitaya SD card image](https://drive.google.com/file/d/1fvGqLEU4Z0ARxMitc8ChkZGRuVNWU0dQ/view?usp=drive_link)**

Write it to the card with Balena Etcher or the equivalent — Red Pitaya's
[Prepare SD card](https://redpitaya.readthedocs.io/en/latest/quickStart/SDcard/SDcard.html)
guide covers the mechanics, and stock images live at
[downloads.redpitaya.com](https://downloads.redpitaya.com/downloads/) if you
ever need to start from one. After a major OS change, reset the calibration to
factory default (or recalibrate). If the card predates OS 2.x, reflash it before
anything else.

**The server on this RP is hand-patched and must not be reinstalled.**
`rp-f0f2b5.local` runs linien-server **2.1.0** with an MCP4921 SPI driver added
to `write_registers()`, which is what exposes `dac_click_voltage` and drives the
repump VCO. `DIO3_N` is its chip select. The patch and its walkthrough live in
the `qmot-linien-controls` repo.

Two things destroy it, both by running `pip3 uninstall linien-server -y` on the
device:

- running **linien-gui** (`linien`) against this Red Pitaya — it installs the
  server automatically on first connect;
- calling `install_remote_server()` from `linien_client.deploy` yourself.

Both fire automatically when the client and server versions disagree, which is
why the client is pinned (below).

**Check the device before trusting it.** SSH in (`root`/`root` — the same
credentials the app uses):

```bash
ssh root@rp-f0f2b5.local
python3 -c "import linien_server; print(linien_server.__version__)"   # 2.1.0
linien-server status
linien-server start        # if it is not running
```

`rp-f0f2b4.local` is a **different** Red Pitaya, one character away. Check the
hostname you typed before concluding anything is broken.

**Check the patch is present** from the lab PC — this is exactly what the app's
V_tune worker does, and the only positive proof the patched server is the one
answering:

```python
from linien_client.connection import LinienClient
from linien_client.device import Device

client = LinienClient(Device(host="rp-f0f2b5.local", username="root", password="root"))
client.connect(autostart_server=False, use_parameter_cache=False)
print(hasattr(client.parameters, "dac_click_voltage"))   # must be True
```

`False` means an unpatched server is installed: the app will run, the lock will
work, and the repump V_tune slider will do nothing. Run this check after
flashing a card, rather than assuming the image already carries the patch.

A Red Pitaya that was just powered on is slow to answer, and its `.local`
hostname takes a while to resolve — if the first connect fails, wait a minute or
two and press Auto-connect again before assuming anything is wrong.

## 2. Lab PC

Python 3.12. Clone, make a virtualenv, install into it:

```powershell
git clone https://github.com/OpenQuantum-Org/oq-controls.git
cd oq-controls
py -3.12 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# macOS / Linux
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Three things that bite:

- **Always use the explicit `.venv\Scripts\python.exe -m pip` form.** A bare
  `pip` hits the global interpreter and clobbers editable linien installs there.
- **linien-client is pinned to 2.1.0** in `requirements.txt`, matching the
  patched server — do not float it. A newer client raises
  `InvalidServerVersionException` on connect, and the paths that respond to that
  reinstall the server, which uninstalls the patch. A client version containing
  `dev` skips the version check entirely; that is not a workaround, it turns a
  loud failure into a silent one.
- On Linux, serial access needs `sudo usermod -a -G dialout $USER` and a
  re-login.

## 3. First run

```powershell
.venv\Scripts\python.exe src\mot_control.py --rp-host rp-f0f2b5.local
```

Serial ports are optional — the startup probe finds and verifies them. Every
setting is remembered in `QSettings`, so later launches need no arguments at
all.

| flag | default | purpose |
| --- | --- | --- |
| `--rp-host` | saved, else `rp-f0f2b5.local` | Red Pitaya running linien-server |
| `--laser-port`, `--mot-coil-port`, `--dispenser-port` | auto-probed | serial ports |
| `--mot-coil-address`, `--dispenser-address` | `1` | Modbus addresses |
| `--vtune-min`, `--vtune-max` | `0.0`, `5.0` | V_tune slider range; 5 V is the DAC's hardware full scale, set by the Click board's Vref |

Expect on a good start: `DEVICES 4/4` on the Devices card, all four rows **OK**,
and a sweeping trace. If the Red Pitaya is not up, a banner over the plot says
so and names the button to press.

# Using it

## Devices, status and connecting

The **Devices** card lists the four devices, each with its port or host and one
of exactly three statuses:

| status | meaning |
| --- | --- |
| **OK** | connected |
| **...** | an attempt is in flight |
| **Error** | everything else |

The reason is always on the row's tooltip and in the event log — unplugged
port, probe failure, wedged server, serial fault. The card's header chip counts
the devices that are OK (`DEVICES 3/4`).

**Auto-connect** connects whatever is not connected: it brings up the Red
Pitaya, then re-runs the USB probe and restarts the serial workers. A Red Pitaya
that is already connected is left alone, because restarting the acquisition
client sweeps and that drops a lock. **Manual setup** holds the host/port fields,
a **Connect** button that always does the full restart (it *will* drop a lock),
and **Restart Linien Server**, which restarts a wedged server without an SSH
session.

**The Red Pitaya connect makes three attempts, two seconds apart.** If all three
fail, a banner over the plot names the host it tried and tells you to press
Auto-connect. Nothing retries on a timer after that — and a Red Pitaya that was
just powered on may need a couple of goes a minute or two apart before its
hostname resolves.

## USB auto-detection

At startup the app identifies each serial device with read-only commands: an
`ilaser` query for the Koheron, and a Modbus model-register read that
distinguishes the DPS5005 (coils) from the DPS5020 (dispenser). The saved port
for each device is re-verified first, so swapped cables correct themselves; a
device that fails verification is searched for across the remaining ports.

Nothing polls the Koheron once it is connected — it reads two setpoints and its
output state, and then only accepts writes — so a separate watcher enumerates the serial ports every
few seconds and marks any device whose port has disappeared. It reports only;
reopening a port is Auto-connect's job.

## Dispenser

The dispenser fires the DPS5020 in constant current (it is close to a short) at
the current and duration set on the Dispenser card's sliders, defaulting to
**7.5 A for 30 s**. The worker programs current and voltage before every enable
and turns the output off when the time is up; clicking again during a fire
aborts it. Clicking Fire again returns straight to firing.

**There is no re-fire lockout.** The 15 min cooldown was removed on 2026-10-16
at the operator's request. Nothing in the app now limits how often the
dispenser runs, so pacing fires - and the Rb they consume - is entirely the
operator's job. The time limit on a single fire is all that is left.

Setpoints are re-enforced during a fire, and an output enabled from the DPS
front panel while the app is connected gets the same time limit. **The auto-off
runs in the app** — if the app or the serial link dies mid-fire, the supply does
not shut itself off.

Cumulative use (number of fires and total seconds) is counted and persisted for
dispenser life, but has no on-screen readout: it reaches you through the event
log and the `dispenser` section of a saved config.

## Lock, filters and PSD

`Set PID...` opens a dialog pre-filled with the current P/I/D gains (0–8191)
read live from the server, and writes them back on Apply.

The Error Signal & Lock card exposes Linien's two IIR stages on the demodulated
error signal (channel A): Automatic (two low-passes at half the modulation
frequency, Linien's default) or manual control of each stage's enable, type and
corner frequency. Settings are read from the device when Linien connects and
pushed back after a short debounce. A disabled stage stays in the signal path at
unity gain, to preserve its propagation delay. If you are mid-edit when the
readback lands, your values stand and the disagreement is logged as
`filter_divergence`.

`Measure Lock PSD...` switches the plot to a log-log error-signal noise view and
the sidebar to the PSD panel: algorithm (LPSD or Welch), max decimation (deeper
reaches lower Fourier frequencies but takes ~16x longer per +4; default 18),
`Run PSD Measurement`, `Optimize PID` (confirmation required — it changes the
lock's gains while measuring), `Clear Plot` and `Exit PSD Mode`. Runs overlay in
different colours with their PID settings in the legend. Each completed run logs
a `psd_complete` event with linien's integrated-noise "fitness" — trend it as a
lock-health metric. Measure while locked. The live trace pauses during raw
acquisition, and leaving the mode does not abort an in-flight acquisition on the
server.

## Config snapshots

`Save Config As...` writes a timestamped JSON snapshot of every **setpoint**:
connections, modulation, sweep speed, error-signal filters, laser current and
temperature, repump V_tune/VCO frequency, coil setpoints, the dispenser
operating point and fire history, and the PSD settings.

A config records what you *set*, never what the hardware is *doing*. It cannot
carry an output, enable, lock or fire state — those keys are stripped on the way
out and on the way in, and the removal is logged — so **loading a config can
never switch the laser, coils, repump, dither or dispenser on or off**. The PID
is not saved either: it lives only on the server, so recording it would mean
recording hardware state.

`Load Config...` applies a snapshot: setpoints go to their connected devices
immediately, including to a running laser or an engaged lock. Partial files are
fine (missing keys are left alone), dispenser fire settings are
confirmation-gated, port and host changes take effect on the next connect, and
fire history is never loaded.

## UI behaviour

- **Status chips on every card header**: device count, laser, repump, lock,
  coils (with live current) and dispenser phase, so a collapsed column still
  reads from across the lab. The window's title row holds only the Autozoom and
  camera toggles.
- **Typed entry**: every slider's value box is editable — type a number, press
  Enter, and it is clamped and applied. Arrow keys work in the boxes.
- **Wheel guard**: the mouse wheel never changes an unfocused control, so
  scrolling the column cannot silently move a setpoint.
- **Coalesced writes**: a slider drag becomes one hardware write (latest value
  wins), not a replay of every intermediate step.
- **Persistent UI state**: window geometry and splitter position are restored.
  Cards always start collapsed and the camera always starts off. Restoring a
  setting only sets the UI — nothing is pushed until you move a control, and on
  connect the laser sliders adopt the Koheron's actual setpoints (as V_tune does
  from the DAC).
- **Laser setpoints survive a power cycle** (2026-10-16). The one place the
  restore runs the other way: if the Koheron's output is **off** when the app
  connects, the saved current and temperature are written *to* it rather than
  read from it. A power cycle leaves the CTL200 at 0 mA / 9999 Ω, and adopting
  that discarded the operating point every time. If the diode is lit — or
  `lason` cannot be read — the app adopts as usual and never writes, because
  pushing current into a running laser unasked is the failure that matters.
- **Tooltips** on every control.

## Event log

Everything notable is appended to `logs/mot-YYYYMMDD.jsonl` next to the code (a
packaged build writes to the OS per-user app-data directory instead). One JSON
object per line, gitignored, safe to tail or to load with
`pandas.read_json(..., lines=True)`. Every record carries `t`, `event` and
`kind`, and `kind` is the filter axis: `session`, `user`, `command`, `readback`,
`state`, `fault`.

- Hardware transactions are logged where they happen, as a correlated
  start/end pair sharing an `op` id, with `duration_ms` and `ok` on the end.
  That is the only profiling this app needs.
- **Repeated identical faults are collapsed**: the first is written, the rest
  are counted, and the count lands as a `fault_repeated` record. An unplugged
  supply used to write the same line twice a second.
- **linien's own logging is captured** — the connect attempt, the host it could
  not resolve, the SSH and RPyC stack — as `library` events, rather than going
  to the console where a real traceback lives.
- Supply readbacks are logged **on change** (output enable, protection trip),
  not on a timer.
- Retention is 90 days.

The **Config & Log** card mirrors the log in a pane: time, a source column, and
the message, coloured by severity. linien's DEBUG detail stays in the file.

# Reference

## File map

| file | what lives there |
| --- | --- |
| `src/mot_control.py` | The window and every card, plus the app entry point. ~3,900 lines, most of it `OpenQuantumControlsWindow`; navigate it by card (`_add_*_card`). |
| `src/mot_workers.py` | One QThread per device: both DPS supplies, the Koheron setpoints, the DAC Click (V_tune), the repump GPIO gate, the PSD client, the USB probe and the serial-port watcher. |
| `src/linien_worker.py` | The linien acquisition/lock client — connect with retries, the spectrum poll, autolock, the server heartbeat. |
| `src/linien_widget.py` | The spectroscopy plot: trace drawing, sweep range, line selection, lock buttons, the Red Pitaya banner. |
| `src/psd_window.py` | The lock-noise (PSD) panel, ported from linien's noise-analysis window. |
| `src/mot_logging.py` | `EventLog`: the JSONL record, correlated transactions, fault-repeat collapsing, and the bridge that captures third-party logging. |
| `src/styles.py` | Colours, fonts, and the shared widget builders (sliders, readouts, cards). |
| `tests/` | Five hardware-free suites; `smoke_ui.py` is the big one and writes the screenshot. |
| `CLAUDE.md` | The rules: invariants, state ownership, what was tried and reverted. Read before changing behaviour. |
| `ROADMAP.md` | What is left, and what was decided against. |

## Red Pitaya client notes

Four RPyC clients talk to the one server — acquisition/lock, V_tune, repump
gate, and PSD. Do not add a fifth (a linien GUI). The V_tune and gate clients
start only once the acquisition client reports connected.

- The parameter cache must be **off** for the V_tune and gate clients
  (`use_parameter_cache=False`): with it on, a value read straight back after a
  write returns the stale cached copy until the next queue drain.
- Each DAC update costs ~0.3 s on the server (a deliberate chip-select retry
  loop), so only the most recent slider value is pushed.
- Unit constants come from `linien_common.common`, never a local literal: `Vpp`
  per volt peak-to-peak for `modulation_amplitude`, `MHz` for
  `modulation_frequency`, `V` counts per volt for PSD amplitudes.

## Linien GPIO mapping

Linien exposes GPIO outputs as bitfields (`gpio_n_out`: `DIO0_N`–`DIO7_N`). The
repump gate is fixed at `DIO1_N` (bank `n`, bit 1, Click Shield socket 2),
active-low; `DIO3_N` is the DAC Click chip select and the gate never touches it.
Both mappings were verified empirically in `qmot-linien-controls` — change
`_start_repump_workers()` in `mot_control.py` if the wiring changes.

## DPS register scaling

The supplies use different Modbus resolutions: **DPS5005** is 0.001 A and
0.01 W per LSB, **DPS5020** is 0.01 A and 0.1 W per LSB. The wrong one
misreports current by 10x. On first connect, check the app's readback against
each supply's front panel.

## DPS protection

The protection register is decoded on every poll. A trip (OVP/OCP/OPP) turns the
header chip to `COILS TRIP` and logs a `protection_trip` fault naming which
protection fired.

## Laser temperature

The Laser card's Temperature slider is in °C (16–38), which is what you actually
set. It is converted to a thermistor resistance with the Steinhart–Hart
coefficients for the head's 10 k NTC before being sent to the Koheron; the ohms
are an implementation detail of how the controller holds the setpoint.

## Fonts

Body text is **IBM Plex Sans**, titles are **Space Grotesk**, both bundled in
`fonts/` under the SIL Open Font License. Extra font files dropped into `fonts/`
are registered but stay local (licensed `PP*`/`Aptos*` files are gitignored). A
missing family falls back to Helvetica Neue and logs a `font` fault. Fonts are
registered from bytes, because the path-based Qt API silently fails on Qt 6.11.

## Tests

Five hardware-free suites, run directly. CI runs all five on every push to
`main` or `v1` and on every pull request.

```powershell
.venv\Scripts\python.exe tests\test_logging.py     # event log: correlation, faults, retention
.venv\Scripts\python.exe tests\test_workers.py     # linien unit conversions, link health, stalls
.venv\Scripts\python.exe tests\test_dispenser.py   # fire state machine, VCO + thermistor
.venv\Scripts\python.exe tests\test_probe.py       # USB port identification logic
.venv\Scripts\python.exe tests\smoke_ui.py         # offscreen UI build + behaviour checks
```

All five are hardware-free, so green says nothing about the devices. A green run
says the UI *constructed* — read `mot-ui-smoke.png`, which the smoke test writes
(gitignored; it is a build output, read once right after the run that made it).

## Packaged builds

`.github/workflows/build.yml` runs the suites on every push and pull request,
and builds standalone executables for macOS, Linux and Windows on a version tag
(`v*`) or a manual run. A tag also attaches them to a GitHub release.

```bash
pip install pyinstaller
pyinstaller OpenQuantumControls.spec --noconfirm   # -> dist/
```

- The binaries are unsigned: macOS needs right-click → Open (or
  `xattr -dr com.apple.quarantine <app>`), Windows SmartScreen needs
  "More info → Run anyway".
- Packaged builds write logs and data to the OS per-user app-data directory
  (macOS: `~/Library/Application Support/OQ-Controls/OpenQuantumControls/logs`).
