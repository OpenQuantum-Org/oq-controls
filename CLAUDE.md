# OpenQuantum Controls

PyQt6 control panel for a MOT experiment: Koheron CTL200 laser controller, two
Riden DPS supplies (MOT coil + Rb dispenser), a Red Pitaya running linien for
laser locking, and a USB camera. Instruments are polled at 1–5 Hz over serial
and RPyC.

`README.md` documents hardware, register maps, GPIO wiring, and behaviour.
Read it before changing device logic — do not duplicate it here.

`ROADMAP.md` holds the current work sequence and open design questions. Check
it before starting anything substantial, and update the status marks as you go.

## Environment

Windows, PowerShell. Python 3.12 in `.venv`.

```powershell
.venv\Scripts\python.exe mot_control.py --rp-host rp-f0f2b5.local
.venv\Scripts\python.exe tests\test_dispenser.py   # fire/cooldown state machine
.venv\Scripts\python.exe tests\test_probe.py       # USB port identification
.venv\Scripts\python.exe tests\smoke_ui.py         # offscreen UI build; refreshes mot-ui-smoke.png
```

Always install with the explicit `.venv\Scripts\python.exe -m pip` form. A bare
`pip` hits global Python and replaces the `2.2.0.dev0` editable linien installs
that live there.

`smoke_ui.py` builds the entire UI with no hardware and writes
`mot-ui-smoke.png`. For any UI or styling change: edit, run it, then **look at
the PNG**. That is the verification loop for visual work — use it before
handing anything over.

## Hardware safety

Reads are free — DPS voltage/current, laser setpoints, thermistor, linien lock
state, port probing. Do those without asking.

**Ask before every actuation, every time.** Prior approval never carries
forward to the next one:

- Dispenser fire — 7.5 A for 30 s, 15-minute lockout, consumes Rb
- Laser enable/disable or current changes
- Any setpoint change on a currently locked system
- DPS output enable

## Architecture invariants

1. The GUI thread never blocks on I/O. No serial, socket, or RPyC call from a
   slot or a widget callback.
2. A device client is touched by exactly one thread — the worker that owns it.
   RPyC connections and `serial.Serial` handles are not thread-safe.
3. GUI→worker is queued invocation (`QMetaObject.invokeMethod` with
   `Qt.QueuedConnection`, or a signal into the worker). Worker→GUI is signals.
   No direct method calls in either direction.
4. Faults go to `EventLog`, never `print`. Anything that only reaches stdout is
   invisible in `logs/*.jsonl` and therefore undebuggable after the fact.
5. New UI code goes in a subsystem module, not `OpenQuantumControlsWindow`.

Rules 3 and 4 are **not yet true of existing code** — there are ~24 direct
GUI→worker calls in `mot_control.py` and 38 `print()` calls, 30 of them in
`linien_worker.py`. These are being converted. Do not add new violations, and
convert what you touch.

Two more that apply to every worker:

6. **Every I/O call has a bounded timeout and a defined failure path.** A
   worker blocked forever on a serial read is a silently dead subsystem — the
   UI keeps showing the last good value and nobody learns anything. On timeout,
   emit a fault and degrade visibly.
7. **Teardown is ordered and explicit.** Stop the worker, join it, *then* let
   widgets die. A worker emitting into a deleted widget is the standard
   crash-on-exit in Qt apps. Note that the dispenser's auto-off runs in its
   worker thread, so killing that thread mid-fire leaves the output enabled —
   teardown must drive outputs to a safe state before exiting.

## State ownership and command semantics

A control panel has two sources of truth: **desired** state (what the user set)
and **actual** state (what the device reports). Conflating them is the root of
most instrument-UI bugs. Keep them separate and make reconciliation explicit.

- **A device readback never silently overwrites a control the user is
  interacting with.** If a widget has focus or has been edited since the last
  push, the readback updates the *actual* display, not the desired value.
  `_sync_filters_from_device` currently violates this — it overwrites the UI
  wholesale, which is exactly how "my typed value got reverted" happens.
- **Show divergence rather than hiding it.** When desired and actual disagree
  beyond tolerance, that is information — a failed write, a front-panel
  override, a device in protection. Surface it; do not paper over it by
  snapping the UI to whichever value arrived last.
- **Setpoint commands conflate; they do not queue.** Once GUI→worker is queued
  (invariant 3), a slider drag can post faster than the worker drains. Keep the
  **latest value only** per parameter — FIFO here means setpoints landing
  seconds after the user let go, and the queue growing without bound. Actions
  with side effects (fire, connect, enable) are *not* conflatable and must
  queue normally.
- **Late responses must not clobber newer input.** When reads return via
  signals, tag each request and drop responses that are older than the most
  recent user action on that control.
- **Restoring persisted settings sets the UI only.** Nothing is pushed to
  hardware until the user moves a control. This is existing, deliberate
  behaviour (see README) — preserve it.

## Performance principles

**This application is not throughput-bound and never will be.** It is a 1–5 Hz
instrument panel driving 9600-baud serial links. Computation is free here.
Performance work means *latency and responsiveness*, not speed of code.

**Cost is round trips, not cycles.** Every `client.parameters.X.value` is a
network hop to the Red Pitaya, and every DPS register access is a serial
transaction. `LinienWorker.get_filters()` reads seven parameters, so it costs
seven sequential round trips — not one. When something is slow, count round
trips before profiling code. Make reads batch into a single call rather than
making each one faster.

**The GUI thread has a hard latency budget: 16 ms.** Anything on it must fit
inside a frame. A single RPyC round trip to the Red Pitaya or a DPS Modbus
transaction can consume that entirely, and they compound — so this reduces to a
simple rule: no I/O on the GUI thread, ever (invariant 1). Freezes here are the
only performance problem users actually perceive.

**Measure before asserting.** The per-round-trip cost to `rp-f0f2b5.local` has
not been characterised — the argument above assumes tens of milliseconds but
that number is unverified. Time it and record the result here; a seven-round-
trip read is a very different problem at 2 ms than at 200 ms.

**Make latency observable in the event log.** Device operations should record
their duration as structured `EventLog` fields, not just their outcome. That
puts performance data in the same place as faults, so a slow link and a broken
one are distinguishable from `logs/*.jsonl` after the fact instead of requiring
a live reproduction. This is the only profiling infrastructure this app needs.

**Time-to-interactive counts as performance.** Startup does USB port
enumeration and per-port probing, which is slow on Windows. That work belongs
off the GUI thread with the UI usable and honestly labelled as still
discovering — never behind a blocked window or a modal.

**Poll intervals are a shared budget.** Each worker's poll loop is a standing
cost on a link other workers also use. Prefer event-driven updates; when
polling is unavoidable, poll as slowly as the UX tolerates and justify the
interval in a comment. Do not add new poll loops without removing or slowing
another.

**Coalesce input before it becomes device traffic.** A slider drag must not
become 100 writes. The existing debounce idiom is correct and should be reused:
`_debounced_log` for log entries, and the 400 ms timer in `_on_filter_changed`
that collapses typing into one hardware write.

**On warm plot paths, mutate — do not rebuild.** `linien_widget.py` is the
reference: curves are created once (`:108`), updated with `setData` (`:271`),
and the Y range is fixed (`:98`) rather than recomputed per frame. Creating and
removing plot items per update is only acceptable on cold paths.

**Do not optimize these — they were measured and are fine.** The camera runs on
Qt's native `QVideoWidget` capture path with no per-frame Python. The PSD plot
recreates curves per update but runs once per acquisition stage, seconds apart.
The 100–2000 ms worker poll loops are nowhere near saturating 9600-baud serial.
Work on any of these is decorative; spend the effort on round-trip count and
GUI-thread blocking instead.

## Linien

Targets `rp-f0f2b5.local`, running a **hand-patched linien-server 2.1.0** (the
`dac_click_voltage` MCP4921 patch is applied directly to `parameters.py` /
`registers.py` on the device). The client in `.venv` is pinned to 2.1.0 to match.

- **Never run linien-gui against this Red Pitaya.** Its
  `install_remote_server()` path runs `pip3 uninstall linien-server -y` on the
  device and destroys the patch. It triggers automatically on
  `ServerNotInstalled` or `InvalidServerVersion`.
- A client version containing `dev` skips the client/server compatibility check
  entirely. That is a dev escape hatch, not a compatibility strategy — it turns
  a loud failure into a silent one. Keep the client pinned so the check passes
  on its merits.
- Never point two clients at one Red Pitaya simultaneously.
- `rp-f0f2b4.local` is a **different** Red Pitaya, one character off.

The hardcoded fallback host in `linien_widget.py:42` and `linien_worker.py:25`
is `rp-F0C6AD.local` — the wrong device. On fresh QSettings, always pass
`--rp-host`. (Fixing that default is a known TODO.)

## Landmines

- **DPS scaling differs by model.** DPS5005 is 0.001 A / 0.01 W per LSB;
  DPS5020 is 0.01 A / 0.1 W. Getting this wrong silently misreports current by
  10x. Sanity-check readback against the supply's front panel.
- **QSettings persists between runs**, so stale ports and hosts survive a
  restart and look like code bugs. `smoke_ui.py` isolates settings and logs to a
  temp dir; anything else you write that touches QSettings should too.
- The repump GPIO gate is fixed at `DIO1_N` and `DIO3_N` is the DAC Click chip
  select — verified empirically, see README before changing.

## Code layout

`mot_control.py` is 3,452 lines and `OpenQuantumControlsWindow` is ~2,500 of
them across 129 methods. It is the main obstacle to working in this repo.

Do not add to it. UI restructuring work should **extract** the panel being
changed into its own module rather than editing in place — the restructure and
the split are one operation, not two. Colours and shared widget styling live in
`styles.py`.

## Style

Match the surrounding code. Existing classes carry substantial docstrings that
explain hardware behaviour and the reasoning behind limits — keep that up;
those comments are load-bearing for anyone debugging at the bench. Comments
should explain why a value or sequence is what it is, not restate the code.
