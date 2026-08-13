# Roadmap

Working sequence for the current improvement effort. One line per step and why
it sits where it does. This is the *order*; implementation detail belongs in a
plan made at the start of each session, against the code as it exists that day.

Architectural rules live in `CLAUDE.md`, not here. This file is expected to
shrink and change — edit it directly as priorities move.

Status: `[ ]` not started · `[~]` in progress · `[x]` done

Last updated 2026-08-13.

---

## [x] 0. CLAUDE.md

Environment facts, hardware-safety rules, architecture invariants, state
ownership, performance principles, linien hazards.

*Why first:* every step below inherits it. Rules written before the work get
enforced; rules written after are just documentation.

## [x] 1. Feedback loop

Confirmed working 2026-08-13: all three suites pass, and the
edit → `smoke_ui.py` → read `mot-ui-smoke.png` loop is verified end to end.
The loop is three commands documented in `CLAUDE.md`; it needs no tooling
around it.

## [ ] 2. Colours and card order

The cheap half of the UI work. Card order is the sequence of
`parent.addWidget(card)` calls in the `_build_*` methods; colours are
`styles.py` plus inline stylesheets.

*Why here:* zero risk, and it produces something visibly better within an hour
while the next three steps are invisible plumbing.

Also fold in: **Mod Strength** (`mot_control.py:1742`) is the modulation
amplitude control — exists and is fully wired, but is named unhelpfully and
defaults to `0 mV` so it looks inert. Rename and give it a sane default.

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

## [ ] 7. Structural UI, done as the module split

Merge Error Signal Filters into Linien Lock (already adjacent in the layout).
Inline `DeviceConfigDialog` — note ports are *already* inline, so the dialog is
additive; the work is deciding what replaces the auto-open at
`mot_control.py:2229`. Each panel extracted into its own module on the way out
of `OpenQuantumControlsWindow`.

*Why last among the big items:* these touch the same panels step 4 rewrote and
the same code a god-class split would move. Done together, each panel is cut
once and the refactor is a side effect of UI work you already wanted.

## [ ] 8. `/bench` skill

Hardware-in-the-loop checklist: power-up order, what to connect, what "working"
looks like per subsystem.

*Why last:* only writable honestly after doing steps 4–7 at the bench.

---

## Open questions

Neither blocks steps 1–6. Both must be answered before step 7 can be planned.

1. **Device config inlining** — per-device connection detail in each device's
   own card, or one always-visible Devices panel replacing the dialog? What
   should happen at `mot_control.py:2229`, where it currently auto-opens?
2. **Merged filter + lock card** — one card with both sets of controls, or
   sections/tabs? Linien Lock already carries four rows and two buttons, so the
   merged card runs tall. Should it start expanded (filters currently default
   to collapsed, `mot_control.py:1559`)?

## Ordering rationale, in one line

Rules before code, feedback loop before changes, observability before the fix
it verifies, correctness before the refactor that spreads it, and the module
split as a side effect of UI work rather than a project needing its own
justification.
