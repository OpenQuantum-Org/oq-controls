# OpenQuantum Controls

PyQt6 control panel for a MOT: Koheron CTL200 laser, two Riden DPS supplies
(MOT coil + Rb dispenser), Red Pitaya running linien, USB camera. Polled at
1–5 Hz over serial and RPyC.

`README.md` has hardware detail, register maps, GPIO wiring. `ROADMAP.md` has
the work sequence and open questions.

## Commands

```powershell
.venv\Scripts\python.exe src\mot_control.py --rp-host rp-f0f2b5.local
.venv\Scripts\python.exe tests\test_logging.py
.venv\Scripts\python.exe tests\test_workers.py
.venv\Scripts\python.exe tests\test_dispenser.py
.venv\Scripts\python.exe tests\test_probe.py
.venv\Scripts\python.exe tests\smoke_ui.py    # refreshes mot-ui-smoke.png
```

Install with the explicit `.venv\Scripts\python.exe -m pip` form — a bare `pip`
hits global Python and clobbers the editable linien installs there.

After any UI change, run `smoke_ui.py` and **read the PNG**. All five suites
are hardware-free, so green says nothing about devices — never report a
device-facing change as working without the bench.

## Hardware safety

Reads are free. **Ask before every actuation, every time** — prior approval
never carries forward:

- Dispenser fire (7.5 A, 30 s, consumes Rb). The 15-min lockout was
  **removed on 2026-10-16** at the operator's request: nothing now rate-limits
  re-firing, so the per-fire time limit is the only guard left and the asking
  matters more than it did.
- Laser enable or current change
- Setpoint change on a locked system
- DPS output enable

**Load Config is an exemption, decided 2026-08-17.** It pushes every setpoint in
the file to its connected device immediately, with no prompt, including to a
running laser and a locked system. Since 2026-11-03 that includes the PID gains,
saved as the gains last set from the app (`_remember_pid`), never read off the
server. What it can never do is switch an output on or
off: a config carries setpoints only, and `_reject_state_keys` drops any key that
describes an output (`_CONFIG_STATE_KEYS`) on the way both out and in.

**The lock is the repump sideband, not the carrier.** The VCO's ~2.915 GHz
sideband is locked to Rb-85 F=2→F'=3 and the 3 MHz dither drives the VCO, so
the error signal sees only the sideband. Tuning the repump while locked is how
the carrier's cooling detuning is swept (docs: `openquantum-documentation`
ch. 3, "Reference Implementation"). Reason from this before touching lock
point, repump or offset code.

**MOT on 2026-10-19/20, none the week before — cause not yet isolated.**
Locking at 2912 MHz and tuning the repump to ~2920 gave a MOT; no code change
caused it (Track repump was off). Two candidates, untested apart:
- *Sweep speed.* While locked the repump moved at 1–2.5 MHz/s on 2026-10-16
  (DAC jumps ~2 MHz, some 12–53 MHz) and 0.14–0.3 MHz/s on 10-19/20 (jumps
  ≤0.3 MHz). Fast sweeps cross the few-MHz MOT window quicker than a MOT
  loads, and big jumps can slip the lock onto another zero crossing unseen.
- *Lock feature.* The lock must sit on a feature that slides as the repump
  is tuned (the sideband). Some 10-16 locks (≈ −0.06 V) were on one that
  barely slid; but most of that day's locks were at +0.26 to +0.58 V, near
  this week's +0.2 V, so this does not separate the weeks cleanly.
Sweep slowly (~0.2 MHz/s, 0.1 MHz steps) and lock on a sliding feature.

**Track repump was removed on 2026-10-20 at the operator's request.** It
rewrote Mod Phase and `combined_offset` on a locked system as the repump moved
(three designs, 2026-10-16 to -19). It never helped get a MOT, and the lock
broke more easily while it ran. Do not rebuild it without being asked.
*Lock here* (lock at the sweep centre, no line search, with a Slope toggle;
built 2026-10-16) was removed on 2026-10-21 as unnecessary, same rule.
So was the *Signal Offset* slider (hand-set `combined_offset`, re-applied
after every unlock); the server's own value, zeroed by `start_sweep` and set
by the autolock when Determine signal offset is ticked, now stands alone.

*Record Repump Scan* (née "Calibrate vs Repump": stepped the unlocked
repump 2800–2970 MHz and saved error I/Q + monitor) was removed on
2026-10-21 too. Its traces stay in `logs/repump-cal-*.npz` and
`logs/repump-scan-*.npz`; old logs say `repump_calibration`/`repump_scan`.
Lock selections are still logged as `lock_selection`.

Laser operating point is **80 mA** (`mot_control.py:1850`). Do not widen slider
ranges or nudge it as a side effect of other work.

On connect the direction depends on whether the diode is lit, **decided
2026-10-16** (this reverses `d441b65` for one case):

- **Output off** → the app writes its *saved* current and temperature to the
  Koheron (`_restore_laser_setpoints`). A power cycle leaves the CTL200 at
  0 mA / 9999 Ω and adopting that threw the operating point away every time.
- **Output on, or `lason` unreadable** → the sliders adopt the Koheron's
  actual setpoints, as before. Unreadable counts as on: writing current into a
  running diode is the error that matters, so silence is not permission.

**Thermistor setpoint is unresolved.** Code default is 6215 Ω (`:1866`), which
`thermistor_ohms_to_c` maps to 36.2 °C. Target is ~27 °C ≈ 9171 Ω; a 10 k NTC
reads 10 kΩ at 25 °C. Do not change the default until confirmed.

## Invariants

1. GUI thread never blocks on I/O — no serial, socket, or RPyC call from a slot.
2. One thread per device client. RPyC and `serial.Serial` are not thread-safe.
3. GUI→worker is queued invocation; worker→GUI is signals. No direct calls.
4. Faults go to `EventLog`, never `print`.
5. New UI code goes in a subsystem module, not `OpenQuantumControlsWindow`.
6. Every I/O call has a timeout and a failure path that surfaces visibly.
7. Teardown stops workers before widgets die, and drives outputs safe first —
   the dispenser's auto-off lives in its worker thread, so killing it mid-fire
   leaves the output on.

1 and 3 are **deliberately not true** for `LinienWorker` and
`LaserSetpointWorker`. Both were converted to the mutex + pending-queue pattern
on 2026-08-14 and **reverted the same day** — the queue was correct but made the
UI feel worse (setpoints landing a poll late, writes sharing the acquisition
connection), and the operator's judgement is the gate that matters. They call
RPyC and serial from GUI slots again, `get_pid`/`get_filters` block, and
`lock_laser` blocks the GUI for up to 30 s. Known and accepted; don't re-fix it
without being asked.

The DPS, DAC Click and GPIO workers *do* use the queue pattern
(`RidenDPSWorker._queue`) and should keep it.

4 holds: no `print`s remain anywhere. Faults go to `EventLog`; `LinienWorker._log`
is the shim for what used to be printed.

Third-party logging goes there too. `capture_library_logging` (`mot_logging.py`)
detaches `linien_client`'s own console handler and routes linien/RPyC/SSH records
into the log as `library` events — WARNING and worse as faults. It must run
**after** those imports: `linien_client/__init__.py` installs its console handler
and sets its module loggers to DEBUG at import time, so raising a level before
that (what `quiet_console` used to do) is overwritten and does nothing anyway.
Four workers import `linien_client` and any of them may be the one that loads it,
so **every import site calls the no-arg sweep**; the window installs the bridge
itself at startup. Two console paths remain and are outside `logging`:
`start_remote_server` writes remote command output to `sys.stdout` and
`connection.py` calls `print_exc()`. Both fire only when the RP answers but its
server is down.

The log pane renders through `_log_line` (`mot_control.py`): a timestamp, a
padded source column (the device for a generic fault, the short library name for
captured chatter, otherwise the event) and the message, coloured by severity.
Records are one line each (`NoWrap`), trimmed per field and then per line.

6 has two halves, and both are easy to get wrong in a poll loop. A failure that
repeats at the poll rate is reported **once**: `LinkHealth` (`mot_workers.py`)
turns a run of failures into a down edge (three in a row → `connection_status`
goes to error, so the row and the DEVICES count say so) and an up edge on
recovery, and `EventLog.fault` collapses identical repeats into one record plus a
`fault_repeated` count. Unplugging a supply used to write ~400 identical lines.

The Koheron is the exception that needs its own answer: it has no poll loop at
all, so nothing notices it being unplugged. `SerialPresenceWorker` enumerates
ports (never opens them, so it costs nothing on the wire) and the window marks a
device whose port has vanished. It reports only — reconnecting stays the
operator's call.

## State ownership

- Desired (what the user set) and actual (what the device reports) are separate.
  A readback never overwrites a control the user is touching. Both readbacks
  that could (`_sync_filters_from_device`, `_on_modulation_readback`) hold off
  behind a "touched" flag and log a `*_divergence` fault instead; the flag
  clears once the operator's own value has been pushed, so it is not a latch.
- Show divergence, don't paper over it — it means a failed write, a front-panel
  override, or a device in protection.
- Setpoints conflate (latest value wins); side-effecting actions queue normally.
- Tag reads so late responses can't clobber newer input.
- Restoring persisted settings sets the UI only, never pushes to hardware.

## Performance

Not throughput-bound and never will be — 1–5 Hz over 9600 baud. Performance
means latency.

- **Count round trips, not cycles.** `get_filters()` is seven sequential RPyC
  hops, not one. Batch reads; don't micro-optimize the code around them.
- GUI thread budget is 16 ms; one round trip can eat it. Hence invariant 1.
- Wrap device ops in `EventLog.command()`: it writes `duration_ms` on every
  transaction, and that is the only profiling this app needs.
- Coalesce input before it becomes traffic (see the 400 ms filter-push timer).
- Warm plot paths mutate, never rebuild — `linien_widget.py:108`/`:271` is the
  reference.
- Round-trip cost to `rp-f0f2b5.local` is **unmeasured**. Measure it before
  arguing from it.
- **Measured and fine, do not optimize:** camera (native Qt capture path,
  `QGraphicsVideoItem` since 2026-11-03 so the MOT box can overlay it; the
  `mot_meter` brightness tap converts one frame per 200 ms, unmeasured), PSD
  plot rebuild (cold, seconds apart), worker poll loops (nowhere near serial
  saturation).

## Linien

`rp-f0f2b5.local` runs a **hand-patched linien-server 2.1.0** (`dac_click_voltage`
MCP4921 patch applied on the device). Client pinned to 2.1.0 to match.

- **Never run linien-gui against this RP** — `install_remote_server()` runs
  `pip3 uninstall linien-server -y` and destroys the patch. Fires automatically
  on `ServerNotInstalled` or `InvalidServerVersion`.
- A client version containing `dev` skips the compatibility check entirely. Not
  a strategy — it turns a loud failure silent.
- Never point two clients at one RP — but note this app already opens **four**
  to the same server: acquisition (`linien_worker`), the vtune DAC, the repump
  gate and PSD. They are all ours, and each costs a per-client parameter queue on
  the server. The vtune and gate clients only start once the acquisition client
  reports connected (`_on_linien_connection`), so an absent RP produces one set of
  connect attempts rather than three. Consolidating the four into one connection
  is still open, and has to resolve `use_parameter_cache` being True for
  acquisition and False for the writers.
- The acquisition client tries `CONNECT_ATTEMPTS` (3) times, 2 s apart, then
  stops and the plot shows a banner naming the host and Auto-connect. Reconnect
  stays a decision the operator makes; nothing retries on a timer forever.
- `rp-f0f2b4.local` is a different RP, one character off.
- The fallback host is `DEFAULT_RP_HOST` in `linien_widget.py` and
  `linien_worker.py`, now `rp-f0f2b5.local`. It used to be `rp-F0C6AD.local`, a
  different device: an empty host field quietly connected to someone else's
  hardware. Keep the two definitions in step.

**Reference source: `C:\Users\kostu\Desktop\linien` (v2.1.0, commit 6caa6a9).**
Match it when changing linien-facing behaviour — PSD mode is a direct port of
its noise-analysis window. Do **not** read `klab-linien-relocking_stable`: it is
2.2.0.dev0 and differs where it matters, renaming the RPCs
(`exposed_start_psd_acquisition` vs `start_psd_acquisition`) and the counts→volts
constant (`VOLTS_TO_COUNTS_FACTOR` vs `V`). Both of its spellings are wrong for
our pinned 2.1.0 client and patched server.

Unit constants come from `linien_common.common`, never a local literal: `Vpp`
(4095.75) per volt peak-to-peak for `modulation_amplitude`, `MHz`
(`0x10000000/8`) per MHz for `modulation_frequency`, and `V = 8192` counts per
volt for PSD amplitudes. Writing physical units straight into these parameters
is silently wrong by 3-4 orders of magnitude.

## Landmines

- **DPS scaling is per-model.** DPS5005 = 0.001 A/LSB, DPS5020 = 0.01 A/LSB.
  Wrong one misreports current by 10x.
- QSettings persists between runs — stale ports look like code bugs.
- Repump gate is `DIO1_N`; `DIO3_N` is the DAC Click chip select.

## Code layout

`mot_control.py` is ~3,800 lines; `OpenQuantumControlsWindow` is ~2,800 of them
across 110 methods. A split was considered on 2026-08-14 and **declined**: the
file is navigable by card (`_add_*_card`), it works, and the churn buys nothing
functional. Revisit only if panels keep being added. Colours live in
`styles.py`.

## Method

One change at a time. Read the code and cite line numbers; don't describe
behaviour from memory. Debugging: reproduce, pull evidence from `logs/*.jsonl`,
one hypothesis, smallest discriminating change. If you can't reproduce it, say
so rather than shipping a plausible fix.

**Verification budget: run the five suites, glance at the screenshot, hand it
over.** That is the whole check for UI work. The user's manual check is fast and
is the real gate, so getting work in front of them beats proving it yourself.

Specifically, do not: bisect to root-cause a cosmetic layout detail, prove a new
assertion fails without its fix, measure widget geometry to confirm what a
screenshot already shows, or re-run suites that passed and whose inputs have not
changed. Fix the visible thing, say what you did not verify, move on.

Real bugs still get real debugging — this is about not gold-plating the
confirmation of work that already looks right.

Comments explain why a value is what it is, not what the code does.
