# Roadmap

Working sequence for the current improvement effort. One line per step and why
it sits where it does. This is the *order*; implementation detail belongs in a
plan made at the start of each session, against the code as it exists that day.

Architectural rules live in `CLAUDE.md`, not here. This file is expected to
shrink and change — edit it directly as priorities move.

Status: `[ ]` not started · `[~]` in progress · `[x]` done

Last updated 2026-08-14.

---

## [x] 0. CLAUDE.md

Environment facts, hardware-safety rules, architecture invariants, state
ownership, performance principles, linien hazards.

*Why first:* every step below inherits it. Rules written before the work get
enforced; rules written after are just documentation.

## [x] 1. Feedback loop

Confirmed working 2026-08-13: all three suites pass, and the
edit → `smoke_ui.py` → read `mot-ui-smoke.png` loop is verified end to end.
Wrapped as the `/verify` skill, whose real job is forcing the screenshot read —
a green run only proves the UI constructed, not that it looks right.

`/bench` (step 8) is the only other skill worth making, and not yet.

## [x] 2. Colours and card order

Done 2026-08-14, and it grew well past "colours and card order": the dark grey
flat theme, one font and one body size, the collapsible-card restructure, the
PSD window port, the linien locking rewrite (SIMPLE mode, determine-offset,
selection-bar cleanup), unit fixes for modulation amplitude and frequency,
temperature and MHz readouts in place of ohms and volts, and a spacing pass.

Mod Strength is now **Mod Amplitude** (`mot_control.py:1612`).

## [ ] 3. Logging substrate — the experimental record

Goes well beyond replacing the 38 `print()` calls. The log is not an ops
convenience; it is the record of what the hardware actually did, and months
later it is the only evidence of why a measurement looked the way it did.
Build it to that standard:

- **Every hardware transaction logged** — what was commanded, what read back,
  how long it took, what the outcome was. Commands and their results are
  correlated by ID, not left to be inferred from adjacency.
- **Self-describing sessions.** Each log opens with app version, git SHA,
  connected device identities and firmware, and resolved ports/hosts. A log
  read in six months must stand alone.
- **Stable structured fields**, so `logs/*.jsonl` is queryable. No prose
  messages carrying the real information.
- **Categories that support filtering:** user action, command, readback, state
  change, fault.
- **Faults can never be lost.** No fault path ends at stdout (CLAUDE.md
  invariant 4). Install an unhandled-exception hook that logs before dying.
- **Durations on device operations**, per the performance principles — this is
  the app's only profiling infrastructure.
- Define retention and rotation (currently one file per day).

*Why before the threading fix:* 30 of the 38 prints are in `linien_worker.py`,
the exact file step 4 rewrites, and today every linien fault is invisible in
the JSONL. A threading fix cannot be verified at the bench when the failure
mode scrolls past in a terminal. This step is what makes step 4 debuggable.

## [ ] 4. Linien threading fix

Convert GUI→worker calls to queued invocation, return reads via signals, end
the shared-RPyC-connection race between `LinienWorker.run()` and the GUI thread.
Apply the conflation and staleness rules from CLAUDE.md. Plan mode first, then
bench verification.

*Why here:* top correctness bug, plausible root cause of the "linien randomly
misbehaves" class. Establishes the pattern the remaining ~20 call sites get
converted to, so everything after it is written correctly the first time.

## [ ] 5. Safe shutdown

Confirmation dialog on close that shows live state of every output — laser,
coil DPS, dispenser DPS, repump gate, lock — and does not let the app exit with
something energised unnoticed. Backed by worker-level teardown that drives
outputs to a safe state (CLAUDE.md invariant 7), so the guarantee holds even
when the app dies without a clean close.

*Why after step 4:* a dialog querying live device state needs the queued read
path to exist. The worker-side safe teardown is the more important half —
the dialog only helps on a clean exit.

*Open:* is "everything off" always right? Leaving the laser locked across an app
restart may be desirable. Likely a per-item checklist showing state with
choices, not a blanket kill.

## [ ] 6. Headless hardware entry point

`python -m oq.cli dps status`, `laser read`, `linien connect`. `RidenDPSClient`
and the probe functions are already GUI-free; the rest is pulling protocol code
out of the QThread wrappers.

*Why after step 4:* the threading fix establishes worker-owns-client, giving
the CLI a clean seam. By then you will have hand-verified at the bench and know
which checks are worth automating.

## [~] 7. Structural UI, done as the module split

The **UI half is done** (step 2): filters and lock are one *Error Signal & Lock*
card, `DeviceConfigDialog` is gone entirely and replaced by the always-visible
Devices panel. Both open questions below are answered by what shipped.

What remains is only the **module split**. `mot_control.py` is still 3,400 lines
with `OpenQuantumControlsWindow` most of it, so the plan to extract each panel
"on the way out" did not happen — the UI work went in place instead. It is now
its own job rather than a side effect, which makes it less attractive: it is
pure churn against a file that currently works.

*Reconsider before doing:* the god class is a real cost only if you keep adding
panels. If the UI is settled, the split buys less than steps 3–5.

## [ ] 8. `/bench` skill

Hardware-in-the-loop checklist: power-up order, what to connect, what "working"
looks like per subsystem.

*Why last:* only writable honestly after doing steps 4–7 at the bench.

---

## Open questions

Both original questions were answered by shipping step 2: device config is one
always-visible Devices panel with a nested collapsed *Manual Setup*, and filters
and lock are a single collapsed *Error Signal & Lock* card.

What is open now is all bench work, none of it answerable from the desk:

1. **Does the lock actually engage in SIMPLE mode**, and does *Determine signal
   offset* move it? Ported from linien 2.1.0, never run against the RP.
2. **Do the temperature and frequency sliders land where they claim?** The
   Steinhart-Hart and VCO conversions are unverified against a real reading.
3. **Is the 16-38 °C range right**, and is 0.1 V / 0.1 A too coarse for setting
   coil current?
4. **Does everything actually turn off on close?** `_silence_modulation_on_exit`
   is wired but only step 5 makes this a guarantee.

## Ordering rationale, in one line

Rules before code, feedback loop before changes, observability before the fix
it verifies, correctness before the refactor that spreads it, and the module
split as a side effect of UI work rather than a project needing its own
justification.
