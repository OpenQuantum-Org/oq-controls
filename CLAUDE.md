# OpenQuantum Controls

PyQt6 control panel for a MOT: Koheron CTL200 laser, two Riden DPS supplies
(MOT coil + Rb dispenser), Red Pitaya running linien, USB camera. Polled at
1–5 Hz over serial and RPyC.

`README.md` has hardware detail, register maps, GPIO wiring. `ROADMAP.md` has
the work sequence and open questions.

## Commands

```powershell
.venv\Scripts\python.exe mot_control.py --rp-host rp-f0f2b5.local
.venv\Scripts\python.exe tests\test_dispenser.py
.venv\Scripts\python.exe tests\test_probe.py
.venv\Scripts\python.exe tests\smoke_ui.py    # refreshes mot-ui-smoke.png
```

Install with the explicit `.venv\Scripts\python.exe -m pip` form — a bare `pip`
hits global Python and clobbers the editable linien installs there.

After any UI change, run `smoke_ui.py` and **read the PNG**. All three suites
are hardware-free, so green says nothing about devices — never report a
device-facing change as working without the bench.

## Hardware safety

Reads are free. **Ask before every actuation, every time** — prior approval
never carries forward:

- Dispenser fire (7.5 A, 30 s, 15-min lockout, consumes Rb)
- Laser enable or current change
- Setpoint change on a locked system
- DPS output enable

Laser operating point is **80 mA** (`mot_control.py:1850`). Do not widen slider
ranges or nudge it as a side effect of other work. On connect the sliders adopt
the Koheron's actual setpoints — keep that direction.

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

3 and 4 are **not yet true**: ~24 direct GUI→worker calls, 38 `print`s (30 in
`linien_worker.py`). Don't add more; convert what you touch.

## State ownership

- Desired (what the user set) and actual (what the device reports) are separate.
  A readback never overwrites a control the user is touching.
  `_sync_filters_from_device` violates this today.
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
- Log durations on device ops. That's the only profiling this app needs.
- Coalesce input before it becomes traffic (see the 400 ms filter-push timer).
- Warm plot paths mutate, never rebuild — `linien_widget.py:108`/`:271` is the
  reference.
- Round-trip cost to `rp-f0f2b5.local` is **unmeasured**. Measure it before
  arguing from it.
- **Measured and fine, do not optimize:** camera (native Qt capture path), PSD
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
- Never point two clients at one RP.
- `rp-f0f2b4.local` is a different RP, one character off.
- Fallback host in `linien_widget.py:42` and `linien_worker.py:25` is
  `rp-F0C6AD.local` — wrong device. Known TODO.

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

`mot_control.py` is 3,452 lines; `OpenQuantumControlsWindow` is ~2,500 of them
across 129 methods. Don't add to it — extract the panel you're changing into
its own module. Colours live in `styles.py`.

## Method

One change at a time. Read the code and cite line numbers; don't describe
behaviour from memory. Debugging: reproduce, pull evidence from `logs/*.jsonl`,
one hypothesis, smallest discriminating change. If you can't reproduce it, say
so rather than shipping a plausible fix.

**Verification budget: run the three suites, glance at the screenshot, hand it
over.** That is the whole check for UI work. The user's manual check is fast and
is the real gate, so getting work in front of them beats proving it yourself.

Specifically, do not: bisect to root-cause a cosmetic layout detail, prove a new
assertion fails without its fix, measure widget geometry to confirm what a
screenshot already shows, or re-run suites that passed and whose inputs have not
changed. Fix the visible thing, say what you did not verify, move on.

Real bugs still get real debugging — this is about not gold-plating the
confirmation of work that already looks right.

Comments explain why a value is what it is, not what the code does.
